from datetime import datetime, timezone

import pytest

from jsn import pipeline
from jsn.database import connect, processed_message_ids, store_message
from jsn.models import GmailMessage


def _email(message_id, job_id):
    return GmailMessage(
        message_id=message_id,
        internal_date=datetime.now(timezone.utc).isoformat(),
        subject="Job alert",
        body=(
            f'<div><a href="https://www.linkedin.com/jobs/view/{job_id}/">Engineer {job_id}</a>'
            '<a href="https://www.linkedin.com/company/example/">Example Co.</a>'
            '<span>Taipei, Taiwan</span></div>'
        ),
    )


def _config(database_path):
    return {
        "gmail": {"label_name": "LinkedIn Job Alerts", "lookback_days": 30},
        "database": {"path": str(database_path), "max_job_age_days": 30},
        "profile": {},
        "filters": {},
        "llm": {},
        "job_details": {"max_per_run": 1},
        "discord": {"title_prefix": "Jobs", "webhook_env": "TEST_WEBHOOK"},
    }


def _analysis(decision):
    return {"decision": decision, "score": 75, "fields": {}}


@pytest.mark.parametrize("decision,expected_count", [("review", 1), ("reject", 0)])
def test_pipeline_test_runs_one_job_and_preserves_normal_state(monkeypatch, tmp_path, decision, expected_count):
    database_path = tmp_path / "regular.sqlite3"
    connection = connect(str(database_path))
    store_message(connection, "previous-message", datetime.now(timezone.utc).isoformat())
    connection.commit()
    connection.close()
    config = _config(database_path)
    monkeypatch.setattr(pipeline, "load_config", lambda: config)
    closed = []

    def messages(gmail_config, processed, max_messages=None):
        assert processed == set()
        assert max_messages == 1
        try:
            yield _email("new-message", "111")
        finally:
            closed.append(True)

    monkeypatch.setattr(pipeline, "fetch_unprocessed_messages", messages)
    fetched = []
    def fetch_description(url, linkedin_job_id, timeout_seconds):
        fetched.append(linkedin_job_id)
        return "Detailed job description", "found"
    monkeypatch.setattr(pipeline, "fetch_job_description", fetch_description)
    scored = []
    def analyze(job, cfg):
        scored.append(job)
        assert job["description_text"] == "Detailed job description"
        return _analysis(decision)
    monkeypatch.setattr(pipeline, "analyze_job", analyze)
    posted = []
    monkeypatch.setattr(pipeline, "post_weekly_report", lambda report, title, webhook: posted.append((report, title, webhook)) or "thread-1")

    pipeline.run_pipeline_test()

    assert closed == [True]
    assert fetched == ["111"]
    assert len(scored) == 1
    assert len(posted) == 1
    report, title, webhook = posted[0]
    assert "【流程測試】" in title
    assert webhook == "TEST_WEBHOOK"
    assert len(report) == expected_count
    if report:
        assert report[0]["analysis"]["decision"] == decision
    connection = connect(str(database_path))
    try:
        assert processed_message_ids(connection) == {"previous-message"}
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM weekly_reports").fetchone()[0] == 0
    finally:
        connection.close()


def test_find_review_posts_first_review_and_stops(monkeypatch, tmp_path):
    config = _config(tmp_path / "regular.sqlite3")
    monkeypatch.setattr(pipeline, "load_config", lambda: config)
    monkeypatch.setattr(pipeline, "fetch_unprocessed_messages", lambda *args: iter([_email("one", "111"), _email("two", "222"), _email("three", "333")]))
    monkeypatch.setattr(pipeline, "fetch_job_description", lambda *args, **kwargs: ("Description", "found"))
    scored = []
    def analyze(job, cfg):
        scored.append(job["linkedin_job_id"])
        return _analysis("review" if job["linkedin_job_id"] == "222" else "pass")
    monkeypatch.setattr(pipeline, "analyze_job", analyze)
    posted = []
    monkeypatch.setattr(pipeline, "post_weekly_report", lambda report, title, webhook: posted.append((report, title)) or "thread-1")

    pipeline.run_test_find_review()

    assert scored == ["111", "222"]
    assert len(posted) == 1
    assert posted[0][0][0]["linkedin_job_id"] == "222"
    assert "【待確認職缺測試】" in posted[0][1]
    assert not (tmp_path / "regular.sqlite3").exists()
