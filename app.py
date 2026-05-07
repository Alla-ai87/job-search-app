import hashlib
from io import BytesIO
import json
import re
import smtplib
import sqlite3
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import requests
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "job_search.db"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_ENDPOINT = "https://serpapi.com/account.json"


SPONSORSHIP_POSITIVE_PHRASES = (
    "visa sponsorship",
    "sponsorship available",
    "we sponsor",
    "H1B",
    "relocation support",
)

SPONSORSHIP_AUTHORIZATION_PHRASES = (
    "work authorization",
    "authorized to work",
    "work permit",
    "employment authorization",
)

SPONSORSHIP_NEGATIVE_PHRASES = (
    "no sponsorship",
    "not eligible for sponsorship",
    "must be authorized to work",
    "no visa support",
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

APPLICATION_STATUSES = ("New", "Interested", "Applied", "Interview", "Offer", "Rejected", "Archived")
ACTIVE_APPLICATION_STATUSES = ("New", "Interested", "Applied", "Interview", "Offer")
COMPANY_HEADER_VALUES = {"company", "companies"}
JOB_TITLE_HEADER_VALUES = {"job title", "job titles", "target job title", "target job titles"}

RESULT_COLUMNS = [
    "id",
    "run_id",
    "run_started_at",
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
        supabase_request("GET", table_name, params={"select": "*", "limit": 1})
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
                sponsorship_reason TEXT,
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
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_column(conn, "job_results", "sponsorship_reason", "TEXT")
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
    if not is_supabase_connected():
        init_db()


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


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


def save_targets(companies_df: pd.DataFrame, job_titles_df: pd.DataFrame) -> tuple[int, int]:
    created_at = utc_now()
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
    identity = "|".join(
        [
            clean_text(row.get("title")).lower(),
            clean_text(row.get("employer_name")).lower(),
            clean_text(row.get("location")).lower(),
            clean_text(row.get("apply_link")).lower(),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


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


def get_results() -> pd.DataFrame:
    if is_supabase_connected():
        rows = supabase_request(
            "GET",
            "job_results",
            params={"select": ",".join(RESULT_COLUMNS), "order": "created_at.desc"},
        )
        results = dataframe_from_rows(rows, RESULT_COLUMNS)
        if not results.empty:
            results = results.sort_values(
                by=["run_started_at", "created_at", "relevance_score"],
                ascending=[False, False, False],
                na_position="last",
            )
        return ensure_job_ids(results)

    with get_connection() as conn:
        results = pd.read_sql_query(
            """
            SELECT
                id,
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
                sponsorship_reason,
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
                created_at
            FROM job_results
            ORDER BY COALESCE(run_started_at, created_at) DESC, relevance_score DESC, company, searched_job_title
            """,
            conn,
        )
    return ensure_job_ids(results)


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


def update_job_tracking(job_id: int, application_status: str, application_notes: str, applied_date: str = "") -> None:
    if application_status not in APPLICATION_STATUSES:
        application_status = "New"
    if is_supabase_connected():
        updated_at = utc_now()
        supabase_request(
            "PATCH",
            "job_results",
            params={"id": f"eq.{job_id}"},
            json_body={
                "application_status": application_status,
                "application_notes": application_notes,
                "applied_date": applied_date or None,
                "status": application_status,
                "notes": application_notes,
            },
            prefer="return=minimal",
        )
        supabase_upsert(
            "application_tracker",
            [
                {
                    "job_id": str(job_id),
                    "job_result_id": job_id,
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
                    "job_id": str(job_id),
                    "job_result_id": job_id,
                    "note_text": application_notes,
                    "updated_at": updated_at,
                }
            ],
            "job_id",
        )
        return

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE job_results
            SET
                application_status = ?,
                application_notes = ?,
                applied_date = ?,
                status = ?,
                notes = ?
            WHERE id = ?
            """,
            (application_status, application_notes, applied_date, application_status, application_notes, job_id),
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


def save_search_profile(profile_name: str, settings: dict[str, int]) -> None:
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


def get_search_profile(profile_name: str) -> dict[str, int]:
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


def detect_sponsorship_status(title: object, description: object, application_text: object = "") -> tuple[str, str]:
    text = " ".join([clean_text(title), clean_text(description), clean_text(application_text)]).lower()
    for phrase in SPONSORSHIP_POSITIVE_PHRASES:
        if phrase_in_text(text, phrase):
            return "sponsorship available", phrase
    for phrase in SPONSORSHIP_NEGATIVE_PHRASES:
        if phrase_in_text(text, phrase):
            return "sponsorship not available", phrase
    for phrase in SPONSORSHIP_AUTHORIZATION_PHRASES:
        if phrase_in_text(text, phrase):
            return "requires work authorization", phrase
    return "not mentioned", ""


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
    rows = []
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
        application_text = collect_application_text(job)
        sponsorship_status, sponsorship_reason = detect_sponsorship_status(title, description, application_text)
        cv_match_score, cv_match_reason = calculate_cv_match(
            title,
            employer_name,
            description,
            searched_job_title,
            active_cv_text,
        )
        if cv_match_score > 0:
            cv_positive_count += 1

        rows.append(
            {
                "result_key": make_result_key(company, searched_job_title, job),
                "run_id": run_id,
                "run_started_at": run_started_at,
                "company": company,
                "searched_job_title": searched_job_title,
                "title": title,
                "employer_name": employer_name,
                "location": location,
                "via": clean_text(job.get("via")),
                "posted_at": clean_text(job.get("detected_extensions", {}).get("posted_at") or job.get("posted_at")),
                "schedule_type": clean_text(job.get("detected_extensions", {}).get("schedule_type")),
                "salary": get_salary(job),
                "description": description,
                "job_url": job_url,
                "apply_link": apply_link,
                "sponsorship_status": sponsorship_status,
                "sponsorship_reason": sponsorship_reason,
                "relevance_score": int(relevance_score),
                "relevance_reason": relevance_reason,
                "cv_match_score": int(cv_match_score),
                "cv_match_reason": cv_match_reason,
                "raw_json": job,
                "created_at": created_at,
            }
        )

    if rows:
        supabase_upsert("job_results", rows, "result_key")
    return len(rows), skipped, cv_positive_count


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
            application_text = collect_application_text(job)
            sponsorship_status, sponsorship_reason = detect_sponsorship_status(title, description, application_text)
            cv_match_score, cv_match_reason = calculate_cv_match(
                title,
                employer_name,
                description,
                searched_job_title,
                active_cv_text,
            )
            if cv_match_score > 0:
                cv_positive_count += 1

            cursor = conn.execute(
                """
                INSERT INTO job_results (
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
                    sponsorship_reason,
                    relevance_score,
                    relevance_reason,
                    cv_match_score,
                    cv_match_reason,
                    status,
                    notes,
                    application_status,
                    applied_date,
                    application_notes,
                    raw_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(result_key) DO UPDATE SET
                    run_id = excluded.run_id,
                    run_started_at = excluded.run_started_at,
                    company = excluded.company,
                    searched_job_title = excluded.searched_job_title,
                    title = excluded.title,
                    employer_name = excluded.employer_name,
                    location = excluded.location,
                    via = excluded.via,
                    posted_at = excluded.posted_at,
                    schedule_type = excluded.schedule_type,
                    salary = excluded.salary,
                    description = excluded.description,
                    job_url = excluded.job_url,
                    apply_link = excluded.apply_link,
                    sponsorship_status = excluded.sponsorship_status,
                    sponsorship_reason = excluded.sponsorship_reason,
                    relevance_score = excluded.relevance_score,
                    relevance_reason = excluded.relevance_reason,
                    cv_match_score = excluded.cv_match_score,
                    cv_match_reason = excluded.cv_match_reason,
                    raw_json = excluded.raw_json,
                    created_at = excluded.created_at
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
                    sponsorship_reason,
                    relevance_score,
                    relevance_reason,
                    cv_match_score,
                    cv_match_reason,
                    "New",
                    "",
                    "New",
                    "",
                    "",
                    json.dumps(job),
                    created_at,
                ),
            )
            inserted += cursor.rowcount
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
        ranked["cv_match_score"] = 0
    ranked["_recency_sort"] = ranked.apply(
        lambda row: parse_recency_score(row.get("posted_at"), row.get("created_at")),
        axis=1,
    )
    ranked = ranked.sort_values(
        by=["cv_match_score", "relevance_score", "_salary_sort", "_recency_sort"],
        ascending=[False, False, False, False],
        na_position="last",
    )
    return ranked.head(limit).drop(columns=["_salary_sort", "_recency_sort"], errors="ignore")


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
    "sponsorship_reason",
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
    export_df = ensure_job_ids(df)
    export_columns = BASE_EXPORT_COLUMNS.copy()
    if include_search_metadata:
        export_columns.insert(2, "searched_job_title")
    if include_technical_details:
        export_columns.extend(TECHNICAL_EXPORT_COLUMNS)

    for column in export_columns:
        if column not in export_df.columns:
            export_df[column] = ""
    export_df = export_df[export_columns].rename(columns={"id": "Job ID"})
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
    return clean_text(status) in {"sponsorship_available", "sponsorship available"}


def highlight_sponsorship_available(row: pd.Series) -> list[str]:
    if is_sponsorship_available_status(row.get("sponsorship_status")):
        return ["background-color: #d1fae5"] * len(row)
    return [""] * len(row)


def highlight_cv_match_score(row: pd.Series) -> list[str]:
    styles = [""] * len(row)
    if "cv_match_score" not in row.index:
        return styles
    try:
        score = int(row.get("cv_match_score") or 0)
    except (TypeError, ValueError):
        score = 0
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
        .format({"cv_match_score": "{:.0f}", "relevance_score": "{:.0f}"})
    )


def top_matches_column_config() -> dict:
    return {
        "Job ID": st.column_config.TextColumn("Job ID"),
        "cv_match_score": st.column_config.NumberColumn("CV Match %", format="%d"),
        "relevance_score": st.column_config.NumberColumn("Relevance", format="%d"),
        "job_url": st.column_config.LinkColumn("Job URL"),
        "apply_link": st.column_config.LinkColumn("Apply", display_text="Apply Now"),
        "cv_match_score": st.column_config.NumberColumn("CV Match %", format="%d"),
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
    st.metric("Search combinations", len(companies_df) * len(job_titles_df))
    if len(companies_df) <= 3:
        st.warning("Please verify Column A contains the full company list.")

    if st.button("Save Uploaded Targets", type="primary"):
        inserted_companies, inserted_job_titles = save_targets(companies_df, job_titles_df)
        if is_supabase_connected():
            st.success(
                f"Saved/updated {inserted_companies} companies and {inserted_job_titles} job titles in Supabase. "
                "Existing duplicates were kept as one saved value."
            )
        else:
            st.success(
                f"Saved {inserted_companies} new companies and {inserted_job_titles} new job titles. "
                "Existing duplicates were skipped."
            )
        st.rerun()


def clamp_int(value: object, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def search_profile_defaults(profile_name: str, company_count: int, job_title_count: int, max_query_variations: int) -> dict[str, int]:
    if profile_name == "Balanced Mode":
        return {
            "max_companies_per_run": min(5, max(1, company_count)),
            "max_job_titles_per_run": min(5, max(1, job_title_count)),
            "max_query_variations": min(4, max_query_variations),
            "max_jobs_per_target": 10,
        }
    if profile_name == "Full / Maximum Mode":
        saved_settings = get_search_profile("Full Search")
        if saved_settings:
            return saved_settings
        return {
            "max_companies_per_run": max(1, company_count),
            "max_job_titles_per_run": max(1, job_title_count),
            "max_query_variations": max_query_variations,
            "max_jobs_per_target": 20,
        }
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

    selected_profile = st.selectbox(
        "Search profile",
        options=["Safe Mode", "Balanced Mode", "Full / Maximum Mode"],
        index=["Safe Mode", "Balanced Mode", "Full / Maximum Mode"].index(
            st.session_state.get("active_search_profile", "Safe Mode")
        ),
    )
    if st.session_state.get("active_search_profile") != selected_profile:
        apply_search_settings(
            search_profile_defaults(selected_profile, company_count, job_title_count, max_query_variation_count),
            company_count,
            job_title_count,
            max_query_variation_count,
        )
        st.session_state["active_search_profile"] = selected_profile

    safe_mode = st.checkbox("Safe mode - reduce API usage", value=selected_profile == "Safe Mode")
    if safe_mode and selected_profile != "Safe Mode" and st.session_state.get("safe_mode_forced_profile") != selected_profile:
        apply_search_settings(
            search_profile_defaults("Safe Mode", company_count, job_title_count, max_query_variation_count),
            company_count,
            job_title_count,
            max_query_variation_count,
        )
        st.session_state["safe_mode_forced_profile"] = selected_profile

    st.caption("Use Full Search only after API quota renews.")

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
        st.session_state["pending_search_profile"] = "Full / Maximum Mode"
        st.success("Loaded Full Search settings. Search was not run.")
        st.rerun()

    selected_companies = companies.head(int(max_companies_per_run)) if companies is not None else pd.DataFrame()
    selected_job_titles = job_titles.head(int(max_job_titles_per_run)) if job_titles is not None else pd.DataFrame()
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
    st.subheader("Dashboard")
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

    col1, col2, col3 = st.columns(3)
    col1.metric("Saved jobs", len(results))
    col2.metric("Companies", results["company"].nunique())
    col3.metric("Sponsorship available", results["sponsorship_status"].map(is_sponsorship_available_status).sum())
    st.caption(
        "New searches save jobs with relevance_score >= 1. Strong controls/contracts/PMO matches score highest; "
        "infrastructure management titles need sector context."
    )

    sponsorship_options = [
        "sponsorship_available",
        "sponsorship_not_available",
        "not_mentioned",
        "sponsorship available",
        "sponsorship not available",
        "not mentioned",
    ]
    sponsorship_filter = st.multiselect(
        "Sponsorship status",
        options=sponsorship_options,
        default=sponsorship_options,
    )
    possible_sponsorship_only = st.checkbox(
        "Show only jobs with possible sponsorship",
        value=False,
    )
    company_filter = st.multiselect(
        "Company",
        options=sorted(results["company"].dropna().unique()),
    )
    application_status_filter = st.selectbox(
        "Application status",
        options=["All statuses", *APPLICATION_STATUSES],
    )
    show_applied_only = st.checkbox(
        "Show only jobs I applied to",
        value=False,
    )
    top_20_per_run = st.checkbox(
        "Show only top 20 highest relevance jobs per run",
        value=False,
    )
    show_technical_details = st.checkbox(
        "Show technical details",
        value=False,
    )
    minimum_relevance_score = st.slider(
        "Minimum relevance score",
        min_value=1,
        max_value=5,
        value=2,
        step=1,
    )

    filtered = results[results["sponsorship_status"].isin(sponsorship_filter)]
    if possible_sponsorship_only:
        filtered = filtered[filtered["sponsorship_status"].map(is_sponsorship_available_status)]
    if company_filter:
        filtered = filtered[filtered["company"].isin(company_filter)]
    if show_applied_only:
        filtered = filtered[filtered["application_status"] == "Applied"]
    elif application_status_filter != "All statuses":
        filtered = filtered[filtered["application_status"] == application_status_filter]
    filtered = filtered[filtered["relevance_score"] >= minimum_relevance_score]
    if top_20_per_run:
        filtered = top_jobs_per_run(filtered, limit=20)

    table_column_config = {
        "Job ID": st.column_config.TextColumn("Job ID"),
        "job_url": st.column_config.LinkColumn("Job URL"),
        "apply_link": st.column_config.LinkColumn("Apply", display_text="Apply Now"),
        "sponsorship_reason": st.column_config.TextColumn("Sponsorship reason", width="medium"),
        "relevance_reason": st.column_config.TextColumn("Relevance reason", width="medium"),
        "cv_match_reason": st.column_config.TextColumn("CV match reason", width="medium"),
        "application_status": st.column_config.TextColumn("status"),
        "applied_date": st.column_config.TextColumn("Applied date"),
        "application_notes": st.column_config.TextColumn("notes", width="large"),
        "description": st.column_config.TextColumn("Description", width="large"),
    }

    st.subheader("Top 20 best matches")
    top_matches = top_best_matches(deduplicate_top_matches(filtered), limit=20)
    if top_matches.empty:
        st.info("No jobs match the current filters.")
    else:
        st.caption("Sorted by CV match score, relevance score, salary when available, then posting recency.")
        top_matches = display_job_id_column(reorder_top_match_columns(top_matches, show_technical_details))
        styled_top_matches = style_top_matches(top_matches)
        st.dataframe(
            styled_top_matches,
            use_container_width=True,
            hide_index=True,
            column_config=top_matches_column_config(),
    )

    st.subheader("All matching jobs")
    display_filtered = display_job_id_column(reorder_job_columns(filtered, show_technical_details))
    styled_filtered = (
        display_filtered.style.apply(highlight_sponsorship_available, axis=1)
        .apply(highlight_cv_match_score, axis=1)
        .apply(highlight_applied_status, axis=1)
    )
    st.dataframe(
        styled_filtered,
        use_container_width=True,
        hide_index=True,
        column_config=table_column_config,
    )

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


def render_top_matches(results: pd.DataFrame) -> None:
    st.subheader("Top Matches")
    if results.empty:
        st.info("No job results saved yet.")
        return
    show_technical_details = st.checkbox("Show technical details", value=False, key="top_matches_show_technical_details")
    top_matches = top_best_matches(deduplicate_top_matches(results), limit=20)
    top_matches = display_job_id_column(reorder_top_match_columns(top_matches, show_technical_details))
    st.caption("Sorted by CV Match %, Relevance, salary when available, and posted_at recency.")
    st.dataframe(
        style_top_matches(top_matches),
        use_container_width=True,
        hide_index=True,
        column_config=top_matches_column_config(),
    )


def render_application_tracker(results: pd.DataFrame) -> None:
    st.subheader("Application Tracker")
    if results.empty:
        st.info("No job results saved yet.")
        return

    tracker_source = ensure_job_ids(results)
    if "application_status" not in tracker_source.columns:
        tracker_source["application_status"] = tracker_source.get("status", "New")
    if "application_notes" not in tracker_source.columns:
        tracker_source["application_notes"] = tracker_source.get("notes", "")
    if "applied_date" not in tracker_source.columns:
        tracker_source["applied_date"] = ""

    tracker_source["application_status"] = tracker_source["application_status"].fillna("New").replace("", "New")
    tracker_source["application_notes"] = tracker_source["application_notes"].fillna("")
    tracker_source["applied_date"] = tracker_source["applied_date"].fillna("")

    total_applied = int((tracker_source["application_status"] == "Applied").sum())
    total_interviews = int((tracker_source["application_status"] == "Interview").sum())
    total_offers = int((tracker_source["application_status"] == "Offer").sum())
    stat_col1, stat_col2, stat_col3 = st.columns(3)
    stat_col1.metric("Total Applied", total_applied)
    stat_col2.metric("Interviews", total_interviews)
    stat_col3.metric("Offers", total_offers)

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    show_applied = filter_col1.checkbox("Show only Applied", value=False)
    show_interviews = filter_col2.checkbox("Show only Interviews", value=False)
    show_active = filter_col3.checkbox("Show only Active jobs", value=False)
    show_technical_details = st.checkbox("Show technical details", value=False, key="tracker_show_technical_details")

    filtered_tracker = tracker_source
    selected_statuses = []
    if show_applied:
        selected_statuses.append("Applied")
    if show_interviews:
        selected_statuses.append("Interview")
    if selected_statuses:
        filtered_tracker = filtered_tracker[filtered_tracker["application_status"].isin(selected_statuses)]
    if show_active:
        filtered_tracker = filtered_tracker[filtered_tracker["application_status"].isin(ACTIVE_APPLICATION_STATUSES)]

    tracker_columns = [
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
        "applied_date",
        "application_notes",
    ]
    if show_technical_details:
        tracker_columns.extend(["run_id", "run_started_at"])
    tracker_df = filtered_tracker[[column for column in tracker_columns if column in filtered_tracker.columns]].copy()
    if tracker_df.empty:
        st.info("No applications match the current tracker filters.")
        return

    tracker_df = display_job_id_column(tracker_df)
    tracker_df.insert(0, "select", False)
    edited = st.data_editor(
        tracker_df,
        use_container_width=True,
        hide_index=True,
        disabled=[
            column
            for column in tracker_df.columns
            if column not in {"select", "application_status", "applied_date", "application_notes"}
        ],
        column_config={
            "select": st.column_config.CheckboxColumn("Select"),
            "Job ID": st.column_config.TextColumn("Job ID"),
            "cv_match_score": st.column_config.NumberColumn("CV Match %", format="%d"),
            "relevance_score": st.column_config.NumberColumn("Relevance", format="%d"),
            "application_status": st.column_config.SelectboxColumn(
                "status",
                options=list(APPLICATION_STATUSES),
                required=True,
            ),
            "applied_date": st.column_config.TextColumn("Applied date"),
            "application_notes": st.column_config.TextColumn("notes", width="large"),
            "apply_link": st.column_config.LinkColumn("Apply", display_text="Apply Now"),
        },
    )

    save_col, applied_col = st.columns([1, 1])
    if save_col.button("Save Tracker Updates", type="primary"):
        edited_for_save = edited.rename(columns={"Job ID": "id"})
        for row in edited_for_save.itertuples(index=False):
            update_job_tracking(
                int(getattr(row, "id")),
                clean_text(getattr(row, "application_status")),
                clean_text(getattr(row, "application_notes")),
                clean_text(getattr(row, "applied_date")),
            )
        st.success("Tracker updates saved.")
        st.rerun()

    if applied_col.button("Mark as Applied"):
        selected_rows = edited[edited["select"] == True]
        if selected_rows.empty:
            st.warning("Select at least one job to mark as applied.")
            return

        applied_timestamp = utc_now()
        selected_rows = selected_rows.rename(columns={"Job ID": "id"})
        for row in selected_rows.itertuples(index=False):
            update_job_tracking(
                int(getattr(row, "id")),
                "Applied",
                clean_text(getattr(row, "application_notes")),
                applied_timestamp,
            )
        st.success(f"Marked {len(selected_rows)} job(s) as applied.")
        st.rerun()


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

    st.title("U.S. Job Search")
    st.caption("Upload target companies and roles, search Google Jobs through SerpAPI, and export deduplicated results.")
    if is_supabase_connected():
        st.success("Connected to Supabase")
    else:
        st.warning("Running in temporary local mode")
        render_supabase_setup_instructions(supabase_status)
    render_local_recovery_section()

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

    upload_tab, search_tab, dashboard_tab, top_matches_tab, tracker_tab, settings_tab = st.tabs(
        ["Upload Targets", "Run Search", "Dashboard", "Top Matches", "Application Tracker", "Settings"]
    )

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

    with dashboard_tab:
        render_dashboard(visible_results, companies, job_titles)

    with top_matches_tab:
        render_top_matches(visible_results)

    with tracker_tab:
        render_application_tracker(visible_results)

    with settings_tab:
        render_settings(visible_results)


if __name__ == "__main__":
    main()
