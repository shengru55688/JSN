from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import JobCandidate


def connect(path: str) -> sqlite3.Connection:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS email_messages (
            message_id TEXT PRIMARY KEY,
            internal_date TEXT NOT NULL,
            processed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_key TEXT PRIMARY KEY,
            linkedin_job_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            original_url TEXT NOT NULL,
            source_text TEXT NOT NULL,
            received_at TEXT NOT NULL,
            gmail_message_id TEXT NOT NULL,
            description_text TEXT,
            description_status TEXT,
            description_fetched_at TEXT,
            analysis_json TEXT,
            config_hash TEXT,
            decision TEXT,
            report_week TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS weekly_reports (
            week_key TEXT PRIMARY KEY,
            discord_thread_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            job_count INTEGER NOT NULL
        );
        """
    )
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)")
    }
    for column in ("description_text", "description_status", "description_fetched_at"):
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
    connection.commit()
    return connection


def processed_message_ids(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT message_id FROM email_messages")
    return {row[0] for row in rows}


def store_message(
    connection: sqlite3.Connection, message_id: str, internal_date: str
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT OR IGNORE INTO email_messages(message_id, internal_date, processed_at) "
        "VALUES (?, ?, ?)",
        (message_id, internal_date, now),
    )


def store_job(connection: sqlite3.Connection, job: JobCandidate) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO jobs(
            job_key, linkedin_job_id, title, company, location, original_url,
            source_text, received_at, gmail_message_id, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_key) DO UPDATE SET
            title = excluded.title,
            company = excluded.company,
            location = excluded.location,
            original_url = excluded.original_url,
            source_text = excluded.source_text,
            received_at = excluded.received_at,
            gmail_message_id = excluded.gmail_message_id,
            description_text = CASE
                WHEN jobs.source_text != excluded.source_text THEN NULL
                ELSE jobs.description_text
            END,
            description_status = CASE
                WHEN jobs.source_text != excluded.source_text THEN NULL
                ELSE jobs.description_status
            END,
            description_fetched_at = CASE
                WHEN jobs.source_text != excluded.source_text THEN NULL
                ELSE jobs.description_fetched_at
            END,
            analysis_json = CASE
                WHEN jobs.source_text != excluded.source_text THEN NULL
                ELSE jobs.analysis_json
            END,
            config_hash = CASE
                WHEN jobs.source_text != excluded.source_text THEN NULL
                ELSE jobs.config_hash
            END,
            decision = CASE
                WHEN jobs.source_text != excluded.source_text THEN NULL
                ELSE jobs.decision
            END,
            last_seen = excluded.last_seen
        """,
        (
            job.job_key,
            job.linkedin_job_id,
            job.title,
            job.company,
            job.location,
            job.original_url,
            job.source_text,
            job.received_at,
            job.gmail_message_id,
            now,
            now,
        ),
    )


def pending_scoring(
    connection: sqlite3.Connection, config_hash: str, cutoff: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM jobs
        WHERE report_week IS NULL
          AND received_at >= ?
          AND (analysis_json IS NULL OR config_hash != ?)
        ORDER BY received_at DESC
        """,
        (cutoff, config_hash),
    )
    return [dict(row) for row in rows]


def pending_job_details(
    connection: sqlite3.Connection,
    cutoff: str,
    retry_before: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT job_key, linkedin_job_id, original_url FROM jobs
        WHERE report_week IS NULL
          AND received_at >= ?
          AND (
              description_status IS NULL
              OR (description_status != 'found' AND description_fetched_at < ?)
          )
        ORDER BY received_at DESC
        LIMIT ?
        """,
        (cutoff, retry_before, limit),
    )
    return [dict(row) for row in rows]


def save_job_description(
    connection: sqlite3.Connection,
    job_key: str,
    description: str | None,
    status: str,
    fetched_at: str,
) -> None:
    existing = connection.execute(
        "SELECT description_text FROM jobs WHERE job_key = ?", (job_key,)
    ).fetchone()
    if existing is None:
        return
    content_changed = existing["description_text"] != description
    connection.execute(
        """
        UPDATE jobs
        SET description_text = ?,
            description_status = ?,
            description_fetched_at = ?,
            analysis_json = CASE WHEN ? THEN NULL ELSE analysis_json END,
            config_hash = CASE WHEN ? THEN NULL ELSE config_hash END,
            decision = CASE WHEN ? THEN NULL ELSE decision END
        WHERE job_key = ?
        """,
        (
            description,
            status,
            fetched_at,
            content_changed,
            content_changed,
            content_changed,
            job_key,
        ),
    )


def save_analysis(
    connection: sqlite3.Connection,
    job_key: str,
    analysis: dict[str, Any],
    config_hash: str,
) -> None:
    connection.execute(
        "UPDATE jobs SET analysis_json = ?, config_hash = ?, decision = ? WHERE job_key = ?",
        (json.dumps(analysis, ensure_ascii=False), config_hash, analysis["decision"], job_key),
    )


def report_candidates(connection: sqlite3.Connection, cutoff: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM jobs
        WHERE report_week IS NULL
          AND received_at >= ?
          AND decision IN ('pass', 'review')
        ORDER BY json_extract(analysis_json, '$.score') DESC, received_at DESC
        """,
        (cutoff,),
    )
    return [dict(row) for row in rows]


def report_examples(
    connection: sqlite3.Connection, cutoff: str, limit: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM jobs
        WHERE received_at >= ?
          AND decision IN ('pass', 'review')
        ORDER BY json_extract(analysis_json, '$.score') DESC, received_at DESC
        LIMIT ?
        """,
        (cutoff, limit),
    )
    return [dict(row) for row in rows]


def report_exists(connection: sqlite3.Connection, week_key: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM weekly_reports WHERE week_key = ?", (week_key,)
    ).fetchone()
    return row is not None


def save_report(
    connection: sqlite3.Connection,
    week_key: str,
    thread_id: str,
    job_keys: list[str],
    job_count: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection:
        connection.execute(
            "INSERT INTO weekly_reports(week_key, discord_thread_id, created_at, job_count) "
            "VALUES (?, ?, ?, ?)",
            (week_key, thread_id, now, job_count),
        )
        connection.executemany(
            "UPDATE jobs SET report_week = ? WHERE job_key = ?",
            [(week_key, job_key) for job_key in job_keys],
        )
