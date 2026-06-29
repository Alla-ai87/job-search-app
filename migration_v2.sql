from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.database import JobsRepository, get_supabase
from app.schemas import (
    DashboardStats,
    EmailSummaryRequest,
    EnqueueRequest,
    RunSummary,
    UpdateJobTracking,
)
from app.services.emailer import send_daily_email
from app.services.processor import process_queue_batch
from app.services.queue import enqueue_daily_searches
from app.services.serpapi import SerpApiClient

router = APIRouter()


def get_repo() -> JobsRepository:
    # get_supabase() returns a module-level cached client – no new connection per request.
    return JobsRepository(get_supabase())


# ── Health ────────────────────────────────────────────────

@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Dashboard stats ───────────────────────────────────────

@router.get("/stats", response_model=DashboardStats)
def stats(repo: JobsRepository = Depends(get_repo)) -> DashboardStats:
    return DashboardStats(
        active_companies=repo.count_table("companies", [("active", "eq", True)]),
        active_titles=repo.count_table("target_job_titles", [("active", "eq", True)]),
        pending_queue=repo.count_table("search_queue", [("status", "eq", "pending")]),
        new_jobs_today=len(repo.list_todays_jobs()),
    )


# ── Jobs ──────────────────────────────────────────────────

@router.get("/jobs")
def jobs(limit: int = 50, repo: JobsRepository = Depends(get_repo)) -> list[dict]:
    return repo.list_recent_jobs(limit)


@router.patch("/jobs/{job_id}/tracking")
def update_tracking(
    job_id: int,
    body: UpdateJobTracking,
    repo: JobsRepository = Depends(get_repo),
) -> dict:
    """Update ATS tracking fields (applied, interview, rejected, offer, notes, date_applied)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    result = repo.update_job_tracking(job_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return result


# ── Queue ─────────────────────────────────────────────────

@router.post("/queue/daily", response_model=RunSummary)
def queue_daily(
    body: EnqueueRequest | None = None,
    repo: JobsRepository = Depends(get_repo),
) -> RunSummary:
    """
    Enqueue searches. Optionally restrict to specific company_ids and/or title_ids
    to conserve SerpAPI quota.
    """
    req = body or EnqueueRequest()
    queued = enqueue_daily_searches(repo, req.company_ids, req.title_ids)
    return RunSummary(queued=queued)


@router.post("/queue/process", response_model=RunSummary)
async def process_queue(
    repo: JobsRepository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> RunSummary:
    return await process_queue_batch(repo, SerpApiClient(settings), settings)


# ── Full daily run (enqueue + process + email) ────────────

@router.post("/daily-run", response_model=RunSummary)
async def daily_run(
    body: EnqueueRequest | None = None,
    repo: JobsRepository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> RunSummary:
    run_id = repo.start_search_run(triggered_by="api")
    req = body or EnqueueRequest()
    queued = enqueue_daily_searches(repo, req.company_ids, req.title_ids)
    summary = await process_queue_batch(repo, SerpApiClient(settings), settings)
    summary.queued = queued
    await send_daily_email(repo, settings)
    repo.finish_search_run(run_id, queued, summary.processed, summary.saved_jobs, summary.errors)
    return summary


# ── Search run history ────────────────────────────────────

@router.get("/runs")
def search_runs(limit: int = 20, repo: JobsRepository = Depends(get_repo)) -> list[dict]:
    return repo.list_search_runs(limit)


# ── Email ─────────────────────────────────────────────────

@router.post("/email/summary")
async def email_summary(
    request: EmailSummaryRequest,
    repo: JobsRepository = Depends(get_repo),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    body = await send_daily_email(repo, settings, dry_run=request.dry_run)
    return {"summary": body}


# ── Reference data ────────────────────────────────────────

@router.get("/companies")
def list_companies(repo: JobsRepository = Depends(get_repo)) -> list[dict]:
    return repo.list_active_companies()


@router.get("/titles")
def list_titles(repo: JobsRepository = Depends(get_repo)) -> list[dict]:
    return repo.list_active_titles()
