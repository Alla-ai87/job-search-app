import argparse
import asyncio

from app.config import get_settings
from app.services.serpapi import SerpApiClient, extract_application_link


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test the SerpAPI Google Jobs connection.")
    parser.add_argument("--company", default="Microsoft", help="Company name to search")
    parser.add_argument("--title", default="Contract Manager", help="Job title to search")
    args = parser.parse_args()

    settings = get_settings()
    client = SerpApiClient(settings)
    jobs = await client.search_jobs(args.company, args.title)

    print(f"SerpAPI connection OK. Found {len(jobs)} result(s).")
    for job in jobs[:3]:
        print("- " + (job.get("title") or "Untitled role"))
        print("  Company: " + (job.get("company_name") or "Unknown"))
        print("  Location: " + (job.get("location") or "Unspecified"))
        print("  Apply: " + (extract_application_link(job) or "No application link returned"))


if __name__ == "__main__":
    asyncio.run(main())
