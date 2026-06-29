from app.database import JobsRepository


def enqueue_daily_searches(
    repo: JobsRepository,
    company_ids: list[int] | None = None,
    title_ids: list[int] | None = None,
) -> int:
    """
    Create pending queue items for every active company × title combination.

    Pass ``company_ids`` and/or ``title_ids`` to restrict the run to a subset.
    This reduces SerpAPI quota usage when you only want to search specific combos.
    """
    companies = repo.list_active_companies()
    titles = repo.list_active_titles()

    # Apply optional filters
    if company_ids:
        companies = [c for c in companies if c["id"] in company_ids]
    if title_ids:
        titles = [t for t in titles if t["id"] in title_ids]

    queued = 0
    for company in companies:
        for title in titles:
            if repo.create_queue_item(company["id"], title["id"]):
                queued += 1

    repo.create_log(
        event="enqueue_daily_searches",
        status="success",
        message=f"Queued {queued} searches",
        metadata={
            "companies": len(companies),
            "titles": len(titles),
            "company_filter": company_ids,
            "title_filter": title_ids,
        },
    )
    return queued
