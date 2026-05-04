# U.S. Job Search Streamlit App

This is a Streamlit Cloud app for searching U.S. jobs from an uploaded Excel list of target companies and job titles.

## Features

- Upload an Excel file where Column A is company names and Column B is target job titles.
- Save target company/title pairs in local SQLite storage.
- Run Google Jobs searches through SerpAPI.
- Save search results while skipping duplicates.
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
