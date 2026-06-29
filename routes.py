from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, HttpUrl


class SponsorshipStatus(str, Enum):
    AVAILABLE = "sponsorship available"
    POSSIBLE = "possible"
    NOT_MENTIONED = "not mentioned"
    NOT_AVAILABLE = "sponsorship not available"
    UNKNOWN = "unknown"


class Company(BaseModel):
    id: int
    name: str
    website: str | None = None
    careers_url: str | None = None
    active: bool = True


class TargetJobTitle(BaseModel):
    id: int
    title: str
    active: bool = True


class QueueItem(BaseModel):
    id: int
    company_id: int
    target_job_title_id: int
    status: str
    attempts: int = 0


class JobResult(BaseModel):
    id: int
    company_id: int
    target_job_title_id: int
    title: str
    company_name: str
    location: str | None = None
    job_id: str | None = None
    application_link: str | None = None
    source_url: str | None = None
    sponsorship_status: SponsorshipStatus
    # ATS tracking fields
    applied: bool = False
    interview: bool = False
    rejected: bool = False
    offer: bool = False
    notes: str | None = None
    date_applied: date | None = None
    created_at: datetime
    updated_at: datetime


class DashboardStats(BaseModel):
    active_companies: int
    active_titles: int
    pending_queue: int
    new_jobs_today: int


class RunSummary(BaseModel):
    queued: int = 0
    processed: int = 0
    saved_jobs: int = 0
    errors: int = 0
    details: dict[str, Any] = {}


class EmailSummaryRequest(BaseModel):
    dry_run: bool = False


class ApplicationLink(BaseModel):
    title: str | None = None
    link: HttpUrl | str | None = None


class UpdateJobTracking(BaseModel):
    """Payload for PATCH /jobs/{id}/tracking"""
    applied: bool | None = None
    interview: bool | None = None
    rejected: bool | None = None
    offer: bool | None = None
    notes: str | None = None
    date_applied: date | None = None


class EnqueueRequest(BaseModel):
    """Optional filters for POST /queue/daily"""
    company_ids: list[int] | None = None
    title_ids: list[int] | None = None
