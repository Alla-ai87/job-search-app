create extension if not exists pgcrypto;

create table if not exists companies (
    id bigserial primary key,
    company text not null unique,
    created_at timestamptz not null default now()
);

create table if not exists target_job_titles (
    id bigserial primary key,
    job_title text not null unique,
    created_at timestamptz not null default now()
);

create table if not exists job_results (
    id bigserial primary key,
    result_key text not null unique,
    job_fingerprint text,
    run_id text not null default 'legacy',
    run_started_at timestamptz,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    company text not null,
    searched_job_title text not null,
    title text,
    employer_name text,
    location text,
    via text,
    posted_at text,
    schedule_type text,
    salary text,
    description text,
    job_url text,
    apply_link text,
    sponsorship_status text not null default 'not mentioned',
    sponsorship_reason text,
    relevance_score integer not null default 0,
    relevance_reason text,
    cv_match_score integer not null default 0,
    cv_match_reason text,
    status text not null default 'New',
    notes text default '',
    application_status text not null default 'New',
    applied_date timestamptz,
    application_notes text default '',
    raw_json jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz,
    merged_into_id bigint references job_results(id)
);

alter table job_results add column if not exists job_fingerprint text;
alter table job_results add column if not exists first_seen_at timestamptz;
alter table job_results add column if not exists last_seen_at timestamptz;
alter table job_results add column if not exists merged_into_id bigint references job_results(id);
alter table job_results add column if not exists updated_at timestamptz;

create table if not exists search_runs (
    run_id text primary key,
    run_started_at timestamptz not null,
    raw_jobs_found integer not null default 0,
    duplicates_skipped integer not null default 0,
    excluded_by_relevance integer not null default 0,
    jobs_saved integer not null default 0
);

create table if not exists cv_profiles (
    profile_key text primary key default 'default',
    cv_text text,
    cv_summary text,
    updated_at timestamptz not null default now()
);

create table if not exists application_tracker (
    job_id text primary key,
    job_result_id bigint references job_results(id) on delete cascade,
    application_status text not null default 'New',
    applied_date timestamptz,
    application_notes text default '',
    updated_at timestamptz not null default now()
);

create table if not exists notes (
    job_id text primary key,
    job_result_id bigint references job_results(id) on delete cascade,
    note_text text default '',
    updated_at timestamptz not null default now()
);

create table if not exists saved_profiles (
    profile_name text primary key,
    settings_json jsonb not null,
    updated_at timestamptz not null default now()
);

update job_results
set
    job_fingerprint = encode(
        digest(
            lower(trim(coalesce(employer_name, ''))) || '|' ||
            lower(trim(coalesce(title, ''))) || '|' ||
            lower(trim(coalesce(location, ''))) || '|' ||
            lower(trim(coalesce(apply_link, ''))) || '|' ||
            lower(trim(coalesce(job_url, ''))),
            'sha256'
        ),
        'hex'
    ),
    first_seen_at = coalesce(first_seen_at, created_at, run_started_at, now()),
    last_seen_at = coalesce(last_seen_at, run_started_at, created_at, now())
where job_fingerprint is null or first_seen_at is null or last_seen_at is null;

with ranked as (
    select
        id,
        first_value(id) over (
            partition by job_fingerprint
            order by first_seen_at nulls last, created_at nulls last, id
        ) as keeper_id,
        row_number() over (
            partition by job_fingerprint
            order by first_seen_at nulls last, created_at nulls last, id
        ) as row_number_in_group
    from job_results
    where job_fingerprint is not null and merged_into_id is null
),
merged as (
    select
        r.keeper_id,
        min(j.first_seen_at) as first_seen_at,
        max(j.last_seen_at) as last_seen_at,
        max(j.relevance_score) as relevance_score,
        max(j.cv_match_score) as cv_match_score,
        max(j.applied_date) as applied_date,
        max(nullif(j.application_notes, '')) as application_notes,
        max(nullif(j.notes, '')) as notes,
        (array_agg(j.application_status order by
            case j.application_status
                when 'Offer' then 7
                when 'Interview' then 6
                when 'Applied' then 5
                when 'Interested' then 4
                when 'Rejected' then 3
                when 'Closed' then 2
                when 'Archived' then 1
                else 0
            end desc
        ))[1] as application_status
    from ranked r
    join job_results j on j.id = r.id
    group by r.keeper_id
)
update job_results keeper
set
    first_seen_at = coalesce(merged.first_seen_at, keeper.first_seen_at),
    last_seen_at = coalesce(merged.last_seen_at, keeper.last_seen_at),
    relevance_score = greatest(keeper.relevance_score, merged.relevance_score),
    cv_match_score = greatest(keeper.cv_match_score, merged.cv_match_score),
    applied_date = coalesce(keeper.applied_date, merged.applied_date),
    application_status = coalesce(nullif(merged.application_status, ''), keeper.application_status),
    status = coalesce(nullif(merged.application_status, ''), keeper.status),
    application_notes = coalesce(nullif(keeper.application_notes, ''), merged.application_notes, keeper.application_notes),
    notes = coalesce(nullif(keeper.notes, ''), merged.notes, keeper.notes)
from merged
where keeper.id = merged.keeper_id;

with ranked as (
    select
        id,
        first_value(id) over (
            partition by job_fingerprint
            order by first_seen_at nulls last, created_at nulls last, id
        ) as keeper_id,
        row_number() over (
            partition by job_fingerprint
            order by first_seen_at nulls last, created_at nulls last, id
        ) as row_number_in_group
    from job_results
    where job_fingerprint is not null and merged_into_id is null
)
update job_results duplicate
set merged_into_id = ranked.keeper_id
from ranked
where duplicate.id = ranked.id and ranked.row_number_in_group > 1;

create index if not exists idx_job_results_company on job_results(company);
create index if not exists idx_job_results_run_started_at on job_results(run_started_at desc);
create index if not exists idx_job_results_application_status on job_results(application_status);
create unique index if not exists idx_job_results_job_fingerprint_active
    on job_results(job_fingerprint)
    where merged_into_id is null;
create index if not exists idx_job_results_last_seen_at on job_results(last_seen_at desc);
create index if not exists idx_application_tracker_status on application_tracker(application_status);
