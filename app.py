import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "job_search.db"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"


SPONSORSHIP_SIGNAL_TERMS = (
    "visa sponsorship",
    "work authorization",
    "h1b",
    "h-1b",
    "relocation support",
)

SPONSORSHIP_AVAILABLE_PATTERNS = (
    r"\bsponsorship\s+(is\s+)?provided\b",
    r"\bvisa\s+sponsorship\s+(is\s+)?provided\b",
)

SPONSORSHIP_NOT_AVAILABLE_PATTERNS = (
    r"\bno\s+(visa\s+)?sponsorship\b",
    "sponsorship is not available",
    r"\bsponsorship\s+(is\s+)?not\s+available\b",
    r"\bwithout\s+(visa\s+)?sponsorship\b",
    r"\b(unable|not\s+able)\s+to\s+sponsor\b",
    r"\bwill\s+not\s+sponsor\b",
    r"\b(do|does)\s+not\s+sponsor\b",
    r"\bcannot\s+sponsor\b",
)

RELEVANT_TITLE_TERMS = (
    "Contract",
    "PMO",
    "Project Controls",
    "Program Controls",
    "Risk",
    "Planning",
    "Scheduler",
)

EXCLUDED_TITLE_TERMS = (
    "Engineer",
    "Developer",
    "Architect",
    "IT",
    "Software",
    "Network",
)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                job_title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(company, job_title)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_key TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL DEFAULT 'legacy',
                run_started_at TEXT,
                company TEXT NOT NULL,
                searched_job_title TEXT NOT NULL,
                title TEXT,
                employer_name TEXT,
                location TEXT,
                via TEXT,
                posted_at TEXT,
                schedule_type TEXT,
                salary TEXT,
                description TEXT,
                job_url TEXT,
                apply_link TEXT,
                sponsorship_status TEXT NOT NULL,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(conn, "job_results", "relevance_score", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "job_results", "run_id", "TEXT NOT NULL DEFAULT 'legacy'")
        ensure_column(conn, "job_results", "run_started_at", "TEXT")


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def load_targets_from_excel(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file, usecols=[0, 1], header=None, engine="openpyxl")
    df.columns = ["company", "job_title"]
    df["company"] = df["company"].map(clean_text)
    df["job_title"] = df["job_title"].map(clean_text)
    df = df[(df["company"] != "") & (df["job_title"] != "")]
    return df.drop_duplicates(subset=["company", "job_title"]).reset_index(drop=True)


def save_targets(df: pd.DataFrame) -> int:
    created_at = utc_now()
    inserted = 0
    with get_connection() as conn:
        for row in df.itertuples(index=False):
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO targets (company, job_title, created_at)
                VALUES (?, ?, ?)
                """,
                (row.company, row.job_title, created_at),
            )
            inserted += cursor.rowcount
    return inserted


def get_targets() -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query(
            "SELECT company, job_title, created_at FROM targets ORDER BY company, job_title",
            conn,
        )


def get_results() -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                run_id,
                run_started_at,
                company,
                searched_job_title,
                title,
                employer_name,
                location,
                via,
                posted_at,
                schedule_type,
                salary,
                sponsorship_status,
                relevance_score,
                job_url,
                apply_link,
                description,
                created_at
            FROM job_results
            ORDER BY COALESCE(run_started_at, created_at) DESC, relevance_score DESC, company, searched_job_title
            """,
            conn,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"run-{timestamp}-{uuid.uuid4().hex[:8]}"


def get_serpapi_key() -> str:
    try:
        return st.secrets["SERPAPI_API_KEY"]
    except Exception:
        return ""


def detect_sponsorship_status(description: object) -> str:
    text = clean_text(description).lower()
    if not any(term in text for term in SPONSORSHIP_SIGNAL_TERMS):
        return "not mentioned"
    if any(re.search(pattern, text) for pattern in SPONSORSHIP_NOT_AVAILABLE_PATTERNS):
        return "sponsorship not available"
    if any(re.search(pattern, text) for pattern in SPONSORSHIP_AVAILABLE_PATTERNS):
        return "sponsorship available"
    return "not mentioned"


def contains_term(text: str, term: str) -> bool:
    escaped_words = [re.escape(word) for word in term.lower().split()]
    pattern = r"\b" + r"\s+".join(escaped_words) + r"\b"
    return re.search(pattern, text.lower()) is not None


def calculate_relevance_score(title: str) -> int:
    title = clean_text(title)
    if not title:
        return 0
    if any(contains_term(title, term) for term in EXCLUDED_TITLE_TERMS):
        return 0
    matched_terms = [term for term in RELEVANT_TITLE_TERMS if contains_term(title, term)]
    return len(matched_terms)


def best_apply_link(job: dict) -> str:
    apply_options = job.get("apply_options") or []
    if apply_options:
        return apply_options[0].get("link", "") or ""
    return job.get("related_links", [{}])[0].get("link", "") if job.get("related_links") else ""


def normalize_job_url(job: dict) -> str:
    for key in ("job_id", "share_link", "link"):
        value = job.get(key)
        if value:
            return str(value)
    return best_apply_link(job)


def get_salary(job: dict) -> str:
    detected = job.get("detected_extensions") or {}
    salary = detected.get("salary")
    if salary:
        return str(salary)
    extensions = job.get("extensions") or []
    salary_bits = [item for item in extensions if "$" in str(item)]
    return ", ".join(salary_bits)


def make_result_key(company: str, searched_job_title: str, job: dict) -> str:
    identity = "|".join(
        [
            clean_text(company).lower(),
            clean_text(searched_job_title).lower(),
            clean_text(job.get("title")).lower(),
            clean_text(job.get("company_name") or job.get("employer_name")).lower(),
            clean_text(job.get("location")).lower(),
            clean_text(normalize_job_url(job)).lower(),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def search_google_jobs(api_key: str, company: str, job_title: str) -> list[dict]:
    params = {
        "engine": "google_jobs",
        "q": f'{job_title} "{company}"',
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    response = requests.get(SERPAPI_ENDPOINT, params=params, timeout=45)
    response.raise_for_status()
    payload = response.json()
    return payload.get("jobs_results", [])


def save_job_results(
    run_id: str,
    run_started_at: str,
    company: str,
    searched_job_title: str,
    jobs: list[dict],
    relevance_threshold: int,
) -> tuple[int, int]:
    created_at = utc_now()
    inserted = 0
    skipped = 0
    with get_connection() as conn:
        for job in jobs:
            title = clean_text(job.get("title"))
            relevance_score = calculate_relevance_score(title)
            if relevance_score < relevance_threshold:
                skipped += 1
                continue

            employer_name = clean_text(job.get("company_name") or job.get("employer_name"))
            location = clean_text(job.get("location"))
            description = clean_text(job.get("description"))
            apply_link = best_apply_link(job)
            job_url = normalize_job_url(job)
            sponsorship_status = detect_sponsorship_status(description)

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO job_results (
                    result_key,
                    run_id,
                    run_started_at,
                    company,
                    searched_job_title,
                    title,
                    employer_name,
                    location,
                    via,
                    posted_at,
                    schedule_type,
                    salary,
                    description,
                    job_url,
                    apply_link,
                    sponsorship_status,
                    relevance_score,
                    raw_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    make_result_key(company, searched_job_title, job),
                    run_id,
                    run_started_at,
                    company,
                    searched_job_title,
                    title,
                    employer_name,
                    location,
                    clean_text(job.get("via")),
                    clean_text(job.get("detected_extensions", {}).get("posted_at") or job.get("posted_at")),
                    clean_text(job.get("detected_extensions", {}).get("schedule_type")),
                    get_salary(job),
                    description,
                    job_url,
                    apply_link,
                    sponsorship_status,
                    relevance_score,
                    json.dumps(job),
                    created_at,
                ),
            )
            inserted += cursor.rowcount
    return inserted, skipped


def top_jobs_per_run(df: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    if df.empty:
        return df
    sorted_df = df.sort_values(
        by=["run_started_at", "created_at", "relevance_score"],
        ascending=[False, False, False],
        na_position="last",
    )
    return sorted_df.groupby("run_id", dropna=False, group_keys=False).head(limit)


def dataframe_to_excel_bytes(df: pd.DataFrame) -> bytes:
    from io import BytesIO

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="job_results")
    return output.getvalue()


def render_upload_section() -> None:
    st.subheader("Upload targets")
    uploaded_file = st.file_uploader(
        "Excel file with companies in Column A and target job titles in Column B",
        type=["xlsx"],
    )

    if uploaded_file is None:
        return

    try:
        df = load_targets_from_excel(uploaded_file)
    except Exception as exc:
        st.error(f"Could not read Excel file: {exc}")
        return

    if df.empty:
        st.warning("No valid company/job title rows were found.")
        return

    st.dataframe(df, use_container_width=True, hide_index=True)
    if st.button("Save Uploaded Targets", type="primary"):
        inserted = save_targets(df)
        st.success(f"Saved {inserted} new target rows. Existing duplicates were skipped.")
        st.rerun()


def render_search_section(targets: pd.DataFrame) -> None:
    st.subheader("Run search")
    api_key = get_serpapi_key()
    if not api_key:
        st.warning("Add SERPAPI_API_KEY to Streamlit secrets before running searches.")

    max_targets = st.number_input(
        "Maximum company/title pairs to search this run",
        min_value=1,
        max_value=max(1, len(targets)),
        value=min(10, max(1, len(targets))),
        step=1,
    )
    relevance_threshold = st.number_input(
        "Minimum relevance score to save",
        min_value=1,
        max_value=len(RELEVANT_TITLE_TERMS),
        value=1,
        step=1,
        help="A job gets 1 point for each allowed term found in the job title. Excluded terms always score 0.",
    )

    disabled = targets.empty or not api_key
    if st.button("Run Job Search", type="primary", disabled=disabled):
        selected_targets = targets.head(int(max_targets))
        run_id = make_run_id()
        run_started_at = utc_now()
        progress = st.progress(0)
        status = st.empty()
        total_inserted = 0
        total_skipped = 0

        for index, row in enumerate(selected_targets.itertuples(index=False), start=1):
            status.write(f"Searching {row.job_title} at {row.company}...")
            try:
                jobs = search_google_jobs(api_key, row.company, row.job_title)
                inserted, skipped = save_job_results(
                    run_id,
                    run_started_at,
                    row.company,
                    row.job_title,
                    jobs,
                    int(relevance_threshold),
                )
                total_inserted += inserted
                total_skipped += skipped
            except requests.HTTPError as exc:
                st.error(f"SerpAPI error for {row.company} / {row.job_title}: {exc}")
            except requests.RequestException as exc:
                st.error(f"Network error for {row.company} / {row.job_title}: {exc}")
            progress.progress(index / len(selected_targets))

        status.write("Search complete.")
        st.success(
            f"Saved {total_inserted} new job results. "
            f"Skipped {total_skipped} low-relevance results. Duplicates were skipped."
        )
        st.rerun()


def render_dashboard(results: pd.DataFrame) -> None:
    st.subheader("Dashboard")
    if results.empty:
        st.info("No job results saved yet.")
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("Saved jobs", len(results))
    col2.metric("Companies", results["company"].nunique())
    col3.metric("Sponsorship available", (results["sponsorship_status"] == "sponsorship available").sum())
    st.caption(
        "Saved jobs must include at least one allowed title term and no excluded title terms. "
        "Relevance score is the count of allowed terms matched in the title."
    )

    sponsorship_filter = st.multiselect(
        "Sponsorship status",
        options=[
            "sponsorship available",
            "sponsorship not available",
            "not mentioned",
        ],
        default=[
            "sponsorship available",
            "sponsorship not available",
            "not mentioned",
        ],
    )
    company_filter = st.multiselect(
        "Company",
        options=sorted(results["company"].dropna().unique()),
    )
    top_10_per_run = st.checkbox(
        "Show only top 10 highest relevance jobs per run",
        value=False,
    )

    filtered = results[results["sponsorship_status"].isin(sponsorship_filter)]
    if company_filter:
        filtered = filtered[filtered["company"].isin(company_filter)]
    if top_10_per_run:
        filtered = top_jobs_per_run(filtered, limit=10)

    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
        column_config={
            "job_url": st.column_config.LinkColumn("Job URL"),
            "apply_link": st.column_config.LinkColumn("Apply link"),
            "description": st.column_config.TextColumn("Description", width="large"),
        },
    )

    st.download_button(
        "Export Results to Excel",
        data=dataframe_to_excel_bytes(filtered),
        file_name=f"job_search_results_{datetime.now().date().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main() -> None:
    st.set_page_config(page_title="U.S. Job Search", layout="wide")
    init_db()

    st.title("U.S. Job Search")
    st.caption("Upload target companies and roles, search Google Jobs through SerpAPI, and export deduplicated results.")

    targets = get_targets()
    results = get_results()

    render_upload_section()

    st.divider()
    st.subheader("Saved targets")
    if targets.empty:
        st.info("Upload an Excel file to add company/title pairs.")
    else:
        st.dataframe(targets, use_container_width=True, hide_index=True)

    st.divider()
    render_search_section(targets)

    st.divider()
    render_dashboard(results)


if __name__ == "__main__":
    main()
