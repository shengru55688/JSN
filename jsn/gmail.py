from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Any, Iterator

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .models import GmailMessage


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _decode_body(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _collect_parts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    html_parts: list[str] = []
    text_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = _decode_body(data)
            if mime_type == "text/html":
                html_parts.append(decoded)
            elif mime_type == "text/plain":
                text_parts.append(decoded)
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    return html_parts, text_parts


def _message_headers(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers", [])
    return {item.get("name", "").lower(): item.get("value", "") for item in headers}


def fetch_unprocessed_messages(
    gmail_config: dict[str, Any], processed_ids: set[str]
) -> Iterator[GmailMessage]:
    required = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Set these GitHub Actions secrets: {', '.join(missing)}")

    credentials = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=[GMAIL_READONLY_SCOPE],
    )
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    label_name = gmail_config.get("label_name", "LinkedIn Job Alerts")
    labels_response = service.users().labels().list(userId="me").execute()
    label = next(
        (item for item in labels_response.get("labels", []) if item.get("name") == label_name),
        None,
    )
    if label is None:
        raise RuntimeError(
            f"Gmail label {label_name!r} was not found; create it and apply it to LinkedIn alerts"
        )

    lookback_days = max(1, int(gmail_config.get("lookback_days", 30)))
    page_size = min(500, max(1, int(gmail_config.get("max_results_per_page", 100))))
    page_token = None
    while True:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=[label["id"]],
                q=f"newer_than:{lookback_days}d",
                maxResults=page_size,
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("messages", []):
            message_id = item["id"]
            if message_id in processed_ids:
                continue

            message = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            payload = message.get("payload", {})
            html_parts, text_parts = _collect_parts(payload)
            body = "\n".join(html_parts or text_parts)
            headers = _message_headers(payload)
            internal_ms = int(message.get("internalDate", "0"))
            received_at = datetime.fromtimestamp(internal_ms / 1000, timezone.utc).isoformat()
            subject = headers.get("subject", "")
            if subject:
                body = f"Subject: {subject}\n{body}"
            yield GmailMessage(message_id, received_at, subject, body)

        page_token = response.get("nextPageToken")
        if not page_token:
            break
