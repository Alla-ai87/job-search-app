from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from supabase import Client, create_client

from app.config import Settings, get_settings

# Module-level cached client (created once per process, not per request).
_client: Client | None = None


def get_supabase(settings: Settings | None = None) -> Client:
    global _client
    if _client is None:
        settings = settings or get_settings()
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured")
        _client = create_client(settings.supabase_url, settings.supabase_service_role_key)
    return _client


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _today_utc() -> str:
    """Return today's date in UTC as an ISO string (YYYY-MM-DD)."""
    return datetime.now(UTC).date().isoformat()


class JobsRepository:
    def __init__(self, client: Client):
        self.client = client

    # ── Companies / titles ────────────────────────────────

    def list_active_companies(self) -> list[dict[str, Any]]:
        return self.client.table("companies").select("*").eq("active", True).execute().data or []

    def list_active_titles(self) -> list[dict[str, Any]]:
        return self.client.table("target_job_titles").select("*").eq("active", True).execute().data or []

    # ── Search queue ──────────────────────────────────────

    def create_queue_item(self, company_id: int, title_id: int) -> dict[str, Any] | None:
        """Insert a pending queue item. Returns None if one already exists (open combo)."""
        existing = (
            self.client.table("search_queue")
            .select("id")
            .eq("company_id", company_id)
            .eq("target_job_title_id", title_id)
            .in_("status", ["pending", "processing"])
            .limit(1)
            .execute()
            .data
        )
        if existing:
            return None
        payload = {
            "company_id": company_id,
            "target_job_title_id": title_id,
            "status": "pending",
            "attempts": 0,
        }
        return self.client.table("search_queue").insert(payload).execute().data[0]

    def fetch_pending_queue(self, limit: int) -> list[dict[str, Any]]:
        return (
            self.client.table("search_queue")
            .select("*, companies(*), target_job_titles(*)")
            .eq("status", "pending")
            .order("created_at")
            .limit(limit)
            .execute()
            .data
            or []
        )

    def update_queue_status(self, queue_id: int, status: str, error: str | None = None) -> None:
        payload: dict[str, Any] = {"status": status}
        if status == "processing":
            payload["started_at"] = _utc_now()
        if status in {"done", "failed"}:
            payload["finished_at"] = _utc_now()
        if error:
            payload["last_error"] = error
        self.client.table("search_queue").update(payload).eq("id", queue_id).execute()

    def increment_queue_attempts(self, queue_id: int, current_attempts: int) -> None:
        self.client.table("search_queue").update({"attempts": current_attempts + 1}).eq("id", queue_id).execute()

    def count_existing_jobs_for_combo(self, company_id: int, title_id: int) -> int:
        """Return how many job_results rows already exist for this company/title combo."""
        response = (
            self.client.table("job_results")
            .select("id", count="exact")
            .eq("company_id", company_id)
            .eq("target_job_title_id", title_id)
            .execute()
        )
        return response.count or 0

    # ── Job results ───────────────────────────────────────

    def job_exists(self, job_id: str | None, application_link: str | None, dedup_key: str | None) -> bool:
        """Three-tier deduplication: job_id → application_link → composite dedup_key."""
        if job_id:
            if self.client.table("job_results").select("id").eq("job_id", job_id).limit(1).execute().data:
                return True
        if application_link:
            if (
                self.client.table("job_results")
                .select("id")
                .eq("application_link", application_link)
                .limit(1)
                .execute()
                .data
            ):
                return True
        if dedup_key:
            if (
                self.client.table("job_results")
                .select("id")
                .eq("dedup_key", dedup_key)
                .limit(1)
                .execute()
                .data
            ):
                return True
        return False

    def save_job(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Insert job if not already stored. Returns the saved row or None if duplicate."""
        dedup_key = payload.get("_dedup_key")
        if self.job_exists(payload.get("job_id"), payload.get("application_link"), dedup_key):
            return None
        # Strip internal helper key before inserting
        clean = {k: v for k, v in payload.items() if not k.startswith("_")}
        return self.client.table("job_results").insert(clean).execute().data[0]

    def update_job_tracking(self, job_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update ATS tracking fields (applied, interview, rejected, offer, notes, date_applied)."""
        result = self.client.table("job_results").update(updates).eq("id", job_id).execute()
        return result.data[0] if result.data else None

    def list_recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return (
            self.client.table("job_results")
            .select("*, companies(name), target_job_titles(title)")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    def list_todays_jobs(self) -> list[dict[str, Any]]:
        """Return jobs created today (UTC). Timezone-safe."""
        return (
            self.client.table("job_results")
            .select("*, companies(name), target_job_titles(title)")
            .gte("created_at", _today_utc())
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )

    # ── Search run history ────────────────────────────────

    def start_search_run(self, triggered_by: str = "scheduled") -> int:
        """Insert a search_runs row and return its id."""
        row = self.client.table("search_runs").insert({"triggered_by": triggered_by}).execute().data[0]
        return row["id"]

    def finish_search_run(self, run_id: int, queued: int, processed: int, saved: int, errors: int) -> None:
        self.client.table("search_runs").update(
            {
                "queued": queued,
                "processed": processed,
                "saved_jobs": saved,
                "errors": errors,
                "finished_at": _utc_now(),
            }
        ).eq("id", run_id).execute()

    def list_search_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return (
            self.client.table("search_runs")
            .select("*")
            .order("started_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )

    # ── Generic helpers ───────────────────────────────────

    def count_table(self, table: str, filters: Iterable[tuple[str, str, Any]] = ()) -> int:
        query = self.client.table(table).select("id", count="exact")
        for column, op, value in filters:
            query = getattr(query, op)(column, value)
        return query.execute().count or 0

    def create_log(self, event: str, status: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        self.client.table("search_logs").insert(
            {
                "event": event,
                "status": status,
                "message": message,
                "metadata": metadata or {},
            }
        ).execute()
