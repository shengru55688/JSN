from __future__ import annotations

import base64
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

from .models import GmailMessage


_IMAP_HOST = "imap.gmail.com"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _mailbox_name(label: str) -> str:
    """Encode a Gmail label as an IMAP modified UTF-7 quoted mailbox name."""
    if any(ord(char) < 32 or ord(char) == 127 for char in label):
        raise ValueError("Gmail label cannot contain control characters")
    encoded: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            encoded.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in label:
        if " " <= char <= "~":
            flush()
            encoded.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return '"' + "".join(encoded).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parts(message: Message) -> tuple[list[str], list[str]]:
    html_parts: list[str] = []
    text_parts: list[str] = []

    def visit(part: Message) -> None:
        if part.get_content_disposition() == "attachment":
            return
        if part.is_multipart():
            for child in part.iter_parts():
                visit(child)
            return
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            return
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return
        try:
            decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        (html_parts if content_type == "text/html" else text_parts).append(decoded)

    visit(message)
    return html_parts, text_parts


def _metadata(response: list[Any]) -> tuple[str, datetime]:
    # X-GM-MSGID is the decimal form of the hexadecimal ID returned by the Gmail API.
    data = b" ".join(item for item in response if isinstance(item, bytes))
    message_id = re.search(rb"\bX-GM-MSGID\s+(\d+)", data, re.IGNORECASE)
    internal_date = re.search(rb'\bINTERNALDATE\s+"([^"]+)"', data, re.IGNORECASE)
    if not message_id or not internal_date:
        raise RuntimeError("Gmail IMAP returned incomplete message metadata")
    try:
        received_at = parsedate_to_datetime(internal_date.group(1).decode("ascii"))
        if received_at.tzinfo is None:
            raise ValueError("missing timezone")
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError("Gmail IMAP returned an invalid message date") from exc
    return format(int(message_id.group(1)), "x"), received_at.astimezone(timezone.utc)


def fetch_unprocessed_messages(
    gmail_config: dict[str, Any],
    processed_ids: set[str],
    max_messages: int | None = None,
) -> Iterator[GmailMessage]:
    if max_messages is not None and max_messages < 1:
        raise ValueError("max_messages must be at least 1")
    required = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError(f"Set these environment variables or GitHub Actions secrets: {', '.join(missing)}")

    label_name = str(gmail_config.get("label_name", "LinkedIn Job Alerts"))
    mailbox = _mailbox_name(label_name)
    lookback_days = max(1, int(gmail_config.get("lookback_days", 30)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    # IMAP SINCE compares calendar dates in the server's timezone; include a
    # buffer day and apply the precise UTC cutoff to each message below.
    since_date = cutoff - timedelta(days=1)
    since = f"{since_date.day:02d}-{_MONTHS[since_date.month - 1]}-{since_date.year}"

    with imaplib.IMAP4_SSL(_IMAP_HOST, timeout=30) as client:
        try:
            client.login(os.environ["GMAIL_ADDRESS"].strip(), os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").strip())
        except imaplib.IMAP4.error as exc:
            raise RuntimeError("Gmail IMAP login failed; check the address and app password") from exc
        try:
            status, _ = client.select(mailbox, readonly=True)
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(f"Cannot open Gmail label {label_name!r}; make sure it is visible in IMAP") from exc
        if status != "OK":
            raise RuntimeError(f"Cannot open Gmail label {label_name!r}; make sure it is visible in IMAP")

        status, result = client.uid("SEARCH", None, "SINCE", since)
        if status != "OK" or not result or result[0] is None:
            raise RuntimeError("Gmail IMAP could not search the selected label")
        yielded_messages = 0
        # Gmail returns UIDs in ascending order; process the newest first.
        for uid in reversed(result[0].split()):
            status, metadata = client.uid("FETCH", uid, "(X-GM-MSGID INTERNALDATE)")
            if status != "OK":
                raise RuntimeError("Gmail IMAP could not fetch message metadata")
            if not any(isinstance(item, bytes) and b"X-GM-MSGID" in item.upper() for item in metadata):
                raise RuntimeError("Gmail IMAP returned incomplete message metadata")
            message_id, received_at = _metadata(metadata)
            if message_id in processed_ids or received_at < cutoff:
                continue
            status, fetched = client.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("Gmail IMAP could not fetch a message")
            bodies = [item[1] for item in fetched if isinstance(item, tuple) and isinstance(item[1], bytes)]
            if len(bodies) != 1:
                raise RuntimeError("Gmail IMAP returned an incomplete message")
            message = BytesParser(policy=policy.default).parsebytes(bodies[0])
            html_parts, text_parts = _parts(message)
            subject = str(message.get("subject", ""))
            body = "\n".join(html_parts or text_parts)
            if subject:
                body = f"Subject: {subject}\n{body}"
            yielded_messages += 1
            yield GmailMessage(message_id, received_at.isoformat(), subject, body)
            if max_messages is not None and yielded_messages >= max_messages:
                return
