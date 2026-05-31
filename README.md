# U.S. Job Search Streamlit App

Streamlit Cloud app for a U.S. infrastructure, construction, PMO, contracts, project controls, planning, scheduler, and risk-focused job search.

## Features

- Upload an Excel file where Column A is companies and Column B is target job titles.
- Store unique companies and unique job titles separately, then search the full `companies x job_titles` combination set.
- Run multiple SerpAPI Google Jobs queries for each company/title combination.
- Save deduplicated results with relevance score, sponsorship status, CV match score, tracker status, notes, and search-run history.
- Review Top 20 best matches sorted by CV match score, relevance, salary, and recency.
- Upload a PDF/DOCX CV for match scoring.
- Update application status, applied date, and notes in the Application Tracker.
- Export filtered results to Excel.
- Use Supabase PostgreSQL for persistent production storage, with local SQLite fallback when Supabase secrets are missing.

## Files

- `app.py` - Streamlit app, search logic, storage logic, CV matching, and tracker.
- `requirements.txt` - Python dependencies for Streamlit Cloud.
- `supabase_schema.sql` - Supabase PostgreSQL schema.
- `.streamlit/secrets.toml.example` - Example secrets file. Do not commit real secrets.

## Deploy On Streamlit Cloud

1. Create a GitHub repository and add these files.
2. Go to [Streamlit Community Cloud](https://streamlit.io/cloud).
3. Create a new app from your GitHub repository.
4. Set the main file path to `app.py`.
5. In the Streamlit Cloud app settings, open **Secrets**.
6. Add your secrets:

```toml
SERPAPI_API_KEY = "your_serpapi_key_here"

SUPABASE_URL = "https://your-project-ref.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "your_supabase_service_role_key_here"
```

7. Save secrets and reboot the app.

## Supabase Setup

1. Create a Supabase project at [supabase.com](https://supabase.com).
2. Open the project dashboard.
3. Go to **SQL Editor**.
4. Paste and run the full contents of `supabase_schema.sql`.
5. Go to **Project Settings > API**.
6. Copy:
   - Project URL into `SUPABASE_URL`
   - Service role key into `SUPABASE_SERVICE_ROLE_KEY`
7. Add both values to Streamlit Cloud **Secrets**.

The service role key must stay server-side only. The app reads it from `st.secrets` and uses it only in backend requests from Streamlit.

Required tables in `supabase_schema.sql`:

- `companies`
- `target_job_titles`
- `job_results`
- `search_runs`
- `application_tracker`
- `notes`
- `saved_profiles`
- `cv_profiles`

## Storage Modes

The app shows one of these messages at startup:

- `Connected to Supabase`
- `Running in temporary local mode`

When Supabase secrets are present and all required tables are accessible, the app stores and loads data from Supabase:

- companies
- target job titles
- job results
- search runs
- application status, applied date, and notes
- CV text/profile data
- saved search profiles

When Supabase secrets or tables are missing, the app falls back to `job_search.db` next to `app.py` and shows exact setup instructions. This is useful for local testing, but Streamlit Cloud can rebuild local storage, so Supabase is required for permanent history.

## Historical Results

On startup, the app automatically loads previously saved jobs and search runs from the active storage layer. This does not call SerpAPI and works even when quota is exhausted.

The top of the app shows `Last saved search run` with:

- `run_id`
- `run_started_at`
- jobs saved
- companies searched

Use the historical controls to reopen old results:

- `Today`
- `Last 7 days`
- `All history`
- `By run_id`

The `Load previous run` dropdown filters Dashboard, Top Matches, and Application Tracker to a saved run without running a new search. New searches append new run IDs and do not overwrite older results.

## Emergency Local Recovery

If the app previously ran in temporary local SQLite mode, open the `Emergency Local Recovery` panel at the top of the app.

The recovery tool searches the app folder for:

- `job_search.db`
- any other `*.db` file

If a local database still exists, click `Recover local data` to load:

- companies
- target job titles
- job results
- search runs
- application tracker rows
- notes

The app shows recovered counts and provides `Export recovered data to Excel`. After Supabase is connected, use `Migrate recovered data to Supabase` to copy recovered local records into the persistent Supabase tables.

If the local file was removed by Streamlit reboot, the app shows: `No recoverable local data found. Streamlit temporary storage was reset.`

## Excel Upload Format

The uploaded spreadsheet should use the first two columns:

| Column A | Column B |
| --- | --- |
| Company name | Target job title |

The app removes blanks, skips header values like `Company` and `Job title`, deduplicates values, saves companies and job titles separately, and searches every company against every target title.

Upload mode defaults to `Replace existing targets`:

- `Replace existing targets` clears only the saved company and job-title target lists, then imports the unique companies from Column A and unique job titles from Column B.
- `Append to existing targets` keeps existing target lists and adds any new unique companies or job titles from the upload.

Neither mode deletes historical job results, saved search runs, application tracker data, or notes. After upload, the app shows companies imported, job titles imported, and total search combinations. Full Search settings are refreshed from the current saved target counts.

Use `Clear all targets` with confirmation to empty only the company and job-title target lists.

## Search Coverage

For each company/title pair, the app runs these SerpAPI Google Jobs query variations:

- `{job_title} {company} jobs United States`
- `{job_title} {company} careers`
- `{job_title} {company} LinkedIn jobs`
- `{job_title} {company} infrastructure jobs`
- `{job_title} {company} construction jobs`
- `{job_title} {company} rail transit jobs`

The sidebar includes:

- `Max jobs per company/title`, default 10, range 3-20.
- `Include broader infrastructure management jobs`, default on.

Results are deduplicated by job ID, application link, and title/employer/location before relevance filtering.

## SerpAPI Quota Protection

The `Run Search` tab includes an `API Usage / Quota` section. The app checks SerpAPI account usage from the Account API and shows:

- monthly searches used
- monthly search limit
- searches remaining

Quota is checked only when you click `Check SerpAPI quota` or `Run Job Search`, so opening historical results does not consume SerpAPI calls.

Full Search is enabled by default:

- max companies per run: all available companies
- max job titles per run: all available job titles
- max query variations per company/title: 4
- max results per company/title: 20

Safe Mode remains available as an optional fallback, but the app never switches to it automatically.

The app estimates API calls before running:

```text
companies x job titles x query variations
```

If the estimated calls exceed remaining SerpAPI quota, the run button is blocked until the search size is reduced.

## Search Profiles

The `Run Search` tab includes three search profiles. `Full Search` is the default on app load.

- `Full Search`: all available companies, all available job titles, 4 query variations per company/title, and 20 results per company/title.
- `Balanced Mode`: moderate coverage.
- `Safe Mode`: conservative fallback for low API usage.

Use `Save current settings as Full Search` to persist the current controls as your Full Search profile. Use `Load Full Search settings` to reload it later. Use `Restore My Full Search Defaults` to return to all companies, all job titles, 4 query variations, and 20 results per company/title. Loading or restoring a profile does not run a search.

The app displays estimated API calls before running. If quota is low, it shows a warning but never silently switches to Safe Mode or reduces search scope. The app still checks SerpAPI quota before running and blocks the run when estimated API calls exceed remaining quota.

## Manual Search Selection

Before running a search, use `Multi-select Companies` and `Multi-select Job Titles` in the `Run Search` tab.

- Nothing selected means search all saved companies or all saved job titles.
- Selecting companies searches only those companies.
- Selecting job titles searches only those titles.

The app updates these counters live:

- companies this run
- job titles this run
- company/title combinations
- estimated API calls

Selections are saved in `saved_profiles` as `Last Search Selection`. Use `Save Priority Companies` / `Load Priority Companies` and `Save Priority Roles` / `Load Priority Roles` for reusable presets. `Select All` and `Clear All` provide quick selection controls.

## Job Relevance Filtering

New searches save jobs with `relevance_score >= 1`. Strong controls, contracts, PMO, planning, scheduler, and risk matches score highest. Broader infrastructure management titles are allowed when paired with construction, rail, transit, metro, infrastructure, water, wastewater, aviation, airport, highway, bridge, tunnel, or design-build context.

Clearly irrelevant terms such as software, developer, cloud, network, IT, technician, inspector, architect, and data center are rejected before saving.

## Sponsorship Detection

For new searches, the app checks job title, description, and application text when available.

Positive phrases mark the job `sponsorship available`; negative phrases mark it `sponsorship not available`; authorization phrases mark it `requires work authorization`; otherwise it is `not mentioned`.

## CV Matching

Upload a PDF or DOCX CV in `Settings`, then run a new search. New saved jobs receive:

- `cv_match_score`, from 0 to 100.
- `cv_match_reason`, showing matched senior infrastructure PMO, controls, contracts, planning, scheduling, risk, industry, and seniority terms.

## Application Tracker

Use the `Application Tracker` tab to update:

- `application_status`: `New`, `Interested`, `Applied`, `Interview`, `Offer`, `Rejected`, or `Archived`
- `applied_date`
- `application_notes`

Updates are persisted in Supabase when configured, or SQLite fallback otherwise.

## Export

The dashboard export includes user-facing fields by default. `searched_job_title`, `run_id`, and `run_started_at` are included only when the related export checkboxes are enabled. `Job ID` is included as the first column.

## Security

API keys and Supabase credentials are read only from Streamlit secrets. They are never stored in the database, printed, or exposed in the frontend.
