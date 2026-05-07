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

7. Save secrets and deploy the app.

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

## Storage Modes

The app shows one of these messages at startup:

- `Storage mode: Supabase`
- `Storage mode: temporary local SQLite`

When Supabase secrets are present, the app stores and loads data from Supabase:

- companies
- target job titles
- job results
- search runs
- application status, applied date, and notes
- CV text/profile data

When Supabase secrets are missing, the app falls back to `job_search.db` next to `app.py`. This is useful for local testing, but Streamlit Cloud can rebuild local storage, so Supabase is recommended for permanent history.

## Excel Upload Format

The uploaded spreadsheet should use the first two columns:

| Column A | Column B |
| --- | --- |
| Company name | Target job title |

The app removes blanks, skips header values like `Company` and `Job title`, deduplicates values, saves companies and job titles separately, and searches every company against every target title.

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

Safe mode is enabled by default and uses conservative limits:

- max companies per run: 2
- max job titles per run: 2
- max query variations per company/title: 2
- max results per company/title: 5

The app estimates API calls before running:

```text
companies x job titles x query variations
```

If the estimated calls exceed remaining SerpAPI quota, the run button is blocked until the search size is reduced.

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
