from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import load_config
from .database import (
    connect,
    pending_job_details,
    pending_scoring,
    processed_message_ids,
    report_candidates,
    report_examples,
    report_exists,
    save_analysis,
    save_job_description,
    save_report,
    store_job,
    store_message,
)
from .discord import post_discord_test, post_weekly_report
from .gmail import fetch_unprocessed_messages
from .job_details import fetch_job_description
from .parser import parse_linkedin_alert
from .scoring import analyze_job, build_job_instructions, scoring_config_hash


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger("jsn")
TAIPEI = ZoneInfo("Asia/Taipei")
DESCRIPTION_RETRY_DAYS = 7


def _report_job(row: dict[str, object]) -> dict[str, object]:
    row["analysis"] = json.loads(str(row.pop("analysis_json")))
    return row


def run_discord_report_test(limit: int) -> None:
    settings_path = Path(os.getenv("JSN_CONFIG_PATH", "config/settings.yaml"))
    if settings_path.exists():
        config = load_config()
    elif os.getenv("JSN_CONFIG_PATH"):
        raise FileNotFoundError(f"Configured settings file does not exist: {settings_path}")
    else:
        config = {"database": {}, "discord": {}}
    database_path = str(config["database"].get("path", "data/jobs.sqlite3"))
    connection = connect(database_path)
    try:
        max_age = max(1, int(config["database"].get("max_job_age_days", 30)))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()
        rows = report_examples(connection, cutoff, limit)
        if len(rows) < limit:
            raise SystemExit(
                f"Need {limit} recently scored jobs eligible for a report, "
                f"but found {len(rows)}. No Discord post was made. "
                "Score more jobs with --test-llm, then retry."
            )

        report = [_report_job(dict(row)) for row in rows]
        local_now = datetime.now(TAIPEI)
        title_prefix = config["discord"].get("title_prefix", "LinkedIn 職缺週報")
        title = f"{title_prefix} 格式測試 {local_now:%Y-%m-%d %H:%M}"
        thread_id = post_weekly_report(
            report,
            title,
            config["discord"].get("webhook_env", "DISCORD_FORUM_WEBHOOK_URL"),
        )
        LOGGER.info(
            "Posted a Discord report-format test with %d jobs; database report state was not changed",
            len(report),
        )
        print(f"Discord report-format test created (Forum thread ID: {thread_id})")
    finally:
        connection.close()


def _enrich_pending_jobs(
    connection,
    config: dict[str, Any],
    cutoff: str,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []

    detail_config = config.get("job_details", {})
    timeout = float(detail_config.get("timeout_seconds", 20))
    retry_days = max(1, int(detail_config.get("retry_after_days", DESCRIPTION_RETRY_DAYS)))
    retry_before = (datetime.now(timezone.utc) - timedelta(days=retry_days)).isoformat()
    pending = pending_job_details(connection, cutoff, retry_before, limit)
    found = 0
    status_counts: dict[str, int] = {}
    attempted_keys: list[str] = []
    for row in pending:
        description, status = fetch_job_description(
            str(row["original_url"]),
            linkedin_job_id=str(row["linkedin_job_id"]),
            timeout_seconds=timeout,
        )
        save_job_description(
            connection,
            str(row["job_key"]),
            description,
            status,
            datetime.now(timezone.utc).isoformat(),
        )
        connection.commit()
        found += description is not None
        status_counts[status] = status_counts.get(status, 0) + 1
        attempted_keys.append(str(row["job_key"]))

    if pending:
        statuses = ", ".join(
            f"{status}={count}" for status, count in sorted(status_counts.items())
        )
        LOGGER.info(
            "Fetched descriptions for %d of %d LinkedIn jobs (%s)",
            found,
            len(pending),
            statuses,
        )
    return attempted_keys


def run_pipeline_test() -> None:
    config = load_config()
    with tempfile.TemporaryDirectory(prefix="jsn-pipeline-test-") as temp_dir:
        database_path = Path(temp_dir) / "jobs.sqlite3"
        connection = connect(str(database_path))
        try:
            with closing(fetch_unprocessed_messages(config["gmail"], set(), max_messages=1)) as messages:
                message = next(messages, None)
            if message is None:
                raise SystemExit(
                    "No recent email was found for the configured Gmail label; "
                    "no LLM request or Discord post was made."
                )

            candidates = parse_linkedin_alert(message)
            if not candidates:
                raise SystemExit(
                    "The selected email contained no LinkedIn job cards; "
                    "no LLM request or Discord post was made."
                )

            candidate = candidates[0]
            store_job(connection, candidate)
            store_message(connection, message.message_id, message.internal_date)
            connection.commit()
            LOGGER.info(
                "Pipeline test selected one email and its first job card "
                "(1 of %d parsed jobs)",
                len(candidates),
            )

            now = datetime.now(timezone.utc)
            max_age = max(1, int(config["database"].get("max_job_age_days", 30)))
            cutoff = (now - timedelta(days=max_age)).isoformat()
            config_hash = scoring_config_hash(config)
            detail_limit = min(
                1,
                max(0, int(config.get("job_details", {}).get("max_per_run", 3))),
            )
            _enrich_pending_jobs(connection, config, cutoff, detail_limit)

            jobs = pending_scoring(connection, config_hash, cutoff)
            if not jobs:
                raise SystemExit(
                    "The selected job is outside the configured scoring window; "
                    "no LLM request or Discord post was made."
                )

            LOGGER.info("Scoring test job 1/1")
            job = jobs[0]
            analysis = analyze_job(dict(job), config)
            save_analysis(connection, str(job["job_key"]), analysis, config_hash)
            connection.commit()

            report_rows = report_candidates(connection, cutoff)
            report = [_report_job(dict(row)) for row in report_rows]
            local_now = datetime.now(TAIPEI)
            week_key = local_now.strftime("%G-W%V")
            title_prefix = config["discord"].get("title_prefix", "LinkedIn 職缺週報")
            title = f"【流程測試】{title_prefix} {week_key} {local_now:%m-%d %H:%M}"
            thread_id = post_weekly_report(
                report,
                title,
                config["discord"].get("webhook_env", "DISCORD_FORUM_WEBHOOK_URL"),
            )
            save_report(
                connection,
                week_key,
                thread_id,
                [str(row["job_key"]) for row in report],
                len(report),
            )
            LOGGER.info(
                "Pipeline test posted a marked Discord report with %d eligible jobs",
                len(report),
            )
            print(f"Pipeline test Discord thread created (Forum thread ID: {thread_id})")
        finally:
            connection.close()


def run_test_find_review() -> None:
    config = load_config()
    now = datetime.now(timezone.utc)
    max_age = max(1, int(config["database"].get("max_job_age_days", 30)))
    cutoff = (now - timedelta(days=max_age)).isoformat()
    config_hash = scoring_config_hash(config)
    detail_limit = min(
        1,
        max(0, int(config.get("job_details", {}).get("max_per_run", 3))),
    )
    messages_checked = 0
    jobs_seen = 0
    jobs_tested = 0
    seen_job_keys: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="jsn-find-review-test-") as temp_dir:
        connection = connect(str(Path(temp_dir) / "jobs.sqlite3"))
        try:
            messages = fetch_unprocessed_messages(config["gmail"], set())
            for message in messages:
                messages_checked += 1
                candidates = parse_linkedin_alert(message)
                store_message(connection, message.message_id, message.internal_date)
                connection.commit()

                for candidate in candidates:
                    if candidate.job_key in seen_job_keys:
                        continue
                    seen_job_keys.add(candidate.job_key)
                    jobs_seen += 1
                    store_job(connection, candidate)
                    connection.commit()

                    _enrich_pending_jobs(connection, config, cutoff, detail_limit)
                    pending = pending_scoring(connection, config_hash, cutoff)
                    job = next(
                        (row for row in pending if row["job_key"] == candidate.job_key),
                        None,
                    )
                    if job is None:
                        LOGGER.info(
                            "Skipping test job %d; it is outside the scoring window",
                            jobs_seen,
                        )
                        continue

                    jobs_tested += 1
                    LOGGER.info("Testing job %d (candidate %d)", jobs_tested, jobs_seen)
                    try:
                        analysis = analyze_job(dict(job), config)
                    except json.JSONDecodeError as error:
                        LOGGER.warning(
                            "Skipping job %d after invalid JSON from the LLM "
                            "at line %d, column %d",
                            jobs_tested,
                            error.lineno,
                            error.colno,
                        )
                        continue
                    save_analysis(
                        connection,
                        str(candidate.job_key),
                        analysis,
                        config_hash,
                    )
                    connection.commit()
                    LOGGER.info(
                        "Tested job %d: decision=%s, score=%d",
                        jobs_tested,
                        analysis["decision"],
                        analysis["score"],
                    )

                    if analysis["decision"] != "review":
                        continue

                    report_rows = report_candidates(connection, cutoff)
                    selected_rows = [
                        row for row in report_rows if row["job_key"] == candidate.job_key
                    ]
                    if not selected_rows:
                        raise RuntimeError(
                            "A review test job was not available for the Discord report"
                        )

                    report = [_report_job(dict(row)) for row in selected_rows]
                    local_now = datetime.now(TAIPEI)
                    week_key = local_now.strftime("%G-W%V")
                    title_prefix = config["discord"].get(
                        "title_prefix", "LinkedIn 職缺週報"
                    )
                    title = (
                        f"【待確認職缺測試】{title_prefix} {week_key} "
                        f"{local_now:%m-%d %H:%M}"
                    )
                    thread_id = post_weekly_report(
                        report,
                        title,
                        config["discord"].get(
                            "webhook_env", "DISCORD_FORUM_WEBHOOK_URL"
                        ),
                    )
                    save_report(
                        connection,
                        week_key,
                        thread_id,
                        [str(row["job_key"]) for row in report],
                        len(report),
                    )
                    LOGGER.info(
                        "Found a review job after testing %d jobs across %d emails",
                        jobs_tested,
                        messages_checked,
                    )
                    print(
                        "Review-job test Discord thread created "
                        f"(Forum thread ID: {thread_id})"
                    )
                    return

            if messages_checked == 0:
                print("No recent email matched the configured Gmail label; no Discord post was made.")
            elif jobs_seen == 0:
                print("No LinkedIn job cards were found; no Discord post was made.")
            elif jobs_tested == 0:
                print(
                    "No job cards were within the configured scoring window; "
                    "no LLM request or Discord post was made."
                )
            else:
                LOGGER.info(
                    "No review job found after testing %d jobs across %d emails",
                    jobs_tested,
                    messages_checked,
                )
                print(
                    f"No review job found after testing {jobs_tested} jobs; "
                    "no Discord post was made."
                )
        finally:
            connection.close()


def run(preview_llm_limit: int | None = None, test_llm: bool = False) -> None:
    config = load_config()
    database_path = str(config["database"].get("path", "data/jobs.sqlite3"))
    connection = connect(database_path)
    try:
        processed = processed_message_ids(connection)
        gmail_messages = fetch_unprocessed_messages(config["gmail"], processed)
        new_message_count = 0
        new_job_count = 0
        for message in gmail_messages:
            candidates = parse_linkedin_alert(message)
            new_message_count += 1
            new_job_count += len(candidates)
            if preview_llm_limit is not None:
                for candidate in candidates:
                    store_job(connection, candidate)
                continue
            for candidate in candidates:
                store_job(connection, candidate)
            store_message(connection, message.message_id, message.internal_date)
            connection.commit()

        if preview_llm_limit is not None:
            LOGGER.info(
                "Preview found %d unprocessed Gmail messages and %d job cards; "
                "none were marked processed",
                new_message_count,
                new_job_count,
            )
            now = datetime.now(timezone.utc)
            max_age = max(1, int(config["database"].get("max_job_age_days", 30)))
            cutoff = (now - timedelta(days=max_age)).isoformat()
            config_hash = scoring_config_hash(config)
            detail_limit = min(
                preview_llm_limit,
                max(0, int(config.get("job_details", {}).get("max_per_run", 3))),
            )
            enriched_job_keys = set(
                _enrich_pending_jobs(connection, config, cutoff, detail_limit)
            )
            jobs = pending_scoring(connection, config_hash, cutoff)
            if enriched_job_keys:
                jobs.sort(key=lambda job: str(job["job_key"]) not in enriched_job_keys)
            if not jobs:
                print("No new or pending jobs are available to preview.")
                return
            selected = jobs[:preview_llm_limit]
            print(
                f"Previewing {len(selected)} of {len(jobs)} job inputs for the LLM "
                "(no LLM request or Discord post was made):"
            )
            for index, job in enumerate(selected, start=1):
                print(f"\n--- LLM input {index} ---")
                print(f"Job detail fetch status: {job.get('description_status') or 'not_attempted'}")
                print(
                    json.dumps(
                        build_job_instructions(job, config),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return

        LOGGER.info(
            "Processed %d Gmail messages and found %d job cards",
            new_message_count,
            new_job_count,
        )

        now = datetime.now(timezone.utc)
        max_age = max(1, int(config["database"].get("max_job_age_days", 30)))
        cutoff = (now - timedelta(days=max_age)).isoformat()
        config_hash = scoring_config_hash(config)

        if test_llm:
            detail_limit = min(
                1,
                max(0, int(config.get("job_details", {}).get("max_per_run", 3))),
            )
            enriched_job_keys = set(
                _enrich_pending_jobs(connection, config, cutoff, detail_limit)
            )
            jobs = pending_scoring(connection, config_hash, cutoff)
            if enriched_job_keys:
                jobs.sort(key=lambda job: str(job["job_key"]) not in enriched_job_keys)
            if not jobs:
                print("No new or pending jobs are available for an LLM test.")
                return

            job = jobs[0]
            LOGGER.info(
                "Sending one job to the LLM for testing: %s at %s",
                job["title"],
                job["company"],
            )
            analysis = analyze_job(job, config)
            save_analysis(connection, str(job["job_key"]), analysis, config_hash)
            connection.commit()
            print("LLM test result:")
            print(json.dumps(analysis, ensure_ascii=False, indent=2))
            LOGGER.info("LLM test finished; Discord was not called")
            return

        detail_limit = max(0, int(config.get("job_details", {}).get("max_per_run", 3)))
        _enrich_pending_jobs(connection, config, cutoff, detail_limit)
        to_score = pending_scoring(connection, config_hash, cutoff)
        total_to_score = len(to_score)
        for index, row in enumerate(to_score, start=1):
            LOGGER.info("Scoring job %d/%d", index, total_to_score)
            analysis = analyze_job(dict(row), config)
            save_analysis(connection, str(row["job_key"]), analysis, config_hash)
            connection.commit()
            LOGGER.info("Scored job %d/%d", index, total_to_score)
        LOGGER.info("Scored %d unreported jobs", len(to_score))

        local_now = datetime.now(TAIPEI)
        week_key = local_now.strftime("%G-W%V")
        if report_exists(connection, week_key):
            LOGGER.info("A weekly report already exists for %s", week_key)
            return

        report_rows = report_candidates(connection, cutoff)
        report = [_report_job(dict(row)) for row in report_rows]
        title = f"{config['discord'].get('title_prefix', 'LinkedIn 職缺週報')} {week_key}"
        thread_id = post_weekly_report(
            report,
            title,
            config["discord"].get("webhook_env", "DISCORD_FORUM_WEBHOOK_URL"),
        )
        save_report(
            connection,
            week_key,
            thread_id,
            [str(row["job_key"]) for row in report],
            len(report),
        )
        LOGGER.info("Posted weekly Forum report %s with %d jobs", week_key, len(report))
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect and score LinkedIn job alerts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preview-llm-input",
        nargs="?",
        const=3,
        type=int,
        metavar="LIMIT",
        help="print up to LIMIT exact LLM input payloads, then exit (default: 3)",
    )
    mode.add_argument(
        "--test-pipeline",
        action="store_true",
        help=(
            "read one recent Gmail email, process its first job through the LLM, "
            "and post a marked test report to Discord"
        ),
    )
    mode.add_argument(
        "--test-find-review",
        action="store_true",
        help=(
            "test recent labeled jobs one by one until one needs review, then "
            "post that job as a marked Discord report"
        ),
    )
    mode.add_argument(
        "--test-llm",
        action="store_true",
        help="send exactly one pending job to the LLM, save its result, and exit without Discord",
    )
    mode.add_argument(
        "--test-discord",
        nargs="?",
        const="DISCORD_FORUM_WEBHOOK_URL",
        metavar="WEBHOOK_ENV",
        help=(
            "create a test Forum post using the named webhook environment variable "
            "(default: DISCORD_FORUM_WEBHOOK_URL)"
        ),
    )
    mode.add_argument(
        "--test-discord-report",
        nargs="?",
        const=3,
        type=int,
        metavar="LIMIT",
        help="post LIMIT eligible, already-scored jobs in the weekly report format (default: 3)",
    )
    args = parser.parse_args()
    if args.preview_llm_input is not None and args.preview_llm_input < 1:
        parser.error("--preview-llm-input LIMIT must be at least 1")
    if args.test_discord_report is not None and args.test_discord_report < 1:
        parser.error("--test-discord-report LIMIT must be at least 1")
    if args.test_discord is not None:
        thread_id = post_discord_test(args.test_discord)
        print(f"Discord test post created successfully (Forum thread ID: {thread_id})")
        return
    if args.test_pipeline:
        run_pipeline_test()
        return
    if args.test_find_review:
        run_test_find_review()
        return
    if args.test_discord_report is not None:
        run_discord_report_test(args.test_discord_report)
        return
    run(preview_llm_limit=args.preview_llm_input, test_llm=args.test_llm)


if __name__ == "__main__":
    main()
