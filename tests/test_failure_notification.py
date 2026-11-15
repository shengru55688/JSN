import json
import os
import subprocess
from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/weekly.yml"


def _notification_job():
    workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    return workflow["jobs"]["notify-failure"]


def test_notification_runs_only_when_weekly_job_fails():
    job = _notification_job()
    assert job["needs"] == "collect-and-notify"
    assert "always()" in job["if"]
    assert "needs.collect-and-notify.result == 'failure'" in job["if"]
    assert job["steps"][0]["env"]["DISCORD_FAILURE_WEBHOOK_URL"] == "${{ secrets.DISCORD_FAILURE_WEBHOOK_URL }}"


def test_notification_sends_run_link_and_propagates_delivery_error(tmp_path):
    output = tmp_path / "curl.json"
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "json.dump(sys.argv[1:], open(os.environ['CURL_OUTPUT'], 'w'))\n"
        "sys.exit(int(os.environ.get('CURL_EXIT', '0')))\n"
    )
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CURL_OUTPUT": str(output),
        "DISCORD_FAILURE_WEBHOOK_URL": "https://example.invalid/webhook-secret",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_WORKFLOW": "Weekly job search",
        "GITHUB_REF_NAME": 'feature/quoted"branch',
    }
    command = _notification_job()["steps"][0]["run"]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    args = json.loads(output.read_text())
    payload = json.loads(args[args.index("--data") + 1])
    assert "Weekly job search failed" in payload["content"]
    assert 'feature/quoted"branch' in payload["content"]
    assert "https://github.com/owner/repo/actions/runs/123456" in payload["content"]
    assert payload["allowed_mentions"] == {"parse": []}
    assert "webhook-secret" not in result.stdout + result.stderr

    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], env={**env, "CURL_EXIT": "22"}, capture_output=True, text=True)
    assert result.returncode == 22


def test_notification_reports_missing_webhook_without_posting(tmp_path):
    env = {**os.environ, "DISCORD_FAILURE_WEBHOOK_URL": ""}
    command = _notification_job()["steps"][0]["run"]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Missing repository secret DISCORD_FAILURE_WEBHOOK_URL" in result.stderr
