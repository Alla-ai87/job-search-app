# U.S. Job Search Streamlit App

This is a Streamlit Cloud app for searching U.S. jobs from an uploaded Excel list of target companies and job titles.

## Features

- Upload an Excel file where Column A is company names and Column B is target job titles.
- Save target company/title pairs in local SQLite storage.
- Run multiple Google Jobs searches through SerpAPI for each company/title pair.
- Save search results while skipping duplicates.
- Filter results for relevant titles before saving.
- Save a `relevance_score` and `relevance_reason` for each job.
- Show a `Top 10 best matches` section sorted by relevance, salary, and recency.
- Filter the dashboard to show only the top 10 highest relevance jobs per search run.
- Detect sponsorship status as:
  - `sponsorship available`
  - `sponsorship not available`
  - `not mentioned`
- View results in a dashboard table.
- Export filtered results to Excel.
- Keep SerpAPI keys secure with Streamlit secrets.

## Files

- `app.py` - Streamlit app and SQLite logic.
- `requirements.txt` - Python dependencies for Streamlit Cloud.
- `.streamlit/secrets.toml.example` - Example secrets file.

## Deploy On Streamlit Cloud

1. Create a GitHub repository and add these files.
2. Go to [Streamlit Community Cloud](https://streamlit.io/cloud).
3. Create a new app from your GitHub repository.
4. Set the main file path to `app.py`.
5. In the Streamlit Cloud app settings, open **Secrets**.
6. Add your SerpAPI key:

```toml
SERPAPI_API_KEY = "your_serpapi_key_here"
```

7. Save secrets and deploy the app.

## Excel Upload Format

The uploaded spreadsheet should use the first two columns:

| Column A | Column B |
| --- | --- |
| Company name | Target job title |

Rows with blank companies or blank job titles are ignored. Duplicate company/title pairs are skipped.

## Local Storage

The app uses SQLite and creates `job_search.db` next to `app.py`.

On Streamlit Cloud, this storage is simple app-local storage. It is useful for lightweight workflows, but it may reset if the app environment is rebuilt. For long-term production storage, replace SQLite with a hosted database.

## Security

The SerpAPI key is read only from `st.secrets["SERPAPI_API_KEY"]`.

The app never prints the API key, stores it in the database, or sends it to the browser.

## Job Relevance Filtering

The app lowercases each job title and applies balanced relevance scoring to new searches only. Existing saved jobs are not deleted automatically.

Strong title matches add 3 points:

- `project controls`
- `program controls`
- `contracts`
- `contract manager`
- `contract management`
- `pmo`
- `risk manager`
- `scheduler`
- `planning manager`

Infrastructure management roles can be included when paired with infrastructure context. The role adds 1 point:

- `project manager`
- `senior project manager`
- `construction manager`
- `controls manager`
- `program manager`

Infrastructure context adds 1 point:

- `construction`
- `rail`
- `transit`
- `metro`
- `infrastructure`
- `water`
- `wastewater`
- `aviation`
- `airport`
- `highway`
- `bridge`
- `tunnel`
- `design-build`

Generic project-manager titles score only when infrastructure context is also present.

These terms reject the job immediately:

- `software`
- `developer`
- `cloud`
- `network`
- `IT`
- `technician`
- `inspector`
- `architect`
- `data center`

Jobs with `relevance_score` below 1 are not saved in new searches. Saved rows include `relevance_reason`, which explains strong matches, infrastructure context, or exclusion reasons.

The dashboard includes a `Top 10 best matches` section sorted by `relevance_score`, salary when available, and posting recency from `posted_at`. It also includes optional filters for `Show only top 10 highest relevance jobs per run` and a `Minimum relevance score` slider with a default of 2 and range from 1 to 5. New searches are saved with a run ID so each run can be ranked separately.

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

## Sponsorship Detection

For new searches, the app checks both job title and description.

Positive phrases mark the job `sponsorship_available`:

- `visa sponsorship`
- `sponsorship available`
- `we sponsor`
- `H1B`
- `relocation support`

Negative phrases mark the job `sponsorship_not_available`:

- `no sponsorship`
- `not eligible for sponsorship`
- `must be authorized to work`
- `no visa support`

If no phrase matches, the job is marked `not_mentioned`.

Saved rows include `sponsorship_reason`, which stores the phrase that triggered the classification. The dashboard can filter to `Show only jobs with possible sponsorship`, and `sponsorship_available` rows are highlighted in green.
