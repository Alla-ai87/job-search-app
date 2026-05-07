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
    run_id text not null default 'legacy',
    run_started_at timestamptz,
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
    created_at timestamptz not null default now()
);

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

create index if not exists idx_job_results_company on job_results(company);
create index if not exists idx_job_results_run_started_at on job_results(run_started_at desc);
create index if not exists idx_job_results_application_status on job_results(application_status);
create index if not exists idx_application_tracker_status on application_tracker(application_status);
