from app.config import Settings
from app.database import JobsRepository
from app.schemas import RunSummary
from app.services.serpapi import SerpApiClient, normalize_serp_job
from app.services.sponsorship import detect_sponsorship_status


async def process_queue_batch(repo: JobsRepository, serpapi: SerpApiClient, settings: Settings) -> RunSummary:
    items = repo.fetch_pending_queue(settings.batch_size)
    summary = RunSummary(processed=len(items))

    for item in items:
        queue_id = item["id"]
        attempts = item.get("attempts", 0)
        company = item["companies"]
        target_title = item["target_job_titles"]

        # Skip this combo if the DB already has enough results for it.
        # This avoids wasting SerpAPI quota on combos that are fully populated.
        existing_count = repo.count_existing_jobs_for_combo(company["id"], target_title["id"])
        if existing_count >= settings.max_jobs_per_combo:
            repo.update_queue_status(queue_id, "done")
            continue

        slots_remaining = settings.max_jobs_per_combo - existing_count
        saved_for_combo = 0

        try:
            repo.update_queue_status(queue_id, "processing")
            # Increment attempts only when actually making the API call.
            repo.increment_queue_attempts(queue_id, attempts)

            jobs = await serpapi.search_jobs(company["name"], target_title["title"])

            for job in jobs:
                if saved_for_combo >= slots_remaining:
                    break

                payload = normalize_serp_job(
                    job=job,
                    company_id=company["id"],
                    title_id=target_title["id"],
                    fallback_company=company["name"],
                )
                payload["sponsorship_status"] = detect_sponsorship_status(
                    payload.pop("_sponsorship_text", ""),
                    payload["title"],
                    payload["company_name"],
                ).value

                saved = repo.save_job(payload)
                if saved:
                    saved_for_combo += 1
                    summary.saved_jobs += 1

            repo.update_queue_status(queue_id, "done")

        except Exception as exc:
            summary.errors += 1
            repo.update_queue_status(queue_id, "failed", str(exc))
            repo.create_log(
                event="process_queue_item",
                status="error",
                message=str(exc),
                metadata={
                    "queue_id": queue_id,
                    "company": company.get("name"),
                    "title": target_title.get("title"),
                },
            )

    repo.create_log(
        event="process_queue_batch",
        status="success" if summary.errors == 0 else "partial",
        message=f"Processed {summary.processed} searches and saved {summary.saved_jobs} jobs",
        metadata=summary.model_dump(),
    )
    return summary
