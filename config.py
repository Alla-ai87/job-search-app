from collections import defaultdict

import httpx

from app.config import Settings
from app.database import JobsRepository


def build_daily_summary(jobs: list[dict]) -> str:
    if not jobs:
        return "No new jobs were found today."

    grouped: dict[str, list[dict]] = defaultdict(list)
    for job in jobs:
        company_name = (job.get("companies") or {}).get("name") or job.get("company_name") or "Unknown company"
        title = (job.get("target_job_titles") or {}).get("title") or "Unknown target title"
        grouped[f"{company_name} / {title}"].append(job)

    lines = ["Daily job-search summary", ""]
    for key, group in grouped.items():
        lines.append(key)
        for job in group:
            link = job.get("application_link") or job.get("source_url") or "No application link"
            sponsorship = job.get("sponsorship_status") or "unknown"
            location = job.get("location") or "Remote/unspecified"
            lines.append(f"  - {job.get('title')} | {location} | {sponsorship} | {link}")
        lines.append("")
    return "\n".join(lines).strip()


async def send_daily_email(repo: JobsRepository, settings: Settings, dry_run: bool = False) -> str:
    # Fetch once and reuse – avoids two round-trips.
    todays_jobs = repo.list_todays_jobs()
    body = build_daily_summary(todays_jobs)

    if dry_run:
        return body

    if not settings.email_api_key:
        raise RuntimeError("EMAIL_API_KEY must be configured to send email")
    if not settings.email_to:
        raise RuntimeError("EMAIL_TO must be configured to send email")

    payload = {
        "from": settings.email_from,
        "to": [settings.email_to],
        "subject": "Daily job-search summary",
        "text": body,
    }
    headers = {"Authorization": f"Bearer {settings.email_api_key}"}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
        response.raise_for_status()

    repo.create_log(
        "send_daily_email",
        "success",
        "Daily summary sent",
        {"jobs_count": len(todays_jobs)},
    )
    return body
