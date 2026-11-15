import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from jsn.config import load_config
from jsn.database import connect, store_message


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/weekly.yml"


def _workflow_steps():
    workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["collect-and-notify"]
    return job, {step["name"]: step for step in job["steps"]}


def _run_step(step, env):
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
    )


def test_r2_and_application_use_same_database_after_failed_run(tmp_path, monkeypatch):
    job, steps = _workflow_steps()
    assert job["env"]["JSN_DATABASE_PATH"] == "/tmp/jsn/jobs.sqlite3"
    assert "always()" in steps["Persist SQLite state to Cloudflare R2"]["if"]
    local_db = tmp_path / "runner" / "jobs.sqlite3"
    remote_db = tmp_path / "fake-r2.sqlite3"
    wrong_db = tmp_path / "configured" / "jobs.sqlite3"
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({
        "profile": {"skills": ["Python"]},
        "gmail": {}, "filters": {}, "llm": {}, "discord": {},
        "database": {"path": str(wrong_db)},
    }))
    aws = tmp_path / "aws"
    aws.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "from pathlib import Path\n"
        "a = sys.argv[1:]\n"
        "remote = Path(os.environ['FAKE_R2_OBJECT'])\n"
        "if a[:2] == ['s3api', 'head-object']:\n"
        "    if not remote.exists():\n"
        "        print('404 Not Found', file=sys.stderr); sys.exit(1)\n"
        "elif a[:2] == ['s3', 'cp']:\n"
        "    src, dst = a[2:4]\n"
        "    if src.startswith('s3://'): shutil.copyfile(remote, dst)\n"
        "    elif dst.startswith('s3://'): shutil.copyfile(src, remote)\n"
        "    else: sys.exit(2)\n"
        "else: sys.exit(2)\n"
    )
    aws.chmod(0o755)
    env = {
        **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "JSN_DATABASE_PATH": str(local_db), "JSN_CONFIG_PATH": str(settings),
        "FAKE_R2_OBJECT": str(remote_db), "RUNNER_TEMP": str(tmp_path),
        "AWS_ACCESS_KEY_ID": "dummy", "AWS_SECRET_ACCESS_KEY": "dummy",
        "R2_ACCOUNT_ID": "account", "R2_BUCKET_NAME": "bucket", "R2_OBJECT_KEY": "state.sqlite3",
    }
    restore = _run_step(steps["Restore SQLite state from Cloudflare R2"], env)
    assert restore.returncode == 0
    assert "starting with an empty database" in restore.stdout
    monkeypatch.setenv("JSN_CONFIG_PATH", str(settings))
    monkeypatch.setenv("JSN_DATABASE_PATH", str(local_db))
    config = load_config()
    assert config["database"]["path"] == str(local_db)
    connection = connect(config["database"]["path"])
    store_message(connection, "saved-before-failure", "2026-11-15T00:00:00+00:00")
    connection.commit()
    connection.close()

    # The workflow's always() upload runs even when collection failed.
    persist = _run_step(steps["Persist SQLite state to Cloudflare R2"], env)
    assert persist.returncode == 0, persist.stderr
    assert "SQLite state uploaded successfully" in persist.stdout
    assert not wrong_db.exists()
    with sqlite3.connect(remote_db) as uploaded:
        assert uploaded.execute("SELECT message_id FROM email_messages").fetchone()[0] == "saved-before-failure"


def test_missing_database_is_reported_without_claiming_upload(tmp_path):
    _, steps = _workflow_steps()
    env = {**os.environ, "JSN_DATABASE_PATH": str(tmp_path / "missing.sqlite3")}
    result = _run_step(steps["Persist SQLite state to Cloudflare R2"], env)
    assert result.returncode == 0
    assert "no R2 upload was made" in result.stdout
    assert "uploaded successfully" not in result.stdout


def test_empty_database_path_override_is_rejected(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({
        "profile": {"skills": ["Python"]},
        "gmail": {}, "filters": {}, "llm": {}, "discord": {}, "database": {},
    }))
    monkeypatch.setenv("JSN_CONFIG_PATH", str(settings))
    monkeypatch.setenv("JSN_DATABASE_PATH", " ")
    with pytest.raises(ValueError, match="JSN_DATABASE_PATH must not be empty"):
        load_config()
