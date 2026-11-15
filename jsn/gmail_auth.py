from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ENV_KEY = "GMAIL_REFRESH_TOKEN"


def _env_assignment(line: str, key: str) -> re.Match[str] | None:
    return re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*?)\s*$", line)


def _save_refresh_token(path: Path, token: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    replacement = f"{ENV_KEY}={token}"
    found = False
    for index, line in enumerate(lines):
        match = _env_assignment(line, ENV_KEY)
        if match is None:
            continue
        found = True
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        lines[index] = replacement + newline

    if not found:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.append(f"{replacement}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.writelines(lines)
            temp_name = temporary.name
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authorize Gmail locally and save a refresh token to an env file."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="env file to update (default: .env)",
    )
    args = parser.parse_args()

    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    missing = [
        name
        for name, value in (
            ("GMAIL_CLIENT_ID", client_id),
            ("GMAIL_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        parser.error(
            f"Set {', '.join(missing)} in the environment or the env file loaded by uv"
        )

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(
        client_config,
        scopes=[GMAIL_READONLY_SCOPE],
        autogenerate_code_verifier=True,
    )

    print("Opening Google authorization in your browser…")
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token. Revoke this app's access in your Google Account "
            "and run the command again."
        )

    _save_refresh_token(args.env_file, credentials.refresh_token)
    print(f"Refresh token saved to {args.env_file}; the token was not printed.")
    print("Run the JSN pipeline with: uv run --env-file .env jsn")


if __name__ == "__main__":
    main()
