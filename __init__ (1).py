from typing import Any

import httpx

from app.config import Settings


class SerpApiClient:
    def __init__(self, settings: Settings):
        if not settings.serpapi_api_key:
            raise RuntimeError("SERPAPI_API_KEY must be configured")
        self.api_key = settings.serpapi_api_key

    async def search_jobs(
        self,
        company: str,
        job_title: str,
        location: str = "United States",
    ) -> list[dict[str, Any]]:
        params = {
            "engine": "google_jobs",
            "q": f'{company} "{job_title}" jobs',
            "location": location,
            "hl": "en",
            "api_key": self.api_key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get("https://serpapi.com/search.json", params=params)
            response.raise_for_status()
            payload = response.json()
        return payload.get("jobs_results", []) or []


def extract_application_link(job: dict[str, Any]) -> str | None:
    for option in job.get("apply_options") or []:
        link = option.get("link")
        if link:
            return link
    for link_obj in job.get("related_links") or []:
        url = link_obj.get("link")
        if url:
            return url
    return job.get("share_link")


def _make_dedup_key(company_name: str, title: str, location: str | None) -> str:
    """
    Composite fallback dedup key: lower(company)|lower(title)|lower(location).
    Must match the generated column expression in schema.sql.
    """
    return "|".join(
        part.lower().strip()
        for part in [company_name, title, location or ""]
    )


def normalize_serp_job(
    job: dict[str, Any],
    company_id: int,
    title_id: int,
    fallback_company: str,
) -> dict[str, Any]:
    """
    Map a raw SerpAPI job dict to a job_results row dict.

    Internal helper keys prefixed with ``_`` are stripped before DB insert.
    """
    description = job.get("description") or ""
    extensions = " ".join(job.get("extensions") or [])
    application_link = extract_application_link(job)
    title = job.get("title") or "Untitled role"
    company_name = job.get("company_name") or fallback_company
    location = job.get("location")

    return {
        "company_id": company_id,
        "target_job_title_id": title_id,
        "title": title,
        "company_name": company_name,
        "location": location,
        "job_id": job.get("job_id"),
        "application_link": application_link,
        "source_url": job.get("share_link") or application_link,
        "description": description,
        "detected_extensions": job.get("extensions") or [],
        "sponsorship_status": None,  # filled in by processor after detection
        # Internal helpers – stripped before insert
        "_sponsorship_text": f"{description} {extensions}",
        "_dedup_key": _make_dedup_key(company_name, title, location),
    }
