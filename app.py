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

APPLICATION_STATUSES = ("New", "Interested", "Applied", "Rejected", "Interview", "Archived")


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
                sponsorship_reason TEXT,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                relevance_reason TEXT,
                cv_match_score INTEGER NOT NULL DEFAULT 0,
                cv_match_reason TEXT,
                status TEXT NOT NULL DEFAULT 'New',
                notes TEXT DEFAULT '',
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
        ensure_column(conn, "job_results", "run_id", "TEXT NOT NULL DEFAULT 'legacy'")
        ensure_column(conn, "job_results", "run_started_at", "TEXT")
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
                job_url,
                apply_link,
                description,
                created_at
            FROM job_results
            ORDER BY COALESCE(run_started_at, created_at) DESC, relevance_score DESC, company, searched_job_title
            """,
            conn,
        )


def get_search_runs() -> pd.DataFrame:
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


def update_job_tracking(job_id: int, status: str, notes: str) -> None:
    if status not in APPLICATION_STATUSES:
        status = "New"
    with get_connection() as conn:
        conn.execute(
            "UPDATE job_results SET status = ?, notes = ? WHERE id = ?",
            (status, notes, job_id),
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


def tokenize_for_match(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9+-]{2,}", clean_text(text).lower()))
    return {token for token in tokens if token not in CV_STOPWORDS}


def extract_cv_text(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    suffix = Path(uploaded_file.name).suffix.lower()
    data = uploaded_file.getvalue()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        from docx import Document

        document = Document(BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    return ""


def calculate_cv_match(title: str, description: str, cv_text: str) -> tuple[int, str]:
    cv_tokens = tokenize_for_match(cv_text)
    job_tokens = tokenize_for_match(f"{title} {description}")
    if not cv_tokens:
        return 0, "No CV uploaded."
    if not job_tokens:
        return 0, "No job text available for CV comparison."

    matched_tokens = sorted(job_tokens & cv_tokens)
    coverage = len(matched_tokens) / max(1, len(job_tokens))
    score = min(100, round(coverage * 100))
    if not matched_tokens:
        return 0, "No meaningful overlap with CV keywords."
    preview = ", ".join(matched_tokens[:12])
    return score, f"Matched CV keywords: {preview}"


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


def search_google_jobs(api_key: str, company: str, job_title: str, max_jobs: int) -> tuple[list[dict], int, int]:
    raw_jobs = []
    for query in make_job_search_queries(company, job_title):
        raw_jobs.extend(search_google_jobs_query(api_key, query))

    unique_jobs, duplicates_skipped = deduplicate_jobs(raw_jobs)
    return unique_jobs[:max_jobs], len(raw_jobs), duplicates_skipped


def save_job_results(
    run_id: str,
    run_started_at: str,
    company: str,
    searched_job_title: str,
    jobs: list[dict],
    relevance_threshold: int,
    include_broader_infrastructure: bool,
    cv_text: str = "",
) -> tuple[int, int]:
    created_at = utc_now()
    inserted = 0
    skipped = 0
    with get_connection() as conn:
        for job in jobs:
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
            cv_match_score, cv_match_reason = calculate_cv_match(title, description, cv_text)

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
                    raw_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        by=["relevance_score", "cv_match_score", "_salary_sort", "_recency_sort"],
        ascending=[False, False, False, False],
        na_position="last",
    )
    return ranked.head(limit).drop(columns=["_salary_sort", "_recency_sort"], errors="ignore")


def dataframe_to_excel_bytes(df: pd.DataFrame) -> bytes:
    from io import BytesIO

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="job_results")
    return output.getvalue()


EXPORT_COLUMNS = [
    "company",
    "searched_job_title",
    "title",
    "employer_name",
    "location",
    "salary",
    "posted_at",
    "sponsorship_status",
    "sponsorship_reason",
    "relevance_score",
    "relevance_reason",
    "cv_match_score",
    "cv_match_reason",
    "status",
    "notes",
    "apply_link",
]


def export_results_to_excel_bytes(df: pd.DataFrame) -> bytes:
    export_df = df.copy()
    for column in EXPORT_COLUMNS:
        if column not in export_df.columns:
            export_df[column] = ""
    return dataframe_to_excel_bytes(export_df[EXPORT_COLUMNS])


def build_email_body(top_matches: pd.DataFrame) -> str:
    if top_matches.empty:
        return "No matching jobs are currently saved."

    lines = ["Top 10 best job matches", ""]
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
    message["Subject"] = "Daily Job Search Top 10 Summary"
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

    st.sidebar.header("Search settings")
    max_jobs_per_target = st.sidebar.slider(
        "Max jobs per company/title",
        min_value=3,
        max_value=20,
        value=10,
        step=1,
    )
    include_broader_infrastructure = st.sidebar.checkbox(
        "Include broader infrastructure management jobs",
        value=True,
    )

    max_targets = st.number_input(
        "Maximum company/title pairs to search this run",
        min_value=1,
        max_value=max(1, len(targets)),
        value=min(10, max(1, len(targets))),
        step=1,
    )
    disabled = targets.empty or not api_key
    if "last_search_counters" in st.session_state:
        counters = st.session_state["last_search_counters"]
        st.caption(f"Last search run: {counters['run_id']}")
        counter_cols = st.columns(4)
        counter_cols[0].metric("Total raw jobs found", counters["total_raw_jobs"])
        counter_cols[1].metric("Duplicates skipped", counters["duplicates_skipped"])
        counter_cols[2].metric("Excluded by relevance", counters["excluded_by_relevance"])
        counter_cols[3].metric("Jobs saved", counters["jobs_saved"])

    if st.button("Run Job Search", type="primary", disabled=disabled):
        selected_targets = targets.head(int(max_targets))
        run_id = make_run_id()
        run_started_at = utc_now()
        progress = st.progress(0)
        status = st.empty()
        total_inserted = 0
        total_raw_jobs = 0
        total_duplicates_skipped = 0
        total_excluded_by_relevance = 0

        for index, row in enumerate(selected_targets.itertuples(index=False), start=1):
            status.write(f"Searching {row.job_title} at {row.company}...")
            try:
                jobs, raw_jobs_found, duplicates_skipped = search_google_jobs(
                    api_key,
                    row.company,
                    row.job_title,
                    int(max_jobs_per_target),
                )
                inserted, excluded_by_relevance = save_job_results(
                    run_id,
                    run_started_at,
                    row.company,
                    row.job_title,
                    jobs,
                    SAVE_RELEVANCE_THRESHOLD,
                    include_broader_infrastructure,
                    st.session_state.get("cv_text", ""),
                )
                total_raw_jobs += raw_jobs_found
                total_duplicates_skipped += duplicates_skipped
                total_inserted += inserted
                total_excluded_by_relevance += excluded_by_relevance
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


def render_dashboard(results: pd.DataFrame) -> None:
    st.subheader("Dashboard")
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
    top_10_per_run = st.checkbox(
        "Show only top 10 highest relevance jobs per run",
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
    filtered = filtered[filtered["relevance_score"] >= minimum_relevance_score]
    if top_10_per_run:
        filtered = top_jobs_per_run(filtered, limit=10)

    table_column_config = {
        "job_url": st.column_config.LinkColumn("Job URL"),
        "apply_link": st.column_config.LinkColumn("Apply link"),
        "sponsorship_reason": st.column_config.TextColumn("Sponsorship reason", width="medium"),
        "relevance_reason": st.column_config.TextColumn("Relevance reason", width="medium"),
        "cv_match_reason": st.column_config.TextColumn("CV match reason", width="medium"),
        "description": st.column_config.TextColumn("Description", width="large"),
    }

    st.subheader("Top 10 best matches")
    top_matches = top_best_matches(filtered, limit=10)
    if top_matches.empty:
        st.info("No jobs match the current filters.")
    else:
        st.caption("Sorted by relevance score, salary when available, then posting recency.")
        styled_top_matches = top_matches.style.apply(highlight_sponsorship_available, axis=1)
        st.dataframe(
            styled_top_matches,
            use_container_width=True,
            hide_index=True,
            column_config=table_column_config,
        )

    st.subheader("All matching jobs")
    styled_filtered = filtered.style.apply(highlight_sponsorship_available, axis=1)
    st.dataframe(
        styled_filtered,
        use_container_width=True,
        hide_index=True,
        column_config=table_column_config,
    )

    st.download_button(
        "Export Full Results to Excel",
        data=export_results_to_excel_bytes(filtered),
        file_name=f"job_search_results_{datetime.now().date().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_top_matches(results: pd.DataFrame) -> None:
    st.subheader("Top Matches")
    if results.empty:
        st.info("No job results saved yet.")
        return
    top_matches = top_best_matches(results, limit=10)
    st.caption("Sorted by relevance_score, cv_match_score, salary when available, and posted_at recency.")
    st.dataframe(
        top_matches.style.apply(highlight_sponsorship_available, axis=1),
        use_container_width=True,
        hide_index=True,
        column_config={
            "job_url": st.column_config.LinkColumn("Job URL"),
            "apply_link": st.column_config.LinkColumn("Apply link"),
            "description": st.column_config.TextColumn("Description", width="large"),
        },
    )


def render_application_tracker(results: pd.DataFrame) -> None:
    st.subheader("Application Tracker")
    if results.empty:
        st.info("No job results saved yet.")
        return

    tracker_columns = ["id", "company", "title", "location", "sponsorship_status", "relevance_score", "cv_match_score", "status", "notes", "apply_link"]
    tracker_df = results[[column for column in tracker_columns if column in results.columns]].copy()
    edited = st.data_editor(
        tracker_df,
        use_container_width=True,
        hide_index=True,
        disabled=[column for column in tracker_df.columns if column not in {"status", "notes"}],
        column_config={
            "status": st.column_config.SelectboxColumn("Status", options=list(APPLICATION_STATUSES), required=True),
            "notes": st.column_config.TextColumn("Notes", width="large"),
            "apply_link": st.column_config.LinkColumn("Apply link"),
        },
    )
    if st.button("Save Tracker Updates", type="primary"):
        for row in edited.itertuples(index=False):
            update_job_tracking(int(getattr(row, "id")), clean_text(getattr(row, "status")), clean_text(getattr(row, "notes")))
        st.success("Tracker updates saved.")
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
            st.session_state["cv_text"] = cv_text
            st.success(f"CV text extracted: {len(cv_text):,} characters.")
        except Exception as exc:
            st.error(f"Could not extract CV text: {exc}")
    if st.session_state.get("cv_text"):
        st.caption(f"Current CV text loaded: {len(st.session_state['cv_text']):,} characters.")

    st.markdown("### Email Settings")
    recipient_email = st.text_input("Recipient email", value=st.session_state.get("recipient_email", ""))
    st.session_state["recipient_email"] = recipient_email
    st.caption("Email is sent through SMTP credentials stored only in Streamlit secrets.")
    if st.button("Send Top 10 Email Summary"):
        if not recipient_email:
            st.warning("Enter a recipient email first.")
        else:
            success, message = send_email_summary(recipient_email, top_best_matches(results, limit=10))
            if success:
                st.success(message)
            else:
                st.error(message)


def main() -> None:
    st.set_page_config(page_title="U.S. Job Search", layout="wide")
    init_db()

    st.title("U.S. Job Search")
    st.caption("Upload target companies and roles, search Google Jobs through SerpAPI, and export deduplicated results.")

    targets = get_targets()
    results = get_results()

    upload_tab, search_tab, dashboard_tab, top_matches_tab, tracker_tab, settings_tab = st.tabs(
        ["Upload Targets", "Run Search", "Dashboard", "Top Matches", "Application Tracker", "Settings"]
    )

    with upload_tab:
        render_upload_section()
        st.divider()
        st.subheader("Saved targets")
        if targets.empty:
            st.info("Upload an Excel file to add company/title pairs.")
        else:
            st.dataframe(targets, use_container_width=True, hide_index=True)

    with search_tab:
        render_search_section(targets)
        st.divider()
        render_search_runs()

    with dashboard_tab:
        render_dashboard(results)

    with top_matches_tab:
        render_top_matches(results)

    with tracker_tab:
        render_application_tracker(results)

    with settings_tab:
        render_settings(results)


if __name__ == "__main__":
    main()
