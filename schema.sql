import asyncio

from app.config import get_settings
from app.database import JobsRepository, get_supabase
from app.services.emailer import send_daily_email
from app.services.processor import process_queue_batch
from app.services.queue import enqueue_daily_searches
from app.services.serpapi import SerpApiClient


async def main() -> None:
    settings = get_settings()
    repo = JobsRepository(get_supabase(settings))

    run_id = repo.start_search_run(triggered_by="scheduled")
    queued = enqueue_daily_searches(repo)
    summary = await process_queue_batch(repo, SerpApiClient(settings), settings)
    summary.queued = queued
    await send_daily_email(repo, settings)
    repo.finish_search_run(run_id, queued, summary.processed, summary.saved_jobs, summary.errors)

    print(
        f"Run #{run_id} complete — "
        f"queued {queued} | processed {summary.processed} | "
        f"saved {summary.saved_jobs} | errors {summary.errors}"
    )


if __name__ == "__main__":
    asyncio.run(main())
