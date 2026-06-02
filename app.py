import hashlib
from io import BytesIO
import json
import os
import re
import smtplib
import sqlite3
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "job_search.db"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_ENDPOINT = "https://serpapi.com/account.json"


SPONSORSHIP_POSITIVE_PHRASES = (
    "visa sponsorship is available",
    "sponsorship is available",
    "will sponsor",
    "we sponsor",
    "employer sponsorship",
    "h-1b sponsorship available",
    "work visa sponsorship available",
)

SPONSORSHIP_AUTHORIZATION_PHRASES = (
    "must be authorized to work",
    "must have work authorization",
    "must be legally authorized to work",
)

SPONSORSHIP_NEGATIVE_PHRASES = (
    "no sponsorship",
    "will not sponsor",
    "does not sponsor",
    "not eligible for sponsorship",
    "sponsorship is not available",
    "without sponsorship",
    "cannot sponsor",
    "no visa support",
)

SPONSORSHIP_NOT_AVAILABLE_PHRASES = (
    *SPONSORSHIP_NEGATIVE_PHRASES,
    "must be authorized to work",
    "must have work authorization",
    "must be legally authorized to work",
)

STRONG_RELEVANCE_TERMS = (
    "project controls",
    "program controls",
    "contracts",
    "contract manager",
    "contract management",
    "pmo",
    "risk manager",
    "scheduler",
    "planning manager",
)

INFRASTRUCTURE_MANAGEMENT_TERMS = (
    "project manager",
    "senior project manager",
    "construction manager",
    "controls manager",
    "program manager",
)

INFRASTRUCTURE_CONTEXT_TERMS = (
    "construction",
    "rail",
    "transit",
    "metro",
    "infrastructure",
    "water",
    "wastewater",
    "aviation",
    "airport",
    "highway",
    "bridge",
    "tunnel",
    "design-build",
)

EXCLUDED_RELEVANCE_TERMS = (
    "software",
    "developer",
    "cloud",
    "network",
    "it",
    "technician",
    "inspector",
    "architect",
    "data center",
)

SAVE_RELEVANCE_THRESHOLD = 1

APPLICATION_STATUSES = ("New", "Interested", "Applied", "Interview", "Offer", "Rejected", "Closed", "Archived")
ACTIVE_APPLICATION_STATUSES = ("New", "Interested", "Applied", "Interview", "Offer")
COMPANY_HEADER_VALUES = {"company", "companies"}
JOB_TITLE_HEADER_VALUES = {"job title", "job titles", "target job title", "target job titles"}

RESULT_COLUMNS = [
    "id",
    "job_fingerprint",
    "run_id",
    "run_started_at",
    "first_seen_at",
    "last_seen_at",
    "company",
    "searched_job_title",
    "title",
    "employer_name",
    "location",
    "via",
    "posted_at",
    "schedule_type",
    "salary",
    "sponsorship_status",
    "sponsorship_reason",
    "matched_sponsorship_phrase",
    "relevance_score",
    "relevance_reason",
    "cv_match_score",
    "cv_match_reason",
    "status",
    "notes",
    "application_status",
    "applied_date",
    "application_notes",
    "job_url",
    "apply_link",
    "description",
    "created_at",
    "updated_at",
    "merged_into_id",
]

REQUIRED_SUPABASE_TABLES = (
    "companies",
    "target_job_titles",
    "job_results",
    "search_runs",
    "application_tracker",
    "notes",
    "saved_profiles",
)

RECOVERY_TABLES = (
    "companies",
    "target_job_titles",
    "job_results",
    "search_runs",
    "application_tracker",
    "notes",
    "saved_profiles",
    "cv_profiles",
)

SUPABASE_SECRETS_EXAMPLE = """SERPAPI_API_KEY = "your_serpapi_key_here"
SUPABASE_URL = "https://your-project-ref.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "your_supabase_service_role_key_here"
"""


def get_supabase_credentials() -> tuple[str, str]:
    try:
        url = clean_text(st.secrets["SUPABASE_URL"]).rstrip("/")
        key = clean_text(st.secrets["SUPABASE_SERVICE_ROLE_KEY"])
    except Exception:
        return "", ""
    return url, key


def is_supabase_configured() -> bool:
    url, key = get_supabase_credentials()
    return bool(url and key)


def storage_mode_label() -> str:
    return "Supabase" if is_supabase_connected() else "temporary local SQLite"


def is_supabase_connected() -> bool:
    return bool(st.session_state.get("supabase_connected", False))


def is_streamlit_cloud_environment() -> bool:
    cwd = Path.cwd().as_posix()
    return (
        cwd.startswith("/mount/src")
        or os.environ.get("STREAMLIT_CLOUD", "").lower() == "true"
        or os.environ.get("STREAMLIT_SHARING_MODE", "") != ""
        or os.environ.get("HOME", "") == "/home/adminuser"
    )


def is_local_sqlite_allowed() -> bool:
    return not is_streamlit_cloud_environment()


def supabase_headers(prefer: str = "") -> dict[str, str]:
    _, key = get_supabase_credentials()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_table_url(table_name: str) -> str:
    url, _ = get_supabase_credentials()
    return f"{url}/rest/v1/{table_name}"


def supabase_request(
    method: str,
    table_name: str,
    params: dict[str, object] | None = None,
    json_body: object | None = None,
    prefer: str = "",
) -> object:
    response = requests.request(
        method,
        supabase_table_url(table_name),
        headers=supabase_headers(prefer),
        params=params,
        json=json_body,
        timeout=45,
    )
    response.raise_for_status()
    if not response.content:
        return []
    return response.json()


def check_supabase_table(table_name: str) -> tuple[bool, str]:
    try:
        select_columns = "*"
        if table_name == "job_results":
            select_columns = ",".join(RESULT_COLUMNS)
        supabase_request("GET", table_name, params={"select": select_columns, "limit": 1})
    except requests.HTTPError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, str(exc)
    return True, ""


@st.cache_data(ttl=120, show_spinner=False)
def get_supabase_setup_status(url: str, key: str) -> dict:
    if not url or not key:
        missing = []
        if not url:
            missing.append("SUPABASE_URL")
        if not key:
            missing.append("SUPABASE_SERVICE_ROLE_KEY")
        return {"connected": False, "missing_secrets": missing, "missing_tables": [], "errors": {}}

    missing_tables = []
    errors = {}
    for table_name in REQUIRED_SUPABASE_TABLES:
        ok, error = check_supabase_table(table_name)
        if not ok:
            missing_tables.append(table_name)
            errors[table_name] = error
    return {
        "connected": not missing_tables,
        "missing_secrets": [],
        "missing_tables": missing_tables,
        "errors": errors,
    }


def render_supabase_setup_instructions(status: dict) -> None:
    missing_secrets = status.get("missing_secrets") or []
    missing_tables = status.get("missing_tables") or []
    if missing_secrets:
        st.error("Running in temporary local mode. Missing Supabase secrets: " + ", ".join(missing_secrets))
    elif missing_tables:
        st.error("Running in temporary local mode. Supabase is configured, but required tables are missing or inaccessible.")
        st.caption("Missing/inaccessible tables: " + ", ".join(missing_tables))
    else:
        st.warning("Running in temporary local mode.")

    st.markdown("#### Connect Supabase in Streamlit Cloud")
    st.markdown(
        """
1. Create a Supabase project.
2. Open Supabase **SQL Editor**.
3. Run the full `supabase_schema.sql` file from this repository.
4. In Streamlit Cloud, open the app settings and go to **Secrets**.
5. Add the secrets below, then reboot the app.
        """
    )
    st.code(SUPABASE_SECRETS_EXAMPLE, language="toml")
    try:
        st.code((APP_DIR / "supabase_schema.sql").read_text(encoding="utf-8"), language="sql")
    except OSError:
        st.caption("The `supabase_schema.sql` file should be committed with the app repository.")
    st.info("Historical runs remain permanent only after the app shows `Connected to Supabase`.")


def supabase_upsert(table_name: str, rows: list[dict], conflict_column: str) -> object:
    if not rows:
        return []
    return supabase_request(
        "POST",
        table_name,
        params={"on_conflict": conflict_column},
        json_body=rows,
        prefer="resolution=merge-duplicates,return=representation,missing=default",
    )


def dataframe_from_rows(rows: object, columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows or [])
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    return df[columns]


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
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_job_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO companies (company, created_at)
            SELECT TRIM(company), MIN(created_at)
            FROM targets
            WHERE
                TRIM(company) <> ''
                AND LOWER(TRIM(company)) NOT IN ('company', 'companies')
            GROUP BY TRIM(company)
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO target_job_titles (job_title, created_at)
            SELECT TRIM(job_title), MIN(created_at)
            FROM targets
            WHERE
                TRIM(job_title) <> ''
                AND LOWER(TRIM(job_title)) NOT IN ('job title', 'job titles', 'target job title', 'target job titles')
            GROUP BY TRIM(job_title)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_key TEXT NOT NULL UNIQUE,
                job_fingerprint TEXT,
                run_id TEXT NOT NULL DEFAULT 'legacy',
                run_started_at TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
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
                sponsorship_reason TEXT,
                matched_sponsorship_phrase TEXT,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                relevance_reason TEXT,
                cv_match_score INTEGER NOT NULL DEFAULT 0,
                cv_match_reason TEXT,
                status TEXT NOT NULL DEFAULT 'New',
                notes TEXT DEFAULT '',
                application_status TEXT NOT NULL DEFAULT 'New',
                applied_date TEXT,
                application_notes TEXT DEFAULT '',
                raw_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                merged_into_id INTEGER
            )
            """
        )
        ensure_column(conn, "job_results", "job_fingerprint", "TEXT")
        ensure_column(conn, "job_results", "first_seen_at", "TEXT")
        ensure_column(conn, "job_results", "last_seen_at", "TEXT")
        ensure_column(conn, "job_results", "merged_into_id", "INTEGER")
        ensure_column(conn, "job_results", "updated_at", "TEXT")
        ensure_column(conn, "job_results", "sponsorship_reason", "TEXT")
        ensure_column(conn, "job_results", "matched_sponsorship_phrase", "TEXT")
        ensure_column(conn, "job_results", "relevance_score", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "job_results", "relevance_reason", "TEXT")
        ensure_column(conn, "job_results", "cv_match_score", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "job_results", "cv_match_reason", "TEXT")
        ensure_column(conn, "job_results", "status", "TEXT NOT NULL DEFAULT 'New'")
        ensure_column(conn, "job_results", "notes", "TEXT DEFAULT ''")
        ensure_column(conn, "job_results", "application_status", "TEXT NOT NULL DEFAULT 'New'")
        ensure_column(conn, "job_results", "applied_date", "TEXT")
        ensure_column(conn, "job_results", "application_notes", "TEXT DEFAULT ''")
        ensure_column(conn, "job_results", "run_id", "TEXT NOT NULL DEFAULT 'legacy'")
        ensure_column(conn, "job_results", "run_started_at", "TEXT")
        conn.execute(
            """
            UPDATE job_results
            SET application_status = COALESCE(NULLIF(status, ''), 'New')
            WHERE application_status IS NULL OR application_status = ''
            """
        )
        conn.execute(
            """
            UPDATE job_results
            SET application_status = status
            WHERE
                application_status = 'New'
                AND status IS NOT NULL
                AND status <> ''
                AND status <> 'New'
            """
        )
        conn.execute(
            """
            UPDATE job_results
            SET application_notes = COALESCE(notes, '')
            WHERE application_notes IS NULL OR application_notes = ''
            """
        )
        migrate_sqlite_job_fingerprints(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_job_results_job_fingerprint_active
            ON job_results(job_fingerprint)
            WHERE merged_into_id IS NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                run_id TEXT PRIMARY KEY,
                run_started_at TEXT NOT NULL,
                raw_jobs_found INTEGER NOT NULL DEFAULT 0,
                duplicates_skipped INTEGER NOT NULL DEFAULT 0,
                excluded_by_relevance INTEGER NOT NULL DEFAULT 0,
                jobs_saved INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cv_profiles (
                profile_key TEXT PRIMARY KEY,
                cv_text TEXT,
                cv_summary TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_profiles (
                profile_name TEXT PRIMARY KEY,
                settings_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS application_tracker (
                job_id TEXT PRIMARY KEY,
                application_status TEXT NOT NULL DEFAULT 'New',
                applied_date TEXT,
                application_notes TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                job_id TEXT PRIMARY KEY,
                note_text TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )


def init_storage() -> None:
    if not is_supabase_connected() and is_local_sqlite_allowed():
        init_db()


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize_identity_text(value: object) -> str:
    return re.sub(r"\s+", " ", clean_text(value).lower()).strip()


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "source",
}


def normalize_identity_url(value: object) -> str:
    url = clean_text(value)
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return normalize_identity_text(url)

    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = re.sub(r"/+$", "", parts.path or "")
    filtered_query = []
    for key, query_value in parse_qsl(parts.query, keep_blank_values=False):
        normalized_key = key.lower()
        if normalized_key in TRACKING_QUERY_PARAMS:
            continue
        if any(normalized_key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        filtered_query.append((normalized_key, query_value.strip()))
    query = urlencode(sorted(filtered_query))
    return urlunsplit((scheme, netloc, path, query, ""))


def make_job_fingerprint(
    employer_name: object,
    title: object,
    location: object,
    apply_link: object = "",
    job_url: object = "",
) -> str:
    identity = "|".join(
        [
            normalize_identity_text(employer_name),
            normalize_identity_text(title),
            normalize_identity_text(location),
            normalize_identity_url(apply_link),
            normalize_identity_url(job_url),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def normalize_lookup_value(value: object) -> str:
    return clean_text(value).lower()


def unique_non_header_values(series: pd.Series, header_values: set[str]) -> list[str]:
    cleaned = series.map(clean_text)
    cleaned = cleaned[cleaned != ""]
    cleaned = cleaned[~cleaned.map(normalize_lookup_value).isin(header_values)]
    return cleaned.drop_duplicates().tolist()


def load_targets_from_excel(uploaded_file) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_excel(uploaded_file, usecols=[0, 1], header=None, engine="openpyxl")
    df.columns = ["company", "job_title"]
    companies = unique_non_header_values(df["company"], COMPANY_HEADER_VALUES)
    job_titles = unique_non_header_values(df["job_title"], JOB_TITLE_HEADER_VALUES)
    return pd.DataFrame({"company": companies}), pd.DataFrame({"job_title": job_titles})


def clear_targets() -> None:
    if is_supabase_connected():
        supabase_request("DELETE", "companies", params={"company": "neq.__never_match_empty_delete__"}, prefer="return=minimal")
        supabase_request("DELETE", "target_job_titles", params={"job_title": "neq.__never_match_empty_delete__"}, prefer="return=minimal")
        return

    with get_connection() as conn:
        conn.execute("DELETE FROM companies")
        conn.execute("DELETE FROM target_job_titles")


def save_targets(companies_df: pd.DataFrame, job_titles_df: pd.DataFrame, replace_existing: bool = False) -> tuple[int, int]:
    created_at = utc_now()
    if replace_existing:
        clear_targets()

    if is_supabase_connected():
        company_rows = [
            {"company": clean_text(row.company), "created_at": created_at}
            for row in companies_df.itertuples(index=False)
            if clean_text(row.company)
        ]
        job_title_rows = [
            {"job_title": clean_text(row.job_title), "created_at": created_at}
            for row in job_titles_df.itertuples(index=False)
            if clean_text(row.job_title)
        ]
        supabase_upsert("companies", company_rows, "company")
        supabase_upsert("target_job_titles", job_title_rows, "job_title")
        return len(company_rows), len(job_title_rows)

    inserted_companies = 0
    inserted_job_titles = 0
    with get_connection() as conn:
        for row in companies_df.itertuples(index=False):
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO companies (company, created_at)
                VALUES (?, ?)
                """,
                (row.company, created_at),
            )
            inserted_companies += cursor.rowcount
        for row in job_titles_df.itertuples(index=False):
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO target_job_titles (job_title, created_at)
                VALUES (?, ?)
                """,
                (row.job_title, created_at),
            )
            inserted_job_titles += cursor.rowcount
    return inserted_companies, inserted_job_titles


def get_companies() -> pd.DataFrame:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "companies",
            params={"select": "company,created_at", "order": "company.asc"},
        )
        return dataframe_from_rows(rows, ["company", "created_at"])

    with get_connection() as conn:
        return pd.read_sql_query(
            "SELECT company, created_at FROM companies ORDER BY company",
            conn,
        )


def get_target_job_titles() -> pd.DataFrame:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "target_job_titles",
            params={"select": "job_title,created_at", "order": "job_title.asc"},
        )
        return dataframe_from_rows(rows, ["job_title", "created_at"])

    with get_connection() as conn:
        return pd.read_sql_query(
            "SELECT job_title, created_at FROM target_job_titles ORDER BY job_title",
            conn,
        )


def build_search_combinations(companies: pd.DataFrame, job_titles: pd.DataFrame) -> pd.DataFrame:
    if companies.empty or job_titles.empty:
        return pd.DataFrame(columns=["company", "job_title"])
    company_values = companies["company"].map(clean_text)
    job_title_values = job_titles["job_title"].map(clean_text)
    company_values = company_values[(company_values != "") & (~company_values.map(normalize_lookup_value).isin(COMPANY_HEADER_VALUES))]
    job_title_values = job_title_values[(job_title_values != "") & (~job_title_values.map(normalize_lookup_value).isin(JOB_TITLE_HEADER_VALUES))]
    combinations = pd.MultiIndex.from_product(
        [company_values.drop_duplicates(), job_title_values.drop_duplicates()],
        names=["company", "job_title"],
    ).to_frame(index=False)
    return combinations.reset_index(drop=True)


def get_targets() -> pd.DataFrame:
    return build_search_combinations(get_companies(), get_target_job_titles())


def stable_job_id_from_row(row: pd.Series) -> str:
    return make_job_fingerprint(
        row.get("employer_name"),
        row.get("title"),
        row.get("location"),
        row.get("apply_link"),
        row.get("job_url"),
    )


def ensure_job_ids(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    prepared = df.copy()
    if "id" not in prepared.columns:
        prepared["id"] = prepared.apply(stable_job_id_from_row, axis=1)
        return prepared

    missing_id_mask = prepared["id"].map(clean_text) == ""
    if missing_id_mask.any():
        prepared.loc[missing_id_mask, "id"] = prepared[missing_id_mask].apply(stable_job_id_from_row, axis=1)
    return prepared


def display_job_id_column(df: pd.DataFrame) -> pd.DataFrame:
    prepared = ensure_job_ids(df)
    if "id" not in prepared.columns:
        return prepared
    return prepared.rename(columns={"id": "Job ID"})


def merge_tracking_data(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return results
    merged = results.copy()
    for column, default in [("application_status", "New"), ("application_notes", ""), ("notes", "")]:
        if column not in merged.columns:
            merged[column] = default

    merged["application_status"] = merged["application_status"].map(normalized_application_status)
    merged["application_notes"] = merged["application_notes"].fillna("").map(clean_text)
    if "status" in merged.columns:
        legacy_status = merged["status"].map(normalized_application_status)
        use_legacy_status = (merged["application_status"] == "New") & (legacy_status != "New")
        merged.loc[use_legacy_status, "application_status"] = legacy_status.loc[use_legacy_status]
    if "notes" in merged.columns:
        legacy_notes = merged["notes"].fillna("").map(clean_text)
        use_legacy_notes = (merged["application_notes"] == "") & (legacy_notes != "")
        merged.loc[use_legacy_notes, "application_notes"] = legacy_notes.loc[use_legacy_notes]
    merged["notes"] = merged["application_notes"]
    merged["status"] = merged["application_status"]
    return merged


def get_results() -> pd.DataFrame:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "job_results",
            params={"select": ",".join(RESULT_COLUMNS), "merged_into_id": "is.null", "order": "created_at.desc"},
        )
        results = dataframe_from_rows(rows, RESULT_COLUMNS)
        if not results.empty:
            results = results.sort_values(
                by=["run_started_at", "created_at", "relevance_score"],
                ascending=[False, False, False],
                na_position="last",
            )
        return merge_tracking_data(ensure_job_ids(results))

    with get_connection() as conn:
        results = pd.read_sql_query(
            """
            SELECT
                id,
                job_fingerprint,
                run_id,
                run_started_at,
                first_seen_at,
                last_seen_at,
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
                sponsorship_reason,
                matched_sponsorship_phrase,
                relevance_score,
                relevance_reason,
                cv_match_score,
                cv_match_reason,
                status,
                notes,
                application_status,
                applied_date,
                application_notes,
                job_url,
                apply_link,
                description,
                created_at,
                updated_at,
                merged_into_id
            FROM job_results
            WHERE merged_into_id IS NULL
            ORDER BY COALESCE(run_started_at, created_at) DESC, relevance_score DESC, company, searched_job_title
            """,
            conn,
        )
    return merge_tracking_data(ensure_job_ids(results))


def get_search_runs() -> pd.DataFrame:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "search_runs",
            params={
                "select": "run_id,run_started_at,raw_jobs_found,duplicates_skipped,excluded_by_relevance,jobs_saved",
                "order": "run_started_at.desc",
            },
        )
        return dataframe_from_rows(
            rows,
            [
                "run_id",
                "run_started_at",
                "raw_jobs_found",
                "duplicates_skipped",
                "excluded_by_relevance",
                "jobs_saved",
            ],
        )

    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                run_id,
                run_started_at,
                raw_jobs_found,
                duplicates_skipped,
                excluded_by_relevance,
                jobs_saved
            FROM search_runs
            ORDER BY run_started_at DESC
            """,
            conn,
        )


def update_job_cv_match(job_id: int, cv_match_score: int, cv_match_reason: str) -> None:
    if is_supabase_connected():
        supabase_request(
            "PATCH",
            "job_results",
            params={"id": f"eq.{job_id}"},
            json_body={
                "cv_match_score": int(cv_match_score),
                "cv_match_reason": cv_match_reason,
            },
            prefer="return=minimal",
        )
        return

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE job_results
            SET cv_match_score = ?, cv_match_reason = ?
            WHERE id = ?
            """,
            (int(cv_match_score), cv_match_reason, job_id),
        )


def recalculate_cv_matches_for_results(results: pd.DataFrame, cv_text: str) -> int:
    if results.empty:
        return 0
    updated = 0
    for row in ensure_job_ids(results).itertuples(index=False):
        job_id = getattr(row, "id", None)
        if clean_text(job_id) == "":
            continue
        try:
            numeric_job_id = int(job_id)
        except (TypeError, ValueError):
            continue
        cv_match_score, cv_match_reason = calculate_cv_match(
            clean_text(getattr(row, "title", "")),
            clean_text(getattr(row, "employer_name", "")),
            clean_text(getattr(row, "description", "")),
            clean_text(getattr(row, "searched_job_title", "")),
            cv_text,
        )
        update_job_cv_match(numeric_job_id, cv_match_score, cv_match_reason)
        updated += 1
    return updated


def update_job_sponsorship(job_id: object, sponsorship_status: str, sponsorship_reason: str, matched_phrase: str) -> bool:
    job_id_text = clean_text(job_id)
    try:
        numeric_job_id = int(job_id_text)
    except (TypeError, ValueError):
        return False
    if is_supabase_connected():
        supabase_request(
            "PATCH",
            "job_results",
            params={"id": f"eq.{numeric_job_id}"},
            json_body={
                "sponsorship_status": sponsorship_status,
                "sponsorship_reason": sponsorship_reason,
                "matched_sponsorship_phrase": matched_phrase,
                "updated_at": utc_now(),
            },
            prefer="return=minimal",
        )
        return True

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE job_results
            SET
                sponsorship_status = ?,
                sponsorship_reason = ?,
                matched_sponsorship_phrase = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (sponsorship_status, sponsorship_reason, matched_phrase, utc_now(), numeric_job_id),
        )
    return True


def recalculate_sponsorship_for_results(results: pd.DataFrame) -> int:
    if results.empty:
        return 0
    updated = 0
    for row in ensure_job_ids(results).itertuples(index=False):
        job_id = getattr(row, "id", None)
        application_text = " ".join(
            [
                clean_text(getattr(row, "apply_link", "")),
                clean_text(getattr(row, "job_url", "")),
            ]
        )
        status, reason, matched_phrase = detect_sponsorship_status(
            clean_text(getattr(row, "title", "")),
            clean_text(getattr(row, "description", "")),
            application_text,
        )
        if update_job_sponsorship(job_id, status, reason, matched_phrase):
            updated += 1
    return updated


def get_job_tracking_from_storage(job_id: object, job_fingerprint: str = "") -> dict:
    job_id_text = clean_text(job_id)
    fingerprint = clean_text(job_fingerprint)
    try:
        numeric_job_id = int(job_id_text)
    except (TypeError, ValueError):
        numeric_job_id = None

    if is_supabase_connected():
        params = {
            "select": "id,job_fingerprint,application_status,notes,application_notes,applied_date",
            "limit": 1,
        }
        if numeric_job_id is not None:
            params["id"] = f"eq.{numeric_job_id}"
        elif fingerprint:
            params["job_fingerprint"] = f"eq.{fingerprint}"
        else:
            return {}
        rows = supabase_request("GET", "job_results", params=params)
        return rows[0] if rows else {}

    with get_connection() as conn:
        if numeric_job_id is not None:
            row = conn.execute(
                """
                SELECT id, job_fingerprint, application_status, notes, application_notes, applied_date
                FROM job_results
                WHERE id = ?
                LIMIT 1
                """,
                (numeric_job_id,),
            ).fetchone()
        elif fingerprint:
            row = conn.execute(
                """
                SELECT id, job_fingerprint, application_status, notes, application_notes, applied_date
                FROM job_results
                WHERE job_fingerprint = ? AND merged_into_id IS NULL
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        else:
            row = None
    return dict(row) if row else {}


def save_search_run(
    run_id: str,
    run_started_at: str,
    raw_jobs_found: int,
    duplicates_skipped: int,
    excluded_by_relevance: int,
    jobs_saved: int,
) -> None:
    if is_supabase_connected():
        supabase_upsert(
            "search_runs",
            [
                {
                    "run_id": run_id,
                    "run_started_at": run_started_at,
                    "raw_jobs_found": raw_jobs_found,
                    "duplicates_skipped": duplicates_skipped,
                    "excluded_by_relevance": excluded_by_relevance,
                    "jobs_saved": jobs_saved,
                }
            ],
            "run_id",
        )
        return

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO search_runs (
                run_id,
                run_started_at,
                raw_jobs_found,
                duplicates_skipped,
                excluded_by_relevance,
                jobs_saved
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                run_started_at = excluded.run_started_at,
                raw_jobs_found = excluded.raw_jobs_found,
                duplicates_skipped = excluded.duplicates_skipped,
                excluded_by_relevance = excluded.excluded_by_relevance,
                jobs_saved = excluded.jobs_saved
            """,
            (run_id, run_started_at, raw_jobs_found, duplicates_skipped, excluded_by_relevance, jobs_saved),
        )


def update_job_tracking(
    job_id: object,
    application_status: str,
    application_notes: str,
    applied_date: str = "",
    job_fingerprint: str = "",
) -> bool:
    if application_status not in APPLICATION_STATUSES:
        application_status = "New"
    job_id_text = clean_text(job_id)
    fingerprint = clean_text(job_fingerprint)
    try:
        numeric_job_id = int(job_id_text)
    except (TypeError, ValueError):
        numeric_job_id = None
    before = get_job_tracking_from_storage(numeric_job_id if numeric_job_id is not None else job_id_text, fingerprint)
    print(
        "TRACKING BEFORE SAVE:",
        "Job ID:", job_id_text,
        "Old Status:", clean_text(before.get("application_status")),
        "New Status:", application_status,
    )

    if is_supabase_connected():
        updated_at = utc_now()
        params = {"id": f"eq.{numeric_job_id}"} if numeric_job_id is not None else {"job_fingerprint": f"eq.{fingerprint}"}
        if numeric_job_id is None and not fingerprint:
            return False
        supabase_request(
            "PATCH",
            "job_results",
            params=params,
            json_body={
                "application_status": application_status,
                "application_notes": application_notes,
                "applied_date": applied_date or None,
                "notes": application_notes,
                "updated_at": updated_at,
            },
            prefer="return=minimal",
        )
        tracker_job_id = str(numeric_job_id) if numeric_job_id is not None else fingerprint
        supabase_upsert(
            "application_tracker",
            [
                {
                    "job_id": tracker_job_id,
                    "job_result_id": numeric_job_id,
                    "application_status": application_status,
                    "application_notes": application_notes,
                    "applied_date": applied_date or None,
                    "updated_at": updated_at,
                }
            ],
            "job_id",
        )
        supabase_upsert(
            "notes",
            [
                {
                    "job_id": tracker_job_id,
                    "job_result_id": numeric_job_id,
                    "note_text": application_notes,
                    "updated_at": updated_at,
                }
            ],
            "job_id",
        )
        after = get_job_tracking_from_storage(numeric_job_id if numeric_job_id is not None else job_id_text, fingerprint)
        print(
            "TRACKING AFTER SAVE:",
            "Job ID:", job_id_text,
            "Database Status:", clean_text(after.get("application_status")),
        )
        return (
            normalized_application_status(after.get("application_status")) == application_status
            and clean_text(after.get("notes")) == application_notes
        )

    if numeric_job_id is None and not fingerprint:
        return False
    with get_connection() as conn:
        updated_at = utc_now()
        if numeric_job_id is not None:
            conn.execute(
                """
                UPDATE job_results
                SET
                    application_status = ?,
                    application_notes = ?,
                    applied_date = ?,
                    notes = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (application_status, application_notes, applied_date, application_notes, updated_at, numeric_job_id),
            )
        else:
            conn.execute(
                """
                UPDATE job_results
                SET
                    application_status = ?,
                    application_notes = ?,
                    applied_date = ?,
                    notes = ?,
                    updated_at = ?
                WHERE job_fingerprint = ? AND merged_into_id IS NULL
                """,
                (application_status, application_notes, applied_date, application_notes, updated_at, fingerprint),
            )
        tracker_job_id = str(numeric_job_id) if numeric_job_id is not None else fingerprint
        conn.execute(
            """
            INSERT INTO application_tracker (job_id, job_result_id, application_status, applied_date, application_notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                job_result_id = excluded.job_result_id,
                application_status = excluded.application_status,
                applied_date = excluded.applied_date,
                application_notes = excluded.application_notes,
                updated_at = excluded.updated_at
            """,
            (tracker_job_id, numeric_job_id, application_status, applied_date, application_notes, updated_at),
        )
        conn.execute(
            """
            INSERT INTO notes (job_id, job_result_id, note_text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                job_result_id = excluded.job_result_id,
                note_text = excluded.note_text,
                updated_at = excluded.updated_at
            """,
            (tracker_job_id, numeric_job_id, application_notes, updated_at),
        )
    after = get_job_tracking_from_storage(numeric_job_id if numeric_job_id is not None else job_id_text, fingerprint)
    print(
        "TRACKING AFTER SAVE:",
        "Job ID:", job_id_text,
        "Database Status:", clean_text(after.get("application_status")),
    )
    return (
        normalized_application_status(after.get("application_status")) == application_status
        and clean_text(after.get("notes")) == application_notes
    )


def save_cv_profile(cv_text: str, cv_summary: str = "") -> None:
    updated_at = utc_now()
    if is_supabase_connected():
        supabase_upsert(
            "cv_profiles",
            [
                {
                    "profile_key": "default",
                    "cv_text": cv_text,
                    "cv_summary": cv_summary,
                    "updated_at": updated_at,
                }
            ],
            "profile_key",
        )
        return

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO cv_profiles (profile_key, cv_text, cv_summary, updated_at)
            VALUES ('default', ?, ?, ?)
            ON CONFLICT(profile_key) DO UPDATE SET
                cv_text = excluded.cv_text,
                cv_summary = excluded.cv_summary,
                updated_at = excluded.updated_at
            """,
            (cv_text, cv_summary, updated_at),
        )


def get_cv_profile() -> tuple[str, str]:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "cv_profiles",
            params={"select": "cv_text,cv_summary", "profile_key": "eq.default", "limit": 1},
        )
        if rows:
            row = rows[0]
            return clean_text(row.get("cv_text")), clean_text(row.get("cv_summary"))
        return "", ""

    with get_connection() as conn:
        row = conn.execute(
            "SELECT cv_text, cv_summary FROM cv_profiles WHERE profile_key = 'default'"
        ).fetchone()
    if not row:
        return "", ""
    return clean_text(row["cv_text"]), clean_text(row["cv_summary"])


def save_search_profile(profile_name: str, settings: dict) -> None:
    updated_at = utc_now()
    payload = json.dumps(settings)
    if is_supabase_connected():
        supabase_upsert(
            "saved_profiles",
            [
                {
                    "profile_name": profile_name,
                    "settings_json": settings,
                    "updated_at": updated_at,
                }
            ],
            "profile_name",
        )
        return

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO saved_profiles (profile_name, settings_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_name) DO UPDATE SET
                settings_json = excluded.settings_json,
                updated_at = excluded.updated_at
            """,
            (profile_name, payload, updated_at),
        )


def get_search_profile(profile_name: str) -> dict:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "saved_profiles",
            params={"select": "settings_json", "profile_name": f"eq.{profile_name}", "limit": 1},
        )
        if not rows:
            return {}
        settings = rows[0].get("settings_json") or {}
        return settings if isinstance(settings, dict) else {}

    with get_connection() as conn:
        row = conn.execute(
            "SELECT settings_json FROM saved_profiles WHERE profile_name = ?",
            (profile_name,),
        ).fetchone()
    if not row:
        return {}
    try:
        settings = json.loads(row["settings_json"])
    except json.JSONDecodeError:
        return {}
    return settings if isinstance(settings, dict) else {}


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


def parse_account_int(payload: dict, field_names: tuple[str, ...]) -> int | None:
    for field_name in field_names:
        value = payload.get(field_name)
        if value is None:
            continue
        try:
            return int(float(str(value).replace(",", "")))
        except ValueError:
            continue
    return None


@st.cache_data(ttl=300, show_spinner=False)
def get_serpapi_account_usage(api_key: str) -> dict:
    response = requests.get(SERPAPI_ACCOUNT_ENDPOINT, params={"api_key": api_key}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    monthly_limit = parse_account_int(
        payload,
        ("searches_per_month", "monthly_search_limit", "plan_searches_per_month"),
    )
    monthly_used = parse_account_int(
        payload,
        ("this_month_usage", "monthly_searches_used", "searches_used"),
    )
    searches_remaining = parse_account_int(
        payload,
        ("total_searches_left", "searches_remaining", "monthly_searches_left"),
    )
    if searches_remaining is None and monthly_limit is not None and monthly_used is not None:
        searches_remaining = max(0, monthly_limit - monthly_used)
    return {
        "monthly_used": monthly_used,
        "monthly_limit": monthly_limit,
        "searches_remaining": searches_remaining,
        "raw": payload,
    }


def quota_value(account_usage: dict, key: str) -> object:
    value = account_usage.get(key)
    return value if value is not None else "Unknown"


def phrase_in_text(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()


def collect_application_text(job: dict) -> str:
    apply_options = job.get("apply_options") or []
    related_links = job.get("related_links") or []
    parts = []
    for option in apply_options:
        parts.extend([clean_text(option.get("title")), clean_text(option.get("link"))])
    for link in related_links:
        parts.extend([clean_text(link.get("text")), clean_text(link.get("link"))])
    return " ".join(part for part in parts if part)


def detect_sponsorship_status(title: object, description: object, application_text: object = "") -> tuple[str, str, str]:
    text = " ".join([clean_text(title), clean_text(description), clean_text(application_text)]).lower()
    for phrase in SPONSORSHIP_NOT_AVAILABLE_PHRASES:
        if phrase_in_text(text, phrase):
            if phrase in SPONSORSHIP_AUTHORIZATION_PHRASES:
                return "requires work authorization", f"Requires work authorization: {phrase}", phrase
            return "sponsorship not available", f"Negative phrase matched: {phrase}", phrase
    for phrase in SPONSORSHIP_AUTHORIZATION_PHRASES:
        if phrase_in_text(text, phrase):
            return "requires work authorization", f"Requires work authorization: {phrase}", phrase
    for phrase in SPONSORSHIP_POSITIVE_PHRASES:
        if phrase_in_text(text, phrase):
            return "sponsorship available", f"Explicit positive phrase matched: {phrase}", phrase
    return "not mentioned", "No explicit sponsorship phrase found", ""


def contains_term(text: str, term: str) -> bool:
    escaped_words = [re.escape(word) for word in term.lower().split()]
    pattern = r"\b" + r"\s+".join(escaped_words) + r"\b"
    return re.search(pattern, text.lower()) is not None


def score_job_relevance(
    title: str,
    description: str = "",
    include_broader_infrastructure: bool = True,
) -> tuple[int, str, bool]:
    title = clean_text(title).lower()
    description = clean_text(description).lower()
    if not title:
        return 0, "No job title found.", False

    excluded_terms = [term for term in EXCLUDED_RELEVANCE_TERMS if contains_term(title, term)]
    if excluded_terms:
        return 0, f"Rejected because title contains excluded term: {', '.join(excluded_terms)}.", True

    strong_terms = [term for term in STRONG_RELEVANCE_TERMS if contains_term(title, term)]
    management_terms = [term for term in INFRASTRUCTURE_MANAGEMENT_TERMS if contains_term(title, term)]
    context_text = f"{title} {description}"
    context_terms = [term for term in INFRASTRUCTURE_CONTEXT_TERMS if contains_term(context_text, term)]
    has_context = bool(context_terms)

    score = len(strong_terms) * 3
    reasons = []
    if strong_terms:
        reasons.append(f"Strong match +3 each: {', '.join(strong_terms)}")
    if include_broader_infrastructure and management_terms and has_context:
        score += 1
        reasons.append(f"Infrastructure management role +1: {', '.join(management_terms)}")
        score += 1
        reasons.append(f"Infrastructure context +1: {', '.join(context_terms)}")
    elif management_terms and not strong_terms:
        if include_broader_infrastructure:
            reasons.append(f"Management role found but no infrastructure context: {', '.join(management_terms)}")
        else:
            reasons.append("Broader infrastructure management jobs disabled.")

    if not strong_terms and not (include_broader_infrastructure and management_terms and has_context):
        reason = "; ".join(reasons) if reasons else "No strong match or infrastructure-context management role found."
        return 0, reason, False

    return score, "; ".join(reasons), False


def calculate_relevance(title: str, description: str = "", include_broader_infrastructure: bool = True) -> int:
    relevance_score, _, is_excluded = score_job_relevance(title, description, include_broader_infrastructure)
    if is_excluded:
        return 0
    return relevance_score


CV_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "that",
    "this",
    "your",
    "you",
    "are",
    "was",
    "were",
    "have",
    "has",
    "job",
    "role",
    "work",
    "team",
    "will",
    "our",
    "their",
    "they",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "by",
    "or",
    "as",
    "at",
}

CV_SKILL_TERMS = (
    "pmo",
    "project controls",
    "contracts",
    "schedule",
    "scheduler",
    "risk management",
    "risk",
)

CV_INDUSTRY_TERMS = (
    "rail",
    "infrastructure",
    "metro",
    "transit",
    "construction",
    "water",
    "wastewater",
    "aviation",
    "airport",
    "highway",
    "bridge",
    "tunnel",
    "design-build",
)

CV_MANAGEMENT_TERMS = (
    "construction management",
    "project management",
    "program management",
    "contract management",
    "controls management",
    "stakeholder management",
)


def tokenize_for_match(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9+-]{2,}", clean_text(text).lower()))
    return {token for token in tokens if token not in CV_STOPWORDS}


def extract_pdf_text(file) -> str:
    from pypdf import PdfReader

    text = ""
    reader = PdfReader(file)
    for page in reader.pages:
        try:
            text += page.extract_text() or ""
        except Exception:
            continue
    return text


def extract_with_pdfplumber(file) -> str:
    import pdfplumber

    text = ""
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text


def extract_cv_text(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    suffix = Path(uploaded_file.name).suffix.lower()
    data = uploaded_file.getvalue()
    if suffix == ".pdf":
        text = ""
        try:
            text = extract_pdf_text(BytesIO(data))
        except Exception:
            text = ""
        if len(text.strip()) < 50:
            try:
                text = extract_with_pdfplumber(BytesIO(data))
            except Exception:
                text = ""
        return text
    if suffix == ".docx":
        from docx import Document

        document = Document(BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    return ""


def matching_phrases(cv_text: str, job_text: str, phrases: tuple[str, ...]) -> list[str]:
    cv_normalized = clean_text(cv_text).lower()
    job_normalized = clean_text(job_text).lower()
    return [phrase for phrase in phrases if phrase in cv_normalized and phrase in job_normalized]


def aliases_match(cv_text: str, job_text: str, aliases: tuple[str, ...]) -> bool:
    cv_normalized = clean_text(cv_text).lower()
    job_normalized = clean_text(job_text).lower()
    return any(alias in cv_normalized for alias in aliases) and any(alias in job_normalized for alias in aliases)


def shared_terms(cv_text: str, job_text: str, terms: tuple[str, ...]) -> list[str]:
    cv_normalized = clean_text(cv_text).lower()
    job_normalized = clean_text(job_text).lower()
    return [
        term
        for term in terms
        if contains_term(cv_normalized, term) and contains_term(job_normalized, term)
    ]


def job_terms(job_text: str, terms: tuple[str, ...]) -> list[str]:
    job_normalized = clean_text(job_text).lower()
    return [term for term in terms if contains_term(job_normalized, term)]


def calculate_cv_match(
    title: str,
    employer: str,
    description: str,
    searched_job_title: str,
    cv_text: str,
) -> tuple[int, str]:
    cv_text = clean_text(cv_text)
    if not cv_text:
        return 0, "No CV uploaded."

    job_text = f"{title} {employer} {description} {searched_job_title}"
    if not clean_text(job_text):
        return 0, "No job text available for CV comparison."

    strong_terms = (
        "project controls",
        "program controls",
        "controls manager",
        "pmo",
        "project controls professional",
        "contracts manager",
        "contract manager",
        "contract management",
        "scheduling manager",
        "planning manager",
        "scheduler",
        "risk manager",
        "risk management",
    )
    medium_terms = (
        "project manager",
        "construction manager",
        "program manager",
    )
    infrastructure_terms = (
        "rail",
        "metro",
        "transit",
        "tunnel",
        "infrastructure",
        "transportation",
        "aviation",
        "airport",
        "water",
        "wastewater",
        "highway",
        "bridge",
    )
    seniority_terms = (
        "senior",
        "lead",
        "director",
        "manager",
    )
    penalty_terms = (
        "regulatory",
        "regulatory specialist",
        "compliance",
        "compliance only",
        "utilities",
        "utility",
        "software",
        "it",
        "developer",
        "architect",
    )

    strong_matches = shared_terms(cv_text, job_text, strong_terms)
    medium_matches = shared_terms(cv_text, job_text, medium_terms)
    industry_matches = shared_terms(cv_text, job_text, infrastructure_terms)
    seniority_matches = shared_terms(cv_text, job_text, seniority_terms)
    penalties = job_terms(job_text, penalty_terms)

    raw_score = 0
    raw_score += 40 * len(strong_matches)
    raw_score += 10 * len(medium_matches)
    raw_score += 15 * len(industry_matches)
    raw_score += 8 * len(seniority_matches)
    raw_score -= 30 * len(penalties)

    score = max(0, min(100, raw_score))
    reasons = []
    if strong_matches:
        reasons.append(f"Strong project controls / PMO / contracts / planning / risk keywords +40 each: {', '.join(strong_matches)}")
    if medium_matches:
        reasons.append(f"Generic management role keywords +10 each: {', '.join(medium_matches)}")
    if industry_matches:
        reasons.append(f"Infrastructure/transportation industry terms +15 each: {', '.join(industry_matches)}")
    if seniority_matches:
        reasons.append(f"Seniority keywords +8 each: {', '.join(seniority_matches)}")
    if penalties:
        reasons.append(f"Penalty -30 each for regulatory/compliance/utilities/technical terms: {', '.join(penalties)}")
    if not reasons:
        return 0, "No CV match against senior infrastructure PMO, project controls, contracts, scheduling, planning, risk, industry, or seniority criteria."
    return score, "; ".join(reasons)


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


def get_job_id(job: dict) -> str:
    return clean_text(job.get("job_id") or job.get("id"))


def get_employer_name(job: dict) -> str:
    return clean_text(job.get("company_name") or job.get("employer_name"))


def make_job_search_queries(company: str, job_title: str) -> list[str]:
    return [
        f"{job_title} {company} jobs United States",
        f"{job_title} {company} careers",
        f"{job_title} {company} LinkedIn jobs",
        f"{job_title} {company} infrastructure jobs",
        f"{job_title} {company} construction jobs",
        f"{job_title} {company} rail transit jobs",
    ]


def job_dedupe_keys(job: dict) -> list[tuple[str, str]]:
    keys = []
    job_id = get_job_id(job).lower()
    if job_id:
        keys.append(("job_id", job_id))

    application_link = best_apply_link(job).lower()
    if application_link:
        keys.append(("application_link", application_link))

    title = clean_text(job.get("title")).lower()
    employer_name = get_employer_name(job).lower()
    location = clean_text(job.get("location")).lower()
    title_employer_location = "|".join([title, employer_name, location])
    if title_employer_location != "||":
        keys.append(("title_employer_location", title_employer_location))
    return keys or [("empty", "")]


def job_dedupe_key(job: dict) -> tuple[str, str]:
    return job_dedupe_keys(job)[0]


def deduplicate_jobs(jobs: list[dict]) -> tuple[list[dict], int]:
    seen = set()
    unique_jobs = []
    duplicates = 0
    for job in jobs:
        keys = job_dedupe_keys(job)
        if any(key in seen for key in keys):
            duplicates += 1
            continue
        seen.update(keys)
        unique_jobs.append(job)
    return unique_jobs, duplicates


def get_salary(job: dict) -> str:
    detected = job.get("detected_extensions") or {}
    salary = detected.get("salary")
    if salary:
        return str(salary)
    extensions = job.get("extensions") or []
    salary_bits = [item for item in extensions if "$" in str(item)]
    return ", ".join(salary_bits)


def make_result_key(company: str, searched_job_title: str, job: dict) -> str:
    dedupe_type, dedupe_value = job_dedupe_key(job)
    identity = "|".join([clean_text(company).lower(), clean_text(searched_job_title).lower(), dedupe_type, dedupe_value])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


STATUS_PRIORITY = {
    "New": 0,
    "Interested": 1,
    "Applied": 2,
    "Interview": 3,
    "Offer": 4,
    "Rejected": 5,
    "Closed": 6,
    "Archived": 7,
}

SPONSORSHIP_PRIORITY = {
    "sponsorship not available": 0,
    "not mentioned": 1,
    "requires work authorization": 2,
    "sponsorship available": 3,
}


def best_application_status(*statuses: object) -> str:
    cleaned = [normalized_application_status(status) for status in statuses]
    return max(cleaned or ["New"], key=lambda status: STATUS_PRIORITY.get(status, 0))


def best_non_empty(*values: object) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def max_numeric(*values: object) -> int:
    numeric_values = pd.to_numeric(pd.Series(list(values)), errors="coerce").dropna()
    if numeric_values.empty:
        return 0
    return int(numeric_values.max())


def best_sponsorship_status(existing_status: object, new_status: object) -> str:
    existing = normalize_sponsorship_status(existing_status)
    new = normalize_sponsorship_status(new_status)
    return new if SPONSORSHIP_PRIORITY.get(new, 0) > SPONSORSHIP_PRIORITY.get(existing, 0) else existing


def is_explicit_positive_sponsorship(status: object, matched_phrase: object) -> bool:
    return normalize_sponsorship_status(status) == "sponsorship available" and clean_text(matched_phrase).lower() in SPONSORSHIP_POSITIVE_PHRASES


def choose_sponsorship_update(existing: dict, new_status: str, new_reason: str, new_phrase: str) -> tuple[str, str, str]:
    existing_status = normalize_sponsorship_status(existing.get("sponsorship_status"))
    existing_reason = clean_text(existing.get("sponsorship_reason"))
    existing_phrase = clean_text(existing.get("matched_sponsorship_phrase"))
    if new_status == "not mentioned" and is_explicit_positive_sponsorship(existing_status, existing_phrase):
        return existing_status, existing_reason, existing_phrase
    return new_status, new_reason, new_phrase


def best_posted_at(existing_posted_at: object, new_posted_at: object, existing_created_at: object = "", new_created_at: object = "") -> str:
    existing_text = clean_text(existing_posted_at)
    new_text = clean_text(new_posted_at)
    if not existing_text:
        return new_text
    if not new_text:
        return existing_text
    existing_score = parse_recency_score(existing_text, existing_created_at)
    new_score = parse_recency_score(new_text, new_created_at)
    return new_text if new_score > existing_score else existing_text


def job_fingerprint_from_values(employer_name: str, title: str, location: str, apply_link: str, job_url: str) -> str:
    return make_job_fingerprint(employer_name, title, location, apply_link, job_url)


def job_fingerprint_from_job(job: dict) -> str:
    return job_fingerprint_from_values(
        get_employer_name(job),
        clean_text(job.get("title")),
        clean_text(job.get("location")),
        best_apply_link(job),
        normalize_job_url(job),
    )


def merge_job_record_values(records: list[dict]) -> dict:
    sorted_records = sorted(records, key=lambda item: (clean_text(item.get("first_seen_at")) or clean_text(item.get("created_at")), int(item.get("id") or 0)))
    keeper = sorted_records[0].copy()
    for record in sorted_records[1:]:
        keeper["application_status"] = best_application_status(keeper.get("application_status"), record.get("application_status"), keeper.get("status"), record.get("status"))
        keeper["status"] = keeper["application_status"]
        keeper["application_notes"] = best_non_empty(keeper.get("application_notes"), record.get("application_notes"), keeper.get("notes"), record.get("notes"))
        keeper["notes"] = keeper["application_notes"]
        keeper["applied_date"] = best_non_empty(keeper.get("applied_date"), record.get("applied_date"))
        keeper["relevance_score"] = max_numeric(keeper.get("relevance_score"), record.get("relevance_score"))
        keeper["cv_match_score"] = max_numeric(keeper.get("cv_match_score"), record.get("cv_match_score"))
        keeper["relevance_reason"] = best_non_empty(keeper.get("relevance_reason"), record.get("relevance_reason"))
        keeper["cv_match_reason"] = best_non_empty(keeper.get("cv_match_reason"), record.get("cv_match_reason"))
        keeper["salary"] = best_non_empty(keeper.get("salary"), record.get("salary"))
        keeper["sponsorship_status"] = best_sponsorship_status(keeper.get("sponsorship_status"), record.get("sponsorship_status"))
        keeper["sponsorship_reason"] = best_non_empty(keeper.get("sponsorship_reason"), record.get("sponsorship_reason"))
        keeper["matched_sponsorship_phrase"] = best_non_empty(keeper.get("matched_sponsorship_phrase"), record.get("matched_sponsorship_phrase"))
        keeper["first_seen_at"] = min(
            [value for value in [clean_text(keeper.get("first_seen_at")), clean_text(record.get("first_seen_at")), clean_text(keeper.get("created_at")), clean_text(record.get("created_at"))] if value]
        )
        keeper["last_seen_at"] = max(
            [value for value in [clean_text(keeper.get("last_seen_at")), clean_text(record.get("last_seen_at")), clean_text(keeper.get("run_started_at")), clean_text(record.get("run_started_at"))] if value]
        )
    return keeper


def row_job_fingerprint(row: dict | pd.Series) -> str:
    return make_job_fingerprint(
        row.get("employer_name"),
        row.get("title"),
        row.get("location"),
        row.get("apply_link"),
        row.get("job_url"),
    )


def migrate_sqlite_job_fingerprints(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM job_results").fetchall()
    if not rows:
        return
    records = [dict(row) for row in rows]
    now = utc_now()
    for record in records:
        fingerprint = clean_text(record.get("job_fingerprint")) or row_job_fingerprint(record)
        first_seen_at = clean_text(record.get("first_seen_at")) or clean_text(record.get("created_at")) or now
        last_seen_at = clean_text(record.get("last_seen_at")) or clean_text(record.get("run_started_at")) or first_seen_at
        conn.execute(
            """
            UPDATE job_results
            SET job_fingerprint = ?, first_seen_at = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (fingerprint, first_seen_at, last_seen_at, record.get("id")),
        )

    refreshed = [dict(row) for row in conn.execute("SELECT * FROM job_results WHERE merged_into_id IS NULL")]
    groups: dict[str, list[dict]] = {}
    for record in refreshed:
        fingerprint = clean_text(record.get("job_fingerprint"))
        if fingerprint:
            groups.setdefault(fingerprint, []).append(record)

    for fingerprint, duplicate_records in groups.items():
        if len(duplicate_records) < 2:
            continue
        merged = merge_job_record_values(duplicate_records)
        keeper_id = int(merged["id"])
        conn.execute(
            """
            UPDATE job_results
            SET
                application_status = ?,
                status = ?,
                application_notes = ?,
                notes = ?,
                applied_date = ?,
                relevance_score = ?,
                cv_match_score = ?,
                relevance_reason = ?,
                cv_match_reason = ?,
                salary = ?,
                sponsorship_status = ?,
                sponsorship_reason = ?,
                matched_sponsorship_phrase = ?,
                first_seen_at = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                merged.get("application_status"),
                merged.get("status"),
                merged.get("application_notes"),
                merged.get("notes"),
                merged.get("applied_date"),
                merged.get("relevance_score"),
                merged.get("cv_match_score"),
                merged.get("relevance_reason"),
                merged.get("cv_match_reason"),
                merged.get("salary"),
                merged.get("sponsorship_status"),
                merged.get("sponsorship_reason"),
                merged.get("matched_sponsorship_phrase"),
                merged.get("first_seen_at"),
                merged.get("last_seen_at"),
                keeper_id,
            ),
        )
        for record in duplicate_records:
            duplicate_id = int(record["id"])
            if duplicate_id != keeper_id:
                conn.execute("UPDATE job_results SET merged_into_id = ? WHERE id = ?", (keeper_id, duplicate_id))


def migrate_supabase_job_fingerprints() -> None:
    if st.session_state.get("job_fingerprint_migration_done"):
        return
    try:
        rows = supabase_request(
            "GET",
            "job_results",
            params={"select": ",".join(RESULT_COLUMNS), "order": "created_at.asc"},
        )
    except requests.RequestException:
        return
    if not rows:
        st.session_state["job_fingerprint_migration_done"] = True
        return

    now = utc_now()
    grouped: dict[str, list[dict]] = {}
    for record in rows:
        fingerprint = clean_text(record.get("job_fingerprint")) or row_job_fingerprint(record)
        first_seen_at = clean_text(record.get("first_seen_at")) or clean_text(record.get("created_at")) or now
        last_seen_at = clean_text(record.get("last_seen_at")) or clean_text(record.get("run_started_at")) or first_seen_at
        record["job_fingerprint"] = fingerprint
        record["first_seen_at"] = first_seen_at
        record["last_seen_at"] = last_seen_at
        if not clean_text(record.get("merged_into_id")):
            grouped.setdefault(fingerprint, []).append(record)
        if not clean_text(record.get("job_fingerprint")) or not clean_text(record.get("first_seen_at")) or not clean_text(record.get("last_seen_at")):
            continue
        supabase_request(
            "PATCH",
            "job_results",
            params={"id": f"eq.{record.get('id')}"},
            json_body={
                "job_fingerprint": fingerprint,
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
            },
            prefer="return=minimal",
        )

    for fingerprint, duplicate_records in grouped.items():
        if len(duplicate_records) < 2:
            continue
        merged = merge_job_record_values(duplicate_records)
        keeper_id = int(merged["id"])
        supabase_request(
            "PATCH",
            "job_results",
            params={"id": f"eq.{keeper_id}"},
            json_body={
                "application_status": merged.get("application_status"),
                "status": merged.get("status"),
                "application_notes": merged.get("application_notes"),
                "notes": merged.get("notes"),
                "applied_date": merged.get("applied_date") or None,
                "relevance_score": int(merged.get("relevance_score") or 0),
                "cv_match_score": int(merged.get("cv_match_score") or 0),
                "relevance_reason": merged.get("relevance_reason"),
                "cv_match_reason": merged.get("cv_match_reason"),
                "salary": merged.get("salary"),
                "sponsorship_status": merged.get("sponsorship_status"),
                "sponsorship_reason": merged.get("sponsorship_reason"),
                "matched_sponsorship_phrase": merged.get("matched_sponsorship_phrase"),
                "first_seen_at": merged.get("first_seen_at"),
                "last_seen_at": merged.get("last_seen_at"),
            },
            prefer="return=minimal",
        )
        for record in duplicate_records:
            duplicate_id = int(record["id"])
            if duplicate_id != keeper_id:
                supabase_request(
                    "PATCH",
                    "job_results",
                    params={"id": f"eq.{duplicate_id}"},
                    json_body={"merged_into_id": keeper_id},
                    prefer="return=minimal",
                )
    st.session_state["job_fingerprint_migration_done"] = True


def search_google_jobs_query(api_key: str, query: str) -> list[dict]:
    params = {
        "engine": "google_jobs",
        "q": query,
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    response = requests.get(SERPAPI_ENDPOINT, params=params, timeout=45)
    response.raise_for_status()
    payload = response.json()
    return payload.get("jobs_results", [])


def search_google_jobs(
    api_key: str,
    company: str,
    job_title: str,
    max_jobs: int,
    max_query_variations: int,
) -> tuple[list[dict], int, int]:
    raw_jobs = []
    queries = make_job_search_queries(company, job_title)[:max(1, int(max_query_variations))]
    for query in queries:
        raw_jobs.extend(search_google_jobs_query(api_key, query))

    unique_jobs, duplicates_skipped = deduplicate_jobs(raw_jobs)
    return unique_jobs[:max_jobs], len(raw_jobs), duplicates_skipped


def save_job_results_supabase(
    run_id: str,
    run_started_at: str,
    company: str,
    searched_job_title: str,
    jobs: list[dict],
    relevance_threshold: int,
    include_broader_infrastructure: bool,
    cv_text: str = "",
) -> tuple[int, int, int]:
    created_at = utc_now()
    saved_count = 0
    skipped = 0
    cv_positive_count = 0
    for job in jobs:
        active_cv_text = clean_text(cv_text) or clean_text(st.session_state.get("cv_text", ""))
        title = clean_text(job.get("title"))
        description = clean_text(job.get("description"))
        relevance_score = calculate_relevance(title, description, include_broader_infrastructure)
        print("TITLE:", title, "SCORE:", relevance_score)
        _, relevance_reason, is_excluded = score_job_relevance(title, description, include_broader_infrastructure)
        if is_excluded:
            skipped += 1
            continue
        if relevance_score < relevance_threshold:
            skipped += 1
            continue

        employer_name = get_employer_name(job)
        location = clean_text(job.get("location"))
        apply_link = best_apply_link(job)
        job_url = normalize_job_url(job)
        job_fingerprint = job_fingerprint_from_values(employer_name, title, location, apply_link, job_url)
        posted_at = clean_text(job.get("detected_extensions", {}).get("posted_at") or job.get("posted_at"))
        salary = get_salary(job)
        application_text = collect_application_text(job)
        sponsorship_status, sponsorship_reason, matched_sponsorship_phrase = detect_sponsorship_status(title, description, application_text)
        cv_match_score, cv_match_reason = calculate_cv_match(
            title,
            employer_name,
            description,
            searched_job_title,
            active_cv_text,
        )
        if cv_match_score > 0:
            cv_positive_count += 1

        existing_rows = supabase_request(
            "GET",
            "job_results",
            params={
                "select": ",".join(RESULT_COLUMNS),
                "job_fingerprint": f"eq.{job_fingerprint}",
                "merged_into_id": "is.null",
                "limit": 1,
            },
        )
        if existing_rows:
            existing = existing_rows[0]
            updated_sponsorship, updated_sponsorship_reason, updated_sponsorship_phrase = choose_sponsorship_update(
                existing,
                sponsorship_status,
                sponsorship_reason,
                matched_sponsorship_phrase,
            )
            update_payload = {
                "run_id": run_id,
                "run_started_at": run_started_at,
                "last_seen_at": run_started_at,
                "posted_at": best_posted_at(existing.get("posted_at"), posted_at, existing.get("created_at"), created_at),
                "salary": clean_text(existing.get("salary")) or salary,
                "sponsorship_status": updated_sponsorship,
                "sponsorship_reason": updated_sponsorship_reason,
                "matched_sponsorship_phrase": updated_sponsorship_phrase,
                "relevance_score": max_numeric(existing.get("relevance_score"), relevance_score),
                "cv_match_score": max_numeric(existing.get("cv_match_score"), cv_match_score),
                "raw_json": job,
            }
            if int(update_payload["relevance_score"]) > int(pd.to_numeric(pd.Series([existing.get("relevance_score")]), errors="coerce").fillna(0).iloc[0]):
                update_payload["relevance_reason"] = relevance_reason
            if int(update_payload["cv_match_score"]) > int(pd.to_numeric(pd.Series([existing.get("cv_match_score")]), errors="coerce").fillna(0).iloc[0]):
                update_payload["cv_match_reason"] = cv_match_reason
            if not clean_text(existing.get("description")) and description:
                update_payload["description"] = description
            supabase_request(
                "PATCH",
                "job_results",
                params={"id": f"eq.{existing.get('id')}"},
                json_body=update_payload,
                prefer="return=minimal",
            )
            saved_count += 1
            continue

        supabase_request(
            "POST",
            "job_results",
            json_body={
                "result_key": make_result_key(company, searched_job_title, job),
                "job_fingerprint": job_fingerprint,
                "run_id": run_id,
                "run_started_at": run_started_at,
                "first_seen_at": run_started_at,
                "last_seen_at": run_started_at,
                "company": company,
                "searched_job_title": searched_job_title,
                "title": title,
                "employer_name": employer_name,
                "location": location,
                "via": clean_text(job.get("via")),
                "posted_at": posted_at,
                "schedule_type": clean_text(job.get("detected_extensions", {}).get("schedule_type")),
                "salary": salary,
                "description": description,
                "job_url": job_url,
                "apply_link": apply_link,
                "sponsorship_status": sponsorship_status,
                "sponsorship_reason": sponsorship_reason,
                "matched_sponsorship_phrase": matched_sponsorship_phrase,
                "relevance_score": int(relevance_score),
                "relevance_reason": relevance_reason,
                "cv_match_score": int(cv_match_score),
                "cv_match_reason": cv_match_reason,
                "notes": "",
                "application_status": "New",
                "applied_date": None,
                "application_notes": "",
                "raw_json": job,
                "created_at": created_at,
            },
            prefer="return=minimal",
        )
        saved_count += 1
    return saved_count, skipped, cv_positive_count


def save_job_results(
    run_id: str,
    run_started_at: str,
    company: str,
    searched_job_title: str,
    jobs: list[dict],
    relevance_threshold: int,
    include_broader_infrastructure: bool,
    cv_text: str = "",
) -> tuple[int, int, int]:
    if is_supabase_connected():
        return save_job_results_supabase(
            run_id,
            run_started_at,
            company,
            searched_job_title,
            jobs,
            relevance_threshold,
            include_broader_infrastructure,
            cv_text,
        )

    created_at = utc_now()
    inserted = 0
    skipped = 0
    cv_positive_count = 0
    with get_connection() as conn:
        for job in jobs:
            active_cv_text = clean_text(cv_text) or clean_text(st.session_state.get("cv_text", ""))
            title = clean_text(job.get("title"))
            description = clean_text(job.get("description"))
            relevance_score = calculate_relevance(title, description, include_broader_infrastructure)
            print("TITLE:", title, "SCORE:", relevance_score)
            _, relevance_reason, is_excluded = score_job_relevance(title, description, include_broader_infrastructure)
            if is_excluded:
                skipped += 1
                continue
            if relevance_score < relevance_threshold:
                skipped += 1
                continue

            employer_name = get_employer_name(job)
            location = clean_text(job.get("location"))
            apply_link = best_apply_link(job)
            job_url = normalize_job_url(job)
            job_fingerprint = job_fingerprint_from_values(employer_name, title, location, apply_link, job_url)
            posted_at = clean_text(job.get("detected_extensions", {}).get("posted_at") or job.get("posted_at"))
            salary = get_salary(job)
            application_text = collect_application_text(job)
            sponsorship_status, sponsorship_reason, matched_sponsorship_phrase = detect_sponsorship_status(title, description, application_text)
            cv_match_score, cv_match_reason = calculate_cv_match(
                title,
                employer_name,
                description,
                searched_job_title,
                active_cv_text,
            )
            if cv_match_score > 0:
                cv_positive_count += 1

            existing = conn.execute(
                "SELECT * FROM job_results WHERE job_fingerprint = ? AND merged_into_id IS NULL LIMIT 1",
                (job_fingerprint,),
            ).fetchone()
            if existing:
                existing_record = dict(existing)
                existing_relevance = max_numeric(existing_record.get("relevance_score"))
                existing_cv = max_numeric(existing_record.get("cv_match_score"))
                updated_relevance = max_numeric(existing_relevance, relevance_score)
                updated_cv = max_numeric(existing_cv, cv_match_score)
                updated_sponsorship, updated_sponsorship_reason, updated_sponsorship_phrase = choose_sponsorship_update(
                    existing_record,
                    sponsorship_status,
                    sponsorship_reason,
                    matched_sponsorship_phrase,
                )
                conn.execute(
                    """
                    UPDATE job_results
                    SET
                        run_id = ?,
                        run_started_at = ?,
                        last_seen_at = ?,
                        posted_at = ?,
                        salary = ?,
                        description = ?,
                        sponsorship_status = ?,
                        sponsorship_reason = ?,
                        matched_sponsorship_phrase = ?,
                        relevance_score = ?,
                        relevance_reason = ?,
                        cv_match_score = ?,
                        cv_match_reason = ?,
                        raw_json = ?
                    WHERE id = ?
                    """,
                    (
                        run_id,
                        run_started_at,
                        run_started_at,
                        best_posted_at(existing_record.get("posted_at"), posted_at, existing_record.get("created_at"), created_at),
                        clean_text(existing_record.get("salary")) or salary,
                        clean_text(existing_record.get("description")) or description,
                        updated_sponsorship,
                        updated_sponsorship_reason,
                        updated_sponsorship_phrase,
                        updated_relevance,
                        relevance_reason if updated_relevance > existing_relevance else existing_record.get("relevance_reason"),
                        updated_cv,
                        cv_match_reason if updated_cv > existing_cv else existing_record.get("cv_match_reason"),
                        json.dumps(job),
                        existing_record.get("id"),
                    ),
                )
                inserted += 1
                continue

            conn.execute(
                """
                INSERT INTO job_results (
                    result_key,
                    job_fingerprint,
                    run_id,
                    run_started_at,
                    first_seen_at,
                    last_seen_at,
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
                    sponsorship_reason,
                    matched_sponsorship_phrase,
                    relevance_score,
                    relevance_reason,
                    cv_match_score,
                    cv_match_reason,
                    notes,
                    application_status,
                    applied_date,
                    application_notes,
                    raw_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    make_result_key(company, searched_job_title, job),
                    job_fingerprint,
                    run_id,
                    run_started_at,
                    run_started_at,
                    run_started_at,
                    company,
                    searched_job_title,
                    title,
                    employer_name,
                    location,
                    clean_text(job.get("via")),
                    posted_at,
                    clean_text(job.get("detected_extensions", {}).get("schedule_type")),
                    salary,
                    description,
                    job_url,
                    apply_link,
                    sponsorship_status,
                    sponsorship_reason,
                    matched_sponsorship_phrase,
                    relevance_score,
                    relevance_reason,
                    cv_match_score,
                    cv_match_reason,
                    "",
                    "New",
                    "",
                    "",
                    json.dumps(job),
                    created_at,
                ),
            )
            inserted += 1
    return inserted, skipped, cv_positive_count


def top_jobs_per_run(df: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    if df.empty:
        return df
    sorted_df = df.sort_values(
        by=["run_started_at", "created_at", "relevance_score"],
        ascending=[False, False, False],
        na_position="last",
    )
    return sorted_df.groupby("run_id", dropna=False, group_keys=False).head(limit)


def parse_salary_value(salary: object) -> float:
    text = clean_text(salary).lower()
    if not text:
        return 0.0

    values = []
    for raw_value in re.findall(r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*([kKmM]?)", text):
        number_text, suffix = raw_value
        try:
            value = float(number_text.replace(",", ""))
        except ValueError:
            continue
        if suffix.lower() == "k":
            value *= 1_000
        elif suffix.lower() == "m":
            value *= 1_000_000
        values.append(value)

    return max(values) if values else 0.0


def parse_recency_score(posted_at: object, created_at: object) -> float:
    text = clean_text(posted_at).lower()
    if "today" in text or "just posted" in text or "hour" in text or "minute" in text:
        return 10_000.0
    if "yesterday" in text:
        return 9_999.0

    match = re.search(r"(\d+)\s+(day|week|month|year)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        days = amount
        if unit == "week":
            days *= 7
        elif unit == "month":
            days *= 30
        elif unit == "year":
            days *= 365
        return max(0.0, 10_000.0 - days)

    parsed_created_at = pd.to_datetime(clean_text(created_at), errors="coerce", utc=True)
    if pd.isna(parsed_created_at):
        return 0.0
    now = pd.Timestamp.now(tz="UTC")
    age_days = max(0.0, (now - parsed_created_at).total_seconds() / 86400)
    return max(0.0, 10_000.0 - age_days)


def top_best_matches(df: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    if df.empty:
        return df
    ranked = df.copy()
    ranked["_salary_sort"] = ranked["salary"].map(parse_salary_value)
    if "cv_match_score" not in ranked.columns:
        ranked["cv_match_score"] = pd.NA
    ranked["_cv_match_sort"] = cv_match_numeric_series(ranked).fillna(0)
    ranked["_relevance_sort"] = pd.to_numeric(ranked.get("relevance_score", 0), errors="coerce").fillna(0)
    ranked["_recency_sort"] = ranked.apply(
        lambda row: parse_recency_score(row.get("posted_at"), row.get("created_at")),
        axis=1,
    )
    ranked = ranked.sort_values(
        by=["_cv_match_sort", "_relevance_sort", "_salary_sort", "_recency_sort"],
        ascending=[False, False, False, False],
        na_position="last",
    )
    return ranked.head(limit).drop(columns=["_cv_match_sort", "_relevance_sort", "_salary_sort", "_recency_sort"], errors="ignore")


def deduplicate_top_matches(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    required_columns = {"title", "employer_name", "location"}
    if not required_columns.issubset(df.columns):
        return df

    deduped = df.copy()
    deduped["_duplicate_title"] = deduped["title"].map(lambda value: clean_text(value).lower())
    deduped["_duplicate_employer"] = deduped["employer_name"].map(lambda value: clean_text(value).lower())
    deduped["_duplicate_location"] = deduped["location"].map(lambda value: clean_text(value).lower())
    deduped["_dedupe_relevance_score"] = pd.to_numeric(deduped.get("relevance_score", 0), errors="coerce").fillna(0)
    deduped["_dedupe_cv_match_score"] = pd.to_numeric(deduped.get("cv_match_score", 0), errors="coerce").fillna(0)
    deduped["_dedupe_recency_score"] = deduped.apply(
        lambda row: parse_recency_score(row.get("posted_at"), row.get("created_at")),
        axis=1,
    )
    deduped = deduped.sort_values(
        by=["_dedupe_relevance_score", "_dedupe_cv_match_score", "_dedupe_recency_score"],
        ascending=[False, False, False],
        na_position="last",
    )
    deduped = deduped.drop_duplicates(
        subset=["_duplicate_title", "_duplicate_employer", "_duplicate_location"],
        keep="first",
    )
    return deduped.drop(
        columns=[
            "_duplicate_title",
            "_duplicate_employer",
            "_duplicate_location",
            "_dedupe_relevance_score",
            "_dedupe_cv_match_score",
            "_dedupe_recency_score",
        ],
        errors="ignore",
    )


def dataframe_to_excel_bytes(df: pd.DataFrame) -> bytes:
    from io import BytesIO

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="job_results")
    return output.getvalue()


def tables_to_excel_bytes(tables: dict[str, pd.DataFrame]) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        wrote_sheet = False
        for table_name, df in tables.items():
            if df.empty:
                continue
            sheet_name = table_name[:31]
            df.to_excel(writer, index=False, sheet_name=sheet_name)
            wrote_sheet = True
        if not wrote_sheet:
            pd.DataFrame({"message": ["No recovered data"]}).to_excel(writer, index=False, sheet_name="summary")
    return output.getvalue()


def find_local_sqlite_databases() -> list[Path]:
    candidates = []
    for path in APP_DIR.rglob("*.db"):
        if path.is_file():
            candidates.append(path)
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def sqlite_table_names(db_path: Path) -> set[str]:
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows}


def load_recoverable_sqlite_data(db_path: Path) -> dict[str, pd.DataFrame]:
    available_tables = sqlite_table_names(db_path)
    recovered = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for table_name in RECOVERY_TABLES:
                if table_name in available_tables:
                    recovered[table_name] = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
                else:
                    recovered[table_name] = pd.DataFrame()
    except sqlite3.Error:
        return {table_name: pd.DataFrame() for table_name in RECOVERY_TABLES}
    return recovered


def recovered_counts(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    return {
        "companies": len(tables.get("companies", pd.DataFrame())),
        "job_titles": len(tables.get("target_job_titles", pd.DataFrame())),
        "jobs": len(tables.get("job_results", pd.DataFrame())),
        "search_runs": len(tables.get("search_runs", pd.DataFrame())),
        "application_tracker": len(tables.get("application_tracker", pd.DataFrame())),
        "notes": len(tables.get("notes", pd.DataFrame())),
    }


def records_from_dataframe(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    prepared = df.where(pd.notna(df), None)
    return prepared.to_dict(orient="records")


def parse_json_column(records: list[dict], column_name: str) -> list[dict]:
    for record in records:
        value = record.get(column_name)
        if isinstance(value, str) and value.strip():
            try:
                record[column_name] = json.loads(value)
            except json.JSONDecodeError:
                record[column_name] = value
    return records


def migrate_recovered_data_to_supabase(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    migrated = {}
    table_conflicts = {
        "companies": "company",
        "target_job_titles": "job_title",
        "job_results": "result_key",
        "search_runs": "run_id",
        "application_tracker": "job_id",
        "notes": "job_id",
        "saved_profiles": "profile_name",
        "cv_profiles": "profile_key",
    }
    for table_name, conflict_column in table_conflicts.items():
        df = tables.get(table_name, pd.DataFrame())
        records = records_from_dataframe(df)
        if not records:
            migrated[table_name] = 0
            continue
        if table_name == "job_results":
            records = parse_json_column(records, "raw_json")
        if table_name == "saved_profiles":
            records = parse_json_column(records, "settings_json")
        supabase_upsert(table_name, records, conflict_column)
        migrated[table_name] = len(records)
    return migrated


BASE_EXPORT_COLUMNS = [
    "id",
    "job_fingerprint",
    "company",
    "title",
    "employer_name",
    "location",
    "salary",
    "posted_at",
    "first_seen_at",
    "last_seen_at",
    "schedule_type",
    "sponsorship_status",
    "cv_match_score",
    "relevance_score",
    "apply_link",
    "sponsorship_reason",
    "matched_sponsorship_phrase",
    "relevance_reason",
    "cv_match_reason",
    "application_status",
    "applied_date",
    "application_notes",
]
SEARCH_METADATA_EXPORT_COLUMNS = ["searched_job_title"]
TECHNICAL_EXPORT_COLUMNS = ["run_id", "run_started_at"]


def export_results_to_excel_bytes(
    df: pd.DataFrame,
    include_search_metadata: bool = False,
    include_technical_details: bool = False,
) -> bytes:
    export_df = prepare_cv_match_display(ensure_job_ids(df))
    export_columns = BASE_EXPORT_COLUMNS.copy()
    if include_search_metadata:
        export_columns.insert(2, "searched_job_title")
    if include_technical_details:
        export_columns.extend(TECHNICAL_EXPORT_COLUMNS)

    for column in export_columns:
        if column not in export_df.columns:
            export_df[column] = ""
    if "CV Match %" in export_df.columns:
        export_df["cv_match_score"] = export_df["CV Match %"]
    export_df = export_df[export_columns].rename(columns={"id": "Job ID"})
    export_df = export_df.rename(columns={"cv_match_score": "CV Match %"})
    return dataframe_to_excel_bytes(export_df)


def build_email_body(top_matches: pd.DataFrame) -> str:
    if top_matches.empty:
        return "No matching jobs are currently saved."

    lines = ["Top 20 best job matches", ""]
    for index, row in enumerate(top_matches.itertuples(index=False), start=1):
        lines.extend(
            [
                f"{index}. {clean_text(getattr(row, 'title', ''))}",
                f"Company: {clean_text(getattr(row, 'company', ''))}",
                f"Location: {clean_text(getattr(row, 'location', ''))}",
                f"Relevance: {clean_text(getattr(row, 'relevance_score', ''))}",
                f"CV match: {clean_text(getattr(row, 'cv_match_score', ''))}",
                f"Sponsorship: {clean_text(getattr(row, 'sponsorship_status', ''))}",
                f"Salary: {clean_text(getattr(row, 'salary', ''))}",
                f"Apply: {clean_text(getattr(row, 'apply_link', ''))}",
                "",
            ]
        )
    return "\n".join(lines)


def send_email_summary(recipient_email: str, top_matches: pd.DataFrame) -> tuple[bool, str]:
    try:
        smtp_host = st.secrets["SMTP_HOST"]
        smtp_port = int(st.secrets.get("SMTP_PORT", 587))
        smtp_user = st.secrets["SMTP_USERNAME"]
        smtp_password = st.secrets["SMTP_PASSWORD"]
        smtp_sender = st.secrets.get("SMTP_SENDER", smtp_user)
    except Exception:
        return False, "Missing SMTP secrets. Add SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, and optionally SMTP_SENDER."

    message = MIMEMultipart()
    message["From"] = smtp_sender
    message["To"] = recipient_email
    message["Subject"] = "Daily Job Search Top 20 Summary"
    message.attach(MIMEText(build_email_body(top_matches), "plain"))

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(message)
    except Exception as exc:
        return False, f"Email failed: {exc}"

    return True, "Email summary sent."


def is_sponsorship_available_status(status: object) -> bool:
    return normalize_sponsorship_status(status) == "sponsorship available"


SPONSORSHIP_STATUS_OPTIONS = (
    "All",
    "sponsorship available",
    "sponsorship not available",
    "not mentioned",
    "requires work authorization",
)

POSSIBLE_SPONSORSHIP_STATUSES = {"sponsorship available", "not mentioned"}


def normalize_sponsorship_status(status: object) -> str:
    text = clean_text(status).lower().replace("_", " ")
    if text in {"sponsorship available", "sponsorship not available", "not mentioned", "requires work authorization"}:
        return text
    return text or "not mentioned"


def apply_sponsorship_filter(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    if df.empty or "sponsorship_status" not in df.columns:
        return df
    status_filter = st.selectbox(
        "Sponsorship status",
        options=list(SPONSORSHIP_STATUS_OPTIONS),
        index=0,
        key=f"{key_prefix}_sponsorship_status_filter",
    )
    possible_only = st.checkbox(
        "Show only possible sponsorship",
        value=False,
        key=f"{key_prefix}_possible_sponsorship_only",
    )
    filtered = df.copy()
    normalized = filtered["sponsorship_status"].map(normalize_sponsorship_status)
    if status_filter != "All":
        filtered = filtered[normalized == status_filter]
        normalized = filtered["sponsorship_status"].map(normalize_sponsorship_status)
    if possible_only:
        filtered = filtered[normalized.isin(POSSIBLE_SPONSORSHIP_STATUSES)]
    return filtered


def normalized_application_status(value: object) -> str:
    status = clean_text(value) or "New"
    return status if status in APPLICATION_STATUSES else "New"


def cv_match_numeric_series(df: pd.DataFrame) -> pd.Series:
    if df.empty or "cv_match_score" not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="Float64")
    calculated = cv_match_calculated_mask(df)
    scores = pd.to_numeric(df["cv_match_score"], errors="coerce")
    return scores.where(calculated).astype("Float64")


def dashboard_master_table(df: pd.DataFrame, show_technical_details: bool = False) -> pd.DataFrame:
    source = prepare_cv_match_display(ensure_job_ids(df))
    for column in ["application_status", "application_notes", "sponsorship_reason", "matched_sponsorship_phrase"]:
        if column not in source.columns:
            source[column] = ""
    source["application_status"] = source["application_status"].map(normalized_application_status)
    source["application_notes"] = source["application_notes"].fillna("")
    sponsorship = source["sponsorship_status"].map(normalize_sponsorship_status) if "sponsorship_status" in source.columns else ""
    table = pd.DataFrame(
        {
            "Job ID": source.get("id", ""),
            "Company": source.get("company", ""),
            "Job Title": source.get("title", ""),
            "Employer": source.get("employer_name", ""),
            "Location": source.get("location", ""),
            "Salary": source.get("salary", ""),
            "Posted": source.get("posted_at", ""),
            "First Seen": source.get("first_seen_at", ""),
            "Last Seen": source.get("last_seen_at", ""),
            "Sponsorship Status": sponsorship,
            "Sponsorship Reason": source.get("sponsorship_reason", ""),
            "Matched Sponsorship Phrase": source.get("matched_sponsorship_phrase", ""),
            "CV Match %": source.get("CV Match %", ""),
            "Relevance Score": source.get("relevance_score", ""),
            "Apply Link": source.get("apply_link", ""),
            "Application Status": source.get("application_status", ""),
            "Notes": source.get("application_notes", ""),
        }
    )
    if show_technical_details:
        table["job_fingerprint"] = source.get("job_fingerprint", "")
        table["searched_job_title"] = source.get("searched_job_title", "")
        table["run_id"] = source.get("run_id", "")
        table["run_started_at"] = source.get("run_started_at", "")
    return table


def is_cv_match_calculated(row: pd.Series) -> bool:
    reason = clean_text(row.get("cv_match_reason")).lower()
    return bool(reason and reason not in {"no cv uploaded.", "not calculated"})


def cv_match_calculated_mask(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series([], index=df.index, dtype=bool)
    if "cv_match_calculated" in df.columns:
        raw = df["cv_match_calculated"]
        if raw.dtype == bool:
            return raw.fillna(False)
        return raw.map(lambda value: clean_text(value).lower() in {"true", "1", "yes", "calculated"})
    if "cv_match_reason" in df.columns:
        return df.apply(is_cv_match_calculated, axis=1).fillna(False).astype(bool)
    scores = pd.to_numeric(df.get("cv_match_score", pd.Series([pd.NA] * len(df), index=df.index)), errors="coerce")
    return scores.notna()


def cv_match_display_series(scores: pd.Series, calculated_mask: pd.Series) -> pd.Series:
    numeric_scores = pd.to_numeric(scores, errors="coerce")
    display = pd.Series([""] * len(numeric_scores), index=numeric_scores.index, dtype=object)
    display.loc[calculated_mask & numeric_scores.notna()] = numeric_scores.loc[calculated_mask & numeric_scores.notna()].round(0).astype("Int64").astype(str)
    display.loc[~calculated_mask] = "Not calculated"
    return display


def prepare_cv_match_display(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    if "cv_match_score" not in prepared.columns:
        prepared["cv_match_score"] = pd.NA
    if "cv_match_reason" not in prepared.columns:
        prepared["cv_match_reason"] = ""
    prepared["cv_match_score"] = pd.to_numeric(prepared["cv_match_score"], errors="coerce").astype("Float64")
    calculated_mask = cv_match_calculated_mask(prepared)
    prepared.loc[~calculated_mask, "cv_match_score"] = pd.NA
    prepared.loc[~calculated_mask, "cv_match_reason"] = "Not calculated"
    prepared["CV Match %"] = cv_match_display_series(prepared["cv_match_score"], calculated_mask)
    return prepared


def format_score(value: object) -> str:
    if clean_text(value) == "" or pd.isna(value):
        return "Not calculated"
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError):
        return clean_text(value)


def highlight_sponsorship_available(row: pd.Series) -> list[str]:
    if is_sponsorship_available_status(row.get("sponsorship_status")):
        return ["background-color: #d1fae5"] * len(row)
    return [""] * len(row)


def highlight_cv_match_score(row: pd.Series) -> list[str]:
    styles = [""] * len(row)
    if "cv_match_score" not in row.index:
        return styles
    score_value = pd.to_numeric(pd.Series([row.get("cv_match_score")]), errors="coerce").iloc[0]
    if pd.isna(score_value):
        return styles
    score = int(score_value)
    if score >= 80:
        color = "background-color: #bbf7d0; color: #166534; font-weight: 700"
    elif score >= 60:
        color = "background-color: #fed7aa; color: #9a3412; font-weight: 700"
    else:
        color = "background-color: #fecaca; color: #991b1b; font-weight: 700"
    styles[list(row.index).index("cv_match_score")] = color
    return styles


def highlight_relevance_score(row: pd.Series) -> list[str]:
    styles = [""] * len(row)
    if "relevance_score" not in row.index:
        return styles
    try:
        score = int(row.get("relevance_score") or 0)
    except (TypeError, ValueError):
        score = 0
    if score >= 3:
        color = "background-color: #bbf7d0; color: #166534; font-weight: 700"
    elif score == 2:
        color = "background-color: #fef08a; color: #854d0e; font-weight: 700"
    else:
        color = "background-color: #e5e7eb; color: #374151; font-weight: 700"
    styles[list(row.index).index("relevance_score")] = color
    return styles


def highlight_applied_status(row: pd.Series) -> list[str]:
    styles = [""] * len(row)
    if clean_text(row.get("application_status")) != "Applied":
        return styles
    if "application_status" in row.index:
        styles[list(row.index).index("application_status")] = "background-color: #bbf7d0; font-weight: 700"
    return styles


def reorder_job_columns(df: pd.DataFrame, show_technical_details: bool = False) -> pd.DataFrame:
    df = ensure_job_ids(df)
    preferred_front_cols = [
        "id",
        "company",
        "title",
        "employer_name",
        "location",
        "salary",
        "posted_at",
        "schedule_type",
        "sponsorship_status",
        "cv_match_score",
        "relevance_score",
        "apply_link",
        "application_status",
        "application_notes",
        "applied_date",
    ]
    hidden_cols = ["searched_job_title", "run_id", "run_started_at"]
    end_cols = ["run_id", "run_started_at"] if show_technical_details else []
    front_cols = [column for column in preferred_front_cols if column in df.columns]
    middle_cols = [column for column in df.columns if column not in front_cols and column not in hidden_cols]
    existing_end_cols = [column for column in end_cols if column in df.columns]
    return df[front_cols + middle_cols + existing_end_cols]


def reorder_top_match_columns(df: pd.DataFrame, show_technical_details: bool = False) -> pd.DataFrame:
    df = ensure_job_ids(df)
    preferred_front_cols = [
        "id",
        "company",
        "title",
        "employer_name",
        "location",
        "salary",
        "posted_at",
        "schedule_type",
        "sponsorship_status",
        "cv_match_score",
        "relevance_score",
        "apply_link",
        "application_status",
        "application_notes",
        "applied_date",
    ]
    hidden_cols = ["searched_job_title", "run_id", "run_started_at"]
    end_cols = ["run_id", "run_started_at"] if show_technical_details else []
    front_cols = [column for column in preferred_front_cols if column in df.columns]
    middle_cols = [column for column in df.columns if column not in front_cols and column not in hidden_cols]
    existing_end_cols = [column for column in end_cols if column in df.columns]
    return df[front_cols + middle_cols + existing_end_cols]


def style_top_matches(df: pd.DataFrame):
    return (
        df.style.apply(highlight_sponsorship_available, axis=1)
        .apply(highlight_cv_match_score, axis=1)
        .apply(highlight_relevance_score, axis=1)
        .apply(highlight_applied_status, axis=1)
        .format({"cv_match_score": format_score, "relevance_score": format_score})
    )


def top_matches_column_config() -> dict:
    return {
        "Job ID": st.column_config.TextColumn("Job ID"),
        "cv_match_score": st.column_config.TextColumn("CV Match %"),
        "relevance_score": st.column_config.NumberColumn("Relevance", format="%d"),
        "job_url": st.column_config.LinkColumn("Job URL"),
        "apply_link": st.column_config.LinkColumn("Apply", display_text="Apply Now"),
        "cv_match_score": st.column_config.TextColumn("CV Match %"),
        "relevance_score": st.column_config.NumberColumn("Relevance", format="%d"),
        "sponsorship_reason": st.column_config.TextColumn("Sponsorship reason", width="medium"),
        "relevance_reason": st.column_config.TextColumn("Relevance reason", width="medium"),
        "cv_match_reason": st.column_config.TextColumn("CV match reason", width="medium"),
        "application_status": st.column_config.TextColumn("status"),
        "applied_date": st.column_config.TextColumn("Applied date"),
        "application_notes": st.column_config.TextColumn("notes", width="large"),
        "description": st.column_config.TextColumn("Description", width="large"),
    }


def render_upload_section() -> None:
    st.subheader("Upload targets")
    updated_message = st.session_state.pop("targets_updated_message", None)
    if updated_message:
        st.success(
            f"{updated_message['mode']}: {updated_message['companies']} companies, "
            f"{updated_message['job_titles']} job titles, "
            f"{updated_message['combinations']} total search combinations."
        )

    existing_companies = get_companies()
    existing_job_titles = get_target_job_titles()
    st.caption(
        f"Current saved targets: {len(existing_companies)} companies, "
        f"{len(existing_job_titles)} job titles, "
        f"{len(existing_companies) * len(existing_job_titles)} combinations."
    )

    with st.expander("Clear all targets"):
        st.warning("This clears only company and job-title target lists. Historical job results and tracker data are not deleted.")
        confirm_clear = st.checkbox("I understand this clears all saved targets", key="confirm_clear_targets")
        if st.button("Clear all targets", disabled=not confirm_clear):
            clear_targets()
            reset_search_settings_after_target_change(0, 0)
            st.session_state["targets_updated_message"] = {
                "mode": "Cleared targets",
                "companies": 0,
                "job_titles": 0,
                "combinations": 0,
            }
            st.rerun()

    upload_mode = st.radio(
        "Upload mode",
        options=["Replace existing targets", "Append to existing targets"],
        index=0,
        horizontal=True,
    )
    uploaded_file = st.file_uploader(
        "Excel file with companies in Column A and target job titles in Column B",
        type=["xlsx"],
    )

    if uploaded_file is None:
        return

    try:
        companies_df, job_titles_df = load_targets_from_excel(uploaded_file)
    except Exception as exc:
        st.error(f"Could not read Excel file: {exc}")
        return

    if companies_df.empty or job_titles_df.empty:
        st.warning("No valid companies or job titles were found.")
        return

    preview_cols = st.columns(2)
    with preview_cols[0]:
        st.metric("Companies found", len(companies_df))
        st.dataframe(companies_df, use_container_width=True, hide_index=True)
    with preview_cols[1]:
        st.metric("Job titles found", len(job_titles_df))
        st.dataframe(job_titles_df, use_container_width=True, hide_index=True)
    st.metric("Total search combinations", len(companies_df) * len(job_titles_df))
    if len(companies_df) <= 3:
        st.warning("Please verify Column A contains the full company list.")

    if st.button("Save Uploaded Targets", type="primary"):
        replace_existing = upload_mode == "Replace existing targets"
        inserted_companies, inserted_job_titles = save_targets(companies_df, job_titles_df, replace_existing=replace_existing)
        current_companies = get_companies()
        current_job_titles = get_target_job_titles()
        reset_search_settings_after_target_change(len(current_companies), len(current_job_titles))
        st.session_state["targets_updated_message"] = {
            "mode": upload_mode,
            "companies": len(current_companies),
            "job_titles": len(current_job_titles),
            "combinations": len(current_companies) * len(current_job_titles),
        }
        st.rerun()


def clamp_int(value: object, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


SEARCH_PROFILE_OPTIONS = ["Full Search", "Balanced Mode", "Safe Mode"]


def full_search_default_settings(company_count: int, job_title_count: int, max_query_variations: int) -> dict[str, int]:
    return {
        "max_companies_per_run": max(1, company_count),
        "max_job_titles_per_run": max(1, job_title_count),
        "max_query_variations": min(4, max_query_variations),
        "max_jobs_per_target": 20,
    }


def reset_search_settings_after_target_change(company_count: int, job_title_count: int) -> None:
    max_query_variations = len(make_job_search_queries("Company", "Job Title"))
    defaults = full_search_default_settings(company_count, job_title_count, max_query_variations)
    apply_search_settings(defaults, company_count, job_title_count, max_query_variations)
    st.session_state["active_search_profile"] = "Full Search"
    st.session_state["safe_mode_checkbox"] = False
    if is_supabase_connected():
        save_search_profile("Full Search", defaults)


def search_profile_defaults(profile_name: str, company_count: int, job_title_count: int, max_query_variations: int) -> dict[str, int]:
    if profile_name == "Balanced Mode":
        return {
            "max_companies_per_run": min(5, max(1, company_count)),
            "max_job_titles_per_run": min(5, max(1, job_title_count)),
            "max_query_variations": min(4, max_query_variations),
            "max_jobs_per_target": 10,
        }
    if profile_name in {"Full Search", "Full / Maximum Mode"}:
        saved_settings = get_search_profile("Full Search")
        if saved_settings:
            return saved_settings
        return full_search_default_settings(company_count, job_title_count, max_query_variations)
    return {
        "max_companies_per_run": min(2, max(1, company_count)),
        "max_job_titles_per_run": min(2, max(1, job_title_count)),
        "max_query_variations": min(2, max_query_variations),
        "max_jobs_per_target": 5,
    }


def apply_search_settings(settings: dict[str, int], company_count: int, job_title_count: int, max_query_variations: int) -> None:
    st.session_state["max_companies_per_run"] = clamp_int(settings.get("max_companies_per_run"), 1, max(1, company_count))
    st.session_state["max_job_titles_per_run"] = clamp_int(settings.get("max_job_titles_per_run"), 1, max(1, job_title_count))
    st.session_state["max_query_variations"] = clamp_int(settings.get("max_query_variations"), 1, max_query_variations)
    st.session_state["max_jobs_per_target"] = clamp_int(settings.get("max_jobs_per_target"), 3, 20)


def current_search_settings() -> dict[str, int]:
    return {
        "max_companies_per_run": int(st.session_state.get("max_companies_per_run", 1)),
        "max_job_titles_per_run": int(st.session_state.get("max_job_titles_per_run", 1)),
        "max_query_variations": int(st.session_state.get("max_query_variations", 1)),
        "max_jobs_per_target": int(st.session_state.get("max_jobs_per_target", 5)),
    }


def valid_saved_values(values: object, options: list[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    option_set = set(options)
    return [clean_text(value) for value in values if clean_text(value) in option_set]


def apply_pending_target_selection(company_options: list[str], job_title_options: list[str]) -> None:
    pending_selection = st.session_state.pop("pending_target_selection", None)
    if not pending_selection:
        return
    if "companies" in pending_selection:
        st.session_state["selected_companies_filter"] = valid_saved_values(pending_selection["companies"], company_options)
    if "job_titles" in pending_selection:
        st.session_state["selected_job_titles_filter"] = valid_saved_values(pending_selection["job_titles"], job_title_options)


def initialize_target_selection(company_options: list[str], job_title_options: list[str]) -> None:
    apply_pending_target_selection(company_options, job_title_options)
    if "selected_companies_filter" not in st.session_state or "selected_job_titles_filter" not in st.session_state:
        saved_selection = get_search_profile("Last Search Selection")
        st.session_state.setdefault(
            "selected_companies_filter",
            valid_saved_values(saved_selection.get("companies"), company_options),
        )
        st.session_state.setdefault(
            "selected_job_titles_filter",
            valid_saved_values(saved_selection.get("job_titles"), job_title_options),
        )


def save_last_target_selection(companies_selected: list[str], job_titles_selected: list[str]) -> None:
    payload = {"companies": companies_selected, "job_titles": job_titles_selected}
    if st.session_state.get("last_saved_target_selection") == payload:
        return
    save_search_profile("Last Search Selection", payload)
    st.session_state["last_saved_target_selection"] = payload


def render_search_section(companies: pd.DataFrame, job_titles: pd.DataFrame) -> None:
    st.subheader("Run search")
    api_key = get_serpapi_key()
    company_count = len(companies) if companies is not None else 0
    job_title_count = len(job_titles) if job_titles is not None else 0
    saved_target_count = company_count * job_title_count

    st.markdown("### API Usage / Quota")
    account_usage = st.session_state.get(
        "serpapi_account_usage",
        {"monthly_used": None, "monthly_limit": None, "searches_remaining": None},
    )
    if not api_key:
        st.warning("Missing SERPAPI_API_KEY in Streamlit secrets")
    if st.button("Check SerpAPI quota", disabled=not bool(api_key)):
        try:
            account_usage = get_serpapi_account_usage(api_key)
            st.session_state["serpapi_account_usage"] = account_usage
        except requests.RequestException as exc:
            st.warning(f"Could not check SerpAPI account usage: {exc}")

    usage_cols = st.columns(3)
    usage_cols[0].metric("Monthly searches used", quota_value(account_usage, "monthly_used"))
    usage_cols[1].metric("Monthly search limit", quota_value(account_usage, "monthly_limit"))
    usage_cols[2].metric("Searches remaining", quota_value(account_usage, "searches_remaining"))

    st.markdown("### Usage controls")
    max_query_variation_count = len(make_job_search_queries("Company", "Job Title"))
    pending_profile = st.session_state.pop("pending_search_profile", "")
    if pending_profile:
        apply_search_settings(
            search_profile_defaults(pending_profile, company_count, job_title_count, max_query_variation_count),
            company_count,
            job_title_count,
            max_query_variation_count,
        )
        st.session_state["active_search_profile"] = pending_profile
    if st.session_state.pop("pending_restore_full_search_defaults", False):
        defaults = full_search_default_settings(company_count, job_title_count, max_query_variation_count)
        apply_search_settings(defaults, company_count, job_title_count, max_query_variation_count)
        save_search_profile("Full Search", defaults)
        st.session_state["active_search_profile"] = "Full Search"
        st.session_state["safe_mode_checkbox"] = False

    if "active_search_profile" not in st.session_state:
        st.session_state["active_search_profile"] = "Full Search"
        apply_search_settings(
            search_profile_defaults("Full Search", company_count, job_title_count, max_query_variation_count),
            company_count,
            job_title_count,
            max_query_variation_count,
        )

    active_profile = st.session_state.get("active_search_profile", "Full Search")
    if active_profile not in SEARCH_PROFILE_OPTIONS:
        active_profile = "Full Search"
        st.session_state["active_search_profile"] = active_profile

    selected_profile = st.selectbox(
        "Search profile",
        options=SEARCH_PROFILE_OPTIONS,
        index=SEARCH_PROFILE_OPTIONS.index(active_profile),
    )
    if st.session_state.get("active_search_profile") != selected_profile:
        apply_search_settings(
            search_profile_defaults(selected_profile, company_count, job_title_count, max_query_variation_count),
            company_count,
            job_title_count,
            max_query_variation_count,
        )
        st.session_state["active_search_profile"] = selected_profile

    safe_mode = st.checkbox(
        "Safe mode - reduce API usage",
        value=selected_profile == "Safe Mode",
        key="safe_mode_checkbox",
    )
    if safe_mode and selected_profile != "Safe Mode":
        st.info("Safe Mode is available as an optional fallback. Select `Safe Mode` from Search profile to apply it.")

    st.caption("Full Search is the default. Quota warnings will not reduce search scope automatically.")

    control_cols = st.columns(4)
    max_companies_per_run = control_cols[0].number_input(
        "Max companies per run",
        min_value=1,
        max_value=max(1, company_count),
        step=1,
        key="max_companies_per_run",
    )
    max_job_titles_per_run = control_cols[1].number_input(
        "Max job titles per run",
        min_value=1,
        max_value=max(1, job_title_count),
        step=1,
        key="max_job_titles_per_run",
    )
    max_query_variations = control_cols[2].number_input(
        "Max query variations per company/title",
        min_value=1,
        max_value=max_query_variation_count,
        step=1,
        key="max_query_variations",
    )
    max_jobs_per_target = control_cols[3].number_input(
        "Max results per company/title",
        min_value=3,
        max_value=20,
        step=1,
        key="max_jobs_per_target",
    )
    profile_cols = st.columns(2)
    if profile_cols[0].button("Save current settings as Full Search"):
        save_search_profile("Full Search", current_search_settings())
        st.success("Saved current settings as Full Search.")
    if profile_cols[1].button("Load Full Search settings"):
        st.session_state["pending_search_profile"] = "Full Search"
        st.success("Loaded Full Search settings. Search was not run.")
        st.rerun()
    if st.button("Restore My Full Search Defaults"):
        st.session_state["pending_restore_full_search_defaults"] = True
        st.success("Restored and saved Full Search defaults.")
        st.rerun()

    company_options = companies["company"].map(clean_text).drop_duplicates().tolist() if companies is not None and "company" in companies.columns else []
    job_title_options = job_titles["job_title"].map(clean_text).drop_duplicates().tolist() if job_titles is not None and "job_title" in job_titles.columns else []
    initialize_target_selection(company_options, job_title_options)

    st.markdown("### Manual search selection")
    st.caption("Nothing selected means search all companies and all job titles.")
    selection_cols = st.columns(2)
    with selection_cols[0]:
        selected_company_values = st.multiselect(
            "Companies",
            options=company_options,
            key="selected_companies_filter",
        )
    with selection_cols[1]:
        selected_job_title_values = st.multiselect(
            "Job Titles",
            options=job_title_options,
            key="selected_job_titles_filter",
        )

    quick_cols = st.columns(4)
    if quick_cols[0].button("Select All"):
        st.session_state["pending_target_selection"] = {
            "companies": company_options,
            "job_titles": job_title_options,
        }
        st.rerun()
    if quick_cols[1].button("Clear All"):
        st.session_state["pending_target_selection"] = {"companies": [], "job_titles": []}
        st.rerun()
    if quick_cols[2].button("Save Priority Companies"):
        save_search_profile("Priority Companies", {"companies": selected_company_values})
        st.success("Saved selected companies as Priority Companies.")
    if quick_cols[3].button("Load Priority Companies"):
        preset = get_search_profile("Priority Companies")
        st.session_state["pending_target_selection"] = {"companies": preset.get("companies", [])}
        st.rerun()

    role_cols = st.columns(2)
    if role_cols[0].button("Save Priority Roles"):
        save_search_profile("Priority Roles", {"job_titles": selected_job_title_values})
        st.success("Saved selected job titles as Priority Roles.")
    if role_cols[1].button("Load Priority Roles"):
        preset = get_search_profile("Priority Roles")
        st.session_state["pending_target_selection"] = {"job_titles": preset.get("job_titles", [])}
        st.rerun()

    save_last_target_selection(selected_company_values, selected_job_title_values)

    filtered_companies = companies
    if selected_company_values:
        filtered_companies = companies[companies["company"].map(clean_text).isin(selected_company_values)]
    filtered_job_titles = job_titles
    if selected_job_title_values:
        filtered_job_titles = job_titles[job_titles["job_title"].map(clean_text).isin(selected_job_title_values)]

    selected_companies = filtered_companies.head(int(max_companies_per_run)) if filtered_companies is not None else pd.DataFrame()
    selected_job_titles = filtered_job_titles.head(int(max_job_titles_per_run)) if filtered_job_titles is not None else pd.DataFrame()
    selected_targets = build_search_combinations(selected_companies, selected_job_titles)
    active_target_count = len(selected_targets)
    estimated_api_calls = active_target_count * int(max_query_variations)
    searches_remaining = account_usage.get("searches_remaining")

    estimate_cols = st.columns(4)
    estimate_cols[0].metric("Companies this run", len(selected_companies))
    estimate_cols[1].metric("Job titles this run", len(selected_job_titles))
    estimate_cols[2].metric("Company/title combos", active_target_count)
    estimate_cols[3].metric("Estimated API calls", estimated_api_calls)

    if searches_remaining is not None and searches_remaining <= max(10, estimated_api_calls * 2):
        st.warning("Low SerpAPI quota. Reduce search size.")

    disable_reasons = []
    if not api_key:
        disable_reasons.append("Missing SERPAPI_API_KEY in Streamlit secrets")
    if company_count == 0 or job_title_count == 0:
        disable_reasons.append("Please upload targets first in Upload Targets tab")
    elif active_target_count == 0:
        disable_reasons.append("No active company/title pairs")
    if searches_remaining is not None and estimated_api_calls > searches_remaining:
        disable_reasons.append("Estimated API calls exceed searches remaining")

    disabled_reason = "; ".join(disable_reasons) if disable_reasons else "Button enabled"
    disabled = bool(disable_reasons)

    if company_count == 0 or job_title_count == 0:
        st.info("Please upload targets first in Upload Targets tab")
    if searches_remaining is not None and estimated_api_calls > searches_remaining:
        st.error("Estimated API calls are greater than your remaining SerpAPI quota. Reduce search size before running.")

    st.markdown("### Debug")
    debug_cols = st.columns(4)
    debug_cols[0].metric("Total companies loaded", company_count)
    debug_cols[1].metric("Total job titles loaded", job_title_count)
    debug_cols[2].metric("Total search combinations", saved_target_count)
    debug_cols[3].metric("SerpAPI key exists", "yes" if api_key else "no")
    st.caption(f"Reason button is disabled: {disabled_reason}")

    st.sidebar.header("Search settings")
    include_broader_infrastructure = st.sidebar.checkbox(
        "Include broader infrastructure management jobs",
        value=True,
    )
    if "last_search_counters" in st.session_state:
        counters = st.session_state["last_search_counters"]
        st.caption(f"Last search run: {counters['run_id']}")
        counter_cols = st.columns(4)
        counter_cols[0].metric("Total raw jobs found", counters["total_raw_jobs"])
        counter_cols[1].metric("Duplicates skipped", counters["duplicates_skipped"])
        counter_cols[2].metric("Excluded by relevance", counters["excluded_by_relevance"])
        counter_cols[3].metric("Jobs saved", counters["jobs_saved"])
        cv_counter_cols = st.columns(3)
        cv_counter_cols[0].metric("CV text loaded", counters.get("cv_text_loaded", "no"))
        cv_counter_cols[1].metric("CV text length", counters.get("cv_text_length", 0))
        cv_counter_cols[2].metric("Jobs with CV match > 0", counters.get("jobs_with_cv_match", 0))

    if st.button("Run Job Search", type="primary", disabled=disabled):
        try:
            account_usage = get_serpapi_account_usage(api_key)
            st.session_state["serpapi_account_usage"] = account_usage
            searches_remaining = account_usage.get("searches_remaining")
        except requests.RequestException as exc:
            st.error(f"Could not check SerpAPI quota before running search: {exc}")
            return
        if searches_remaining is not None and estimated_api_calls > searches_remaining:
            st.error("Estimated API calls are greater than your remaining SerpAPI quota. Reduce search size before running.")
            return

        run_id = make_run_id()
        run_started_at = utc_now()
        progress = st.progress(0)
        status = st.empty()
        total_inserted = 0
        total_raw_jobs = 0
        total_duplicates_skipped = 0
        total_excluded_by_relevance = 0
        total_jobs_with_cv_match = 0
        cv_text_for_run = clean_text(st.session_state.get("cv_text", ""))

        for index, row in enumerate(selected_targets.itertuples(index=False), start=1):
            status.write(f"Searching {row.job_title} at {row.company}...")
            try:
                jobs, raw_jobs_found, duplicates_skipped = search_google_jobs(
                    api_key,
                    row.company,
                    row.job_title,
                    int(max_jobs_per_target),
                    int(max_query_variations),
                )
                inserted, excluded_by_relevance, jobs_with_cv_match = save_job_results(
                    run_id,
                    run_started_at,
                    row.company,
                    row.job_title,
                    jobs,
                    SAVE_RELEVANCE_THRESHOLD,
                    include_broader_infrastructure,
                    cv_text_for_run,
                )
                total_raw_jobs += raw_jobs_found
                total_duplicates_skipped += duplicates_skipped
                total_inserted += inserted
                total_excluded_by_relevance += excluded_by_relevance
                total_jobs_with_cv_match += jobs_with_cv_match
            except requests.HTTPError as exc:
                st.error(f"SerpAPI error for {row.company} / {row.job_title}: {exc}")
            except requests.RequestException as exc:
                st.error(f"Network error for {row.company} / {row.job_title}: {exc}")
            progress.progress(index / len(selected_targets))

        status.write("Search complete.")
        st.session_state["last_search_counters"] = {
            "run_id": run_id,
            "total_raw_jobs": total_raw_jobs,
            "duplicates_skipped": total_duplicates_skipped,
            "excluded_by_relevance": total_excluded_by_relevance,
            "jobs_saved": total_inserted,
            "cv_text_loaded": "yes" if cv_text_for_run else "no",
            "cv_text_length": len(cv_text_for_run),
            "jobs_with_cv_match": total_jobs_with_cv_match,
        }
        save_search_run(
            run_id,
            run_started_at,
            total_raw_jobs,
            total_duplicates_skipped,
            total_excluded_by_relevance,
            total_inserted,
        )
        st.success("Search complete. Results were saved and the dashboard will refresh.")
        st.rerun()


def render_dashboard(results: pd.DataFrame, companies: pd.DataFrame, job_titles: pd.DataFrame) -> None:
    st.subheader("Jobs Dashboard")
    tracking_message = st.session_state.pop("tracking_updated_message", None)
    if tracking_message:
        st.success(tracking_message)
    total_companies = len(companies) if companies is not None else 0
    total_job_titles = len(job_titles) if job_titles is not None else 0
    total_search_combinations = total_companies * total_job_titles
    target_cols = st.columns(3)
    target_cols[0].metric("Total companies loaded", total_companies)
    target_cols[1].metric("Total job titles loaded", total_job_titles)
    target_cols[2].metric("Total search combinations", total_search_combinations)

    if results.empty:
        st.info("No job results saved yet.")
        return

    working = ensure_job_ids(results).copy()
    if "application_status" not in working.columns:
        working["application_status"] = "New"
    if "application_notes" not in working.columns:
        working["application_notes"] = ""
    working["application_status"] = working["application_status"].map(normalized_application_status)
    working["application_notes"] = working["application_notes"].fillna("")

    col1, col2, col3 = st.columns(3)
    col1.metric("Saved jobs", len(results))
    col2.metric("Companies", results["company"].nunique())
    col3.metric("Sponsorship available", results["sponsorship_status"].map(is_sponsorship_available_status).sum())

    st.markdown("### Tracking Counts")
    st.caption("Tracking source: job_results.application_status and job_results.notes")
    status_counts = working["application_status"].value_counts().to_dict()
    status_count_cols = st.columns(6)
    for index, status_name in enumerate(["New", "Interested", "Applied", "Interview", "Offer", "Rejected"]):
        status_count_cols[index].metric(status_name, int(status_counts.get(status_name, 0)))

    st.markdown("### Filters")
    filter_cols = st.columns(3)
    company_filter = filter_cols[0].multiselect(
        "Company",
        options=sorted(working["company"].dropna().map(clean_text).unique()),
    )
    location_filter = filter_cols[1].multiselect(
        "Location",
        options=sorted(working["location"].dropna().map(clean_text).unique()),
    )
    application_status_filter = filter_cols[2].selectbox(
        "Application Status",
        options=["All", *APPLICATION_STATUSES],
    )

    filter_cols_2 = st.columns(4)
    sponsorship_status_filter = filter_cols_2[0].selectbox(
        "Sponsorship Status",
        options=list(SPONSORSHIP_STATUS_OPTIONS),
        index=0,
    )
    cv_minimum = filter_cols_2[1].slider("CV Match %", min_value=0, max_value=100, value=0, step=5)
    relevance_minimum = filter_cols_2[2].slider("Relevance Score", min_value=0, max_value=5, value=0, step=1)
    seen_filter = filter_cols_2[3].selectbox(
        "Seen status",
        options=["All", "New this run", "Seen before", "Not seen in latest run"],
    )

    quick_cols = st.columns(3)
    only_sponsorship_available = quick_cols[0].checkbox("Show only sponsorship available")
    only_possible_sponsorship = quick_cols[1].checkbox("Show only possible sponsorship")
    only_applied = quick_cols[2].checkbox("Show only applied jobs")

    quick_cols_2 = st.columns(3)
    only_interviews = quick_cols_2[0].checkbox("Show only interview jobs")
    only_active = quick_cols_2[1].checkbox("Show only active jobs")
    only_top_matches = quick_cols_2[2].checkbox("Show only top 20 matches")

    filtered = working.copy()
    normalized_sponsorship = filtered["sponsorship_status"].map(normalize_sponsorship_status)
    if sponsorship_status_filter != "All":
        filtered = filtered[normalized_sponsorship == sponsorship_status_filter]
    if only_sponsorship_available:
        filtered = filtered[filtered["sponsorship_status"].map(normalize_sponsorship_status) == "sponsorship available"]
    if only_possible_sponsorship:
        filtered = filtered[filtered["sponsorship_status"].map(normalize_sponsorship_status).isin(POSSIBLE_SPONSORSHIP_STATUSES)]
    if company_filter:
        filtered = filtered[filtered["company"].map(clean_text).isin(company_filter)]
    if location_filter:
        filtered = filtered[filtered["location"].map(clean_text).isin(location_filter)]
    if application_status_filter != "All":
        filtered = filtered[filtered["application_status"] == application_status_filter]
    if only_applied:
        filtered = filtered[filtered["application_status"] == "Applied"]
    if only_interviews:
        filtered = filtered[filtered["application_status"] == "Interview"]
    if only_active:
        filtered = filtered[filtered["application_status"].isin(ACTIVE_APPLICATION_STATUSES)]
    dashboard_latest_run_id = latest_run_id(pd.DataFrame(), working)
    if seen_filter == "New this run" and dashboard_latest_run_id:
        first_seen = pd.to_datetime(filtered.get("first_seen_at", ""), errors="coerce", utc=True)
        latest_run_started = pd.to_datetime(
            working.loc[working["run_id"].map(clean_text) == dashboard_latest_run_id, "run_started_at"],
            errors="coerce",
            utc=True,
        )
        latest_timestamp = latest_run_started.max() if not latest_run_started.empty else pd.NaT
        filtered = filtered[(filtered["run_id"].map(clean_text) == dashboard_latest_run_id) & (first_seen >= latest_timestamp)]
    elif seen_filter == "Seen before":
        filtered = filtered[
            (filtered.get("first_seen_at", "").map(clean_text) != "")
            & (filtered.get("last_seen_at", "").map(clean_text) != "")
            & (filtered.get("first_seen_at", "").map(clean_text) != filtered.get("last_seen_at", "").map(clean_text))
        ]
    elif seen_filter == "Not seen in latest run" and dashboard_latest_run_id:
        filtered = filtered[filtered["run_id"].map(clean_text) != dashboard_latest_run_id]
    filtered = filtered[pd.to_numeric(filtered["relevance_score"], errors="coerce").fillna(0) >= relevance_minimum]
    cv_scores = cv_match_numeric_series(filtered)
    filtered = filtered[(cv_scores.fillna(-1) >= cv_minimum) | (cv_minimum == 0)]
    if only_top_matches:
        filtered = top_best_matches(deduplicate_top_matches(filtered), limit=20)

    st.markdown("### All Matching Jobs")
    if filtered.empty:
        if only_applied or application_status_filter == "Applied":
            st.info("No jobs marked as Applied yet")
            return
        st.info("No jobs match the current filters.")
        return

    show_technical_details = st.checkbox("Show technical details", value=False)
    master_table = dashboard_master_table(filtered, show_technical_details=show_technical_details)
    fingerprint_lookup = {
        clean_text(row.get("id")): clean_text(row.get("job_fingerprint"))
        for _, row in ensure_job_ids(filtered).iterrows()
    }
    edited = st.data_editor(
        master_table,
        use_container_width=True,
        hide_index=True,
        disabled=[
            column
            for column in master_table.columns
            if column not in {"Application Status", "Notes"}
        ],
        column_config={
            "Job ID": st.column_config.TextColumn("Job ID"),
            "Company": st.column_config.TextColumn("Company"),
            "Job Title": st.column_config.TextColumn("Job Title"),
            "Employer": st.column_config.TextColumn("Employer"),
            "Location": st.column_config.TextColumn("Location"),
            "Salary": st.column_config.TextColumn("Salary"),
            "Posted": st.column_config.TextColumn("Posted"),
            "First Seen": st.column_config.TextColumn("First Seen"),
            "Last Seen": st.column_config.TextColumn("Last Seen"),
            "Sponsorship Status": st.column_config.TextColumn("Sponsorship Status"),
            "Sponsorship Reason": st.column_config.TextColumn("Sponsorship Reason", width="medium"),
            "Matched Sponsorship Phrase": st.column_config.TextColumn("Matched Sponsorship Phrase", width="medium"),
            "CV Match %": st.column_config.TextColumn("CV Match %"),
            "Relevance Score": st.column_config.NumberColumn("Relevance Score", format="%d"),
            "Apply Link": st.column_config.LinkColumn("Apply Link", display_text="Apply Now"),
            "Application Status": st.column_config.SelectboxColumn(
                "Application Status",
                options=list(APPLICATION_STATUSES),
                required=True,
            ),
            "Notes": st.column_config.TextColumn("Notes", width="large"),
            "job_fingerprint": st.column_config.TextColumn("job_fingerprint"),
            "searched_job_title": st.column_config.TextColumn("searched_job_title"),
            "run_id": st.column_config.TextColumn("run_id"),
            "run_started_at": st.column_config.TextColumn("run_started_at"),
        },
    )

    changed_rows = []
    original_tracking = master_table.set_index("Job ID")[["Application Status", "Notes"]]
    edited_tracking = edited.set_index("Job ID")[["Application Status", "Notes"]]
    for job_id in edited_tracking.index:
        if job_id not in original_tracking.index:
            continue
        original_status = clean_text(original_tracking.at[job_id, "Application Status"])
        original_notes = clean_text(original_tracking.at[job_id, "Notes"])
        edited_status = clean_text(edited_tracking.at[job_id, "Application Status"])
        edited_notes = clean_text(edited_tracking.at[job_id, "Notes"])
        if original_status != edited_status or original_notes != edited_notes:
            changed_rows.append((job_id, edited_status, edited_notes))

    if changed_rows:
        saved_any = False
        debug_rows = []
        for job_id, edited_status, edited_notes in changed_rows:
            before = get_job_tracking_from_storage(job_id, fingerprint_lookup.get(clean_text(job_id), ""))
            old_status = normalized_application_status(before.get("application_status"))
            saved = update_job_tracking(
                job_id,
                edited_status,
                edited_notes,
                job_fingerprint=fingerprint_lookup.get(clean_text(job_id), ""),
            )
            after = get_job_tracking_from_storage(job_id, fingerprint_lookup.get(clean_text(job_id), ""))
            debug_rows.append(
                {
                    "Job ID": job_id,
                    "Old Status": old_status,
                    "New Status": edited_status,
                    "Database Status After Save": normalized_application_status(after.get("application_status")),
                    "Saved": "yes" if saved else "no",
                }
            )
            saved_any = saved or saved_any
        st.session_state["tracking_debug_rows"] = debug_rows
        if saved_any:
            st.session_state["tracking_updated_message"] = "Tracking updated"
        st.rerun()

    tracking_debug_rows = st.session_state.pop("tracking_debug_rows", None)
    if tracking_debug_rows:
        with st.expander("Tracking save debug", expanded=True):
            st.dataframe(pd.DataFrame(tracking_debug_rows), use_container_width=True, hide_index=True)

    include_search_metadata = st.checkbox(
        "Include search metadata",
        value=False,
    )
    st.download_button(
        "Export Full Results to Excel",
        data=export_results_to_excel_bytes(
            filtered,
            include_search_metadata=include_search_metadata,
            include_technical_details=show_technical_details,
        ),
        file_name=f"job_search_results_{datetime.now().date().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_search_runs() -> None:
    st.subheader("Saved Search Runs")
    runs = get_search_runs()
    if runs.empty:
        st.info("No search runs saved yet.")
    else:
        st.dataframe(runs, use_container_width=True, hide_index=True)


def render_settings(results: pd.DataFrame) -> None:
    st.subheader("Settings")

    st.markdown("### CV / Resume Matching")
    saved_cv_text, _ = get_cv_profile()
    active_cv_text = clean_text(st.session_state.get("cv_text", "")) or saved_cv_text
    if active_cv_text and not clean_text(st.session_state.get("cv_text", "")):
        st.session_state["cv_text"] = active_cv_text
    st.caption(f"Saved CV text loaded: {'yes' if active_cv_text else 'no'}")
    cv_file = st.file_uploader("Upload CV / resume as PDF or DOCX", type=["pdf", "docx"])
    if cv_file is not None and st.button("Extract CV Text"):
        try:
            cv_text = extract_cv_text(cv_file)
            st.info(f"Extracted text length: {len(cv_text):,} characters")
            if len(cv_text.strip()) < 50:
                st.warning("This PDF may be scanned. Please upload DOCX instead.")
                return
            st.session_state["cv_text"] = cv_text
            save_cv_profile(cv_text)
            st.success(f"CV text extracted: {len(cv_text):,} characters.")
        except Exception:
            st.error("Could not read file, please try another format")
    if st.session_state.get("cv_text"):
        st.caption(f"Current CV text loaded: {len(st.session_state['cv_text']):,} characters.")

    if st.button("Recalculate CV Match for All Saved Jobs"):
        active_cv_text = clean_text(st.session_state.get("cv_text", "")) or clean_text(get_cv_profile()[0])
        if not active_cv_text:
            st.warning("Please upload and extract CV before calculating CV match.")
            return
        all_results = get_results()
        if all_results.empty:
            st.info("No saved jobs found to recalculate.")
            return
        updated_count = recalculate_cv_matches_for_results(all_results, active_cv_text)
        st.success(f"Recalculated CV match for {updated_count} saved job(s).")
        st.rerun()

    st.markdown("### Sponsorship Recalculation")
    st.caption("Uses strict explicit sponsorship phrases and checks negative/work-authorization phrases first. No SerpAPI calls are made.")
    if st.button("Recalculate Sponsorship for All Saved Jobs"):
        all_results = get_results()
        if all_results.empty:
            st.info("No saved jobs found to recalculate.")
            return
        updated_count = recalculate_sponsorship_for_results(all_results)
        st.success(f"Recalculated sponsorship for {updated_count} saved job(s).")
        st.rerun()


def render_local_recovery_section() -> None:
    with st.expander("Emergency Local Recovery", expanded=False):
        st.caption("Checks this app folder for `job_search.db` or other local SQLite `.db` files. No SerpAPI calls are made.")
        db_files = find_local_sqlite_databases()
        if not db_files:
            st.warning("No recoverable local data found. Streamlit temporary storage was reset.")
            return

        db_options = {str(path): path for path in db_files}
        selected_db_label = st.selectbox(
            "Local SQLite database file",
            options=list(db_options.keys()),
            format_func=lambda value: f"{Path(value).name} - {db_options[value].stat().st_size:,} bytes",
        )
        selected_db = db_options[selected_db_label]

        if st.button("Recover local data"):
            recovered = load_recoverable_sqlite_data(selected_db)
            st.session_state["recovered_local_data"] = recovered
            st.session_state["recovered_local_db"] = str(selected_db)

        recovered = st.session_state.get("recovered_local_data")
        if not recovered:
            return

        counts = recovered_counts(recovered)
        count_cols = st.columns(4)
        count_cols[0].metric("companies recovered", counts["companies"])
        count_cols[1].metric("job titles recovered", counts["job_titles"])
        count_cols[2].metric("jobs recovered", counts["jobs"])
        count_cols[3].metric("search runs recovered", counts["search_runs"])
        extra_cols = st.columns(2)
        extra_cols[0].metric("tracker rows recovered", counts["application_tracker"])
        extra_cols[1].metric("notes recovered", counts["notes"])

        if counts["companies"] == 0 and counts["job_titles"] == 0 and counts["jobs"] == 0 and counts["search_runs"] == 0:
            st.warning("No recoverable local data found. Streamlit temporary storage was reset.")
            return

        st.download_button(
            "Export recovered data to Excel",
            data=tables_to_excel_bytes(recovered),
            file_name=f"recovered_local_job_search_{datetime.now().date().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.markdown("#### Prepare migration to Supabase")
        if not is_supabase_connected():
            st.info("Connect Supabase first, then reopen this recovery panel and click `Migrate recovered data to Supabase`.")
            return
        if st.button("Migrate recovered data to Supabase"):
            try:
                migrated = migrate_recovered_data_to_supabase(recovered)
            except requests.RequestException as exc:
                st.error(f"Supabase migration failed: {exc}")
                return
            st.success(
                "Migrated recovered data to Supabase: "
                + ", ".join(f"{table}: {count}" for table, count in migrated.items())
            )
            st.rerun()


def run_timestamp_series(results: pd.DataFrame) -> pd.Series:
    if results.empty:
        return pd.Series(dtype="datetime64[ns, UTC]")
    source = results["run_started_at"] if "run_started_at" in results.columns else pd.Series([""] * len(results))
    created = results["created_at"] if "created_at" in results.columns else pd.Series([""] * len(results))
    timestamps = pd.to_datetime(source, errors="coerce", utc=True)
    created_timestamps = pd.to_datetime(created, errors="coerce", utc=True)
    return timestamps.fillna(created_timestamps)


def latest_run_id(search_runs: pd.DataFrame, results: pd.DataFrame) -> str:
    if not search_runs.empty and "run_id" in search_runs.columns:
        return clean_text(search_runs.iloc[0].get("run_id"))
    if not results.empty and "run_id" in results.columns:
        timestamps = run_timestamp_series(results)
        if not timestamps.empty and timestamps.notna().any():
            return clean_text(results.loc[timestamps.idxmax()].get("run_id"))
    return ""


def render_history_overview(results: pd.DataFrame, search_runs: pd.DataFrame) -> pd.DataFrame:
    st.markdown("### Last saved search run")
    if not is_supabase_connected():
        st.warning("Temporary local storage may reset.")

    if results.empty and search_runs.empty:
        st.info("No historical job results are saved yet.")
        return results

    last_run_id = latest_run_id(search_runs, results)
    last_run = pd.Series(dtype=object)
    if last_run_id and not search_runs.empty:
        matching_runs = search_runs[search_runs["run_id"].map(clean_text) == last_run_id]
        if not matching_runs.empty:
            last_run = matching_runs.iloc[0]

    last_run_results = results[results["run_id"].map(clean_text) == last_run_id] if last_run_id and "run_id" in results.columns else pd.DataFrame()
    last_cols = st.columns(4)
    last_cols[0].metric("run_id", last_run_id or "Unknown")
    last_cols[1].metric("run_started_at", clean_text(last_run.get("run_started_at", "")) or "Unknown")
    last_cols[2].metric("jobs saved", clean_text(last_run.get("jobs_saved", "")) or len(last_run_results))
    last_cols[3].metric("companies searched", last_run_results["company"].nunique() if not last_run_results.empty and "company" in last_run_results.columns else 0)

    st.markdown("### Historical results")
    run_ids = []
    if "run_id" in results.columns:
        run_ids = [run_id for run_id in results["run_id"].map(clean_text).drop_duplicates().tolist() if run_id]
    if not run_ids and not search_runs.empty and "run_id" in search_runs.columns:
        run_ids = [run_id for run_id in search_runs["run_id"].map(clean_text).drop_duplicates().tolist() if run_id]

    filter_col, run_col = st.columns([1, 2])
    history_filter = filter_col.selectbox(
        "History filter",
        options=["Today", "Last 7 days", "All history", "By run_id"],
        index=2,
    )
    run_options = ["All runs", *run_ids]
    default_run_index = run_options.index(last_run_id) if last_run_id in run_options else 0
    selected_run_id = run_col.selectbox("Load previous run", options=run_options, index=default_run_index)

    filtered = results.copy()
    timestamps = run_timestamp_series(filtered)
    now = pd.Timestamp.now(tz="UTC")
    if history_filter == "Today":
        filtered = filtered[timestamps.dt.date == now.date()]
    elif history_filter == "Last 7 days":
        filtered = filtered[timestamps >= (now - pd.Timedelta(days=7))]
    elif history_filter == "By run_id":
        if selected_run_id == "All runs" and last_run_id:
            selected_run_id = last_run_id
        filtered = filtered[filtered["run_id"].map(clean_text) == selected_run_id] if selected_run_id != "All runs" else filtered

    if selected_run_id != "All runs" and history_filter != "By run_id":
        filtered = filtered[filtered["run_id"].map(clean_text) == selected_run_id]

    if filtered.empty:
        st.info("No saved jobs match the selected historical filter.")
    else:
        st.success(f"Loaded {len(filtered)} saved historical job result(s). No SerpAPI search was run.")
    return filtered


def main() -> None:
    st.set_page_config(page_title="U.S. Job Search", layout="wide")
    supabase_url, supabase_key = get_supabase_credentials()
    supabase_status = get_supabase_setup_status(supabase_url, supabase_key)
    st.session_state["supabase_connected"] = bool(supabase_status.get("connected"))
    init_storage()
    if is_supabase_connected():
        migrate_supabase_job_fingerprints()

    st.title("U.S. Job Search")
    st.caption("Upload target companies and roles, search Google Jobs through SerpAPI, and export deduplicated results.")
    if is_supabase_connected():
        st.success("Connected to Supabase")
    else:
        st.warning("Running in temporary local mode")
        render_supabase_setup_instructions(supabase_status)
    render_local_recovery_section()
    if not is_supabase_connected() and not is_local_sqlite_allowed():
        st.stop()

    if "cv_text" not in st.session_state or not clean_text(st.session_state.get("cv_text")):
        saved_cv_text, _ = get_cv_profile()
        if saved_cv_text:
            st.session_state["cv_text"] = saved_cv_text

    companies = get_companies()
    job_titles = get_target_job_titles()
    targets = build_search_combinations(companies, job_titles)
    results = get_results()
    search_runs = get_search_runs()
    visible_results = render_history_overview(results, search_runs)

    dashboard_tab, upload_tab, search_tab, settings_tab = st.tabs(
        ["Dashboard", "Upload Targets", "Run Search", "Settings"]
    )

    with dashboard_tab:
        render_dashboard(visible_results, companies, job_titles)

    with upload_tab:
        render_upload_section()
        st.divider()
        saved_cols = st.columns(2)
        with saved_cols[0]:
            st.subheader("Saved Companies")
            if companies.empty:
                st.info("Upload an Excel file to add companies from Column A.")
            else:
                st.dataframe(companies, use_container_width=True, hide_index=True)
        with saved_cols[1]:
            st.subheader("Saved Job Titles")
            if job_titles.empty:
                st.info("Upload an Excel file to add job titles from Column B.")
            else:
                st.dataframe(job_titles, use_container_width=True, hide_index=True)
        st.metric("Total search combinations", len(targets))

    with search_tab:
        render_search_section(companies, job_titles)
        st.divider()
        render_search_runs()

    with settings_tab:
        render_settings(visible_results)


if __name__ == "__main__":
    main()
