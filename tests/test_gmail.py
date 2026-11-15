from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import imaplib

import pytest

from jsn.gmail import _mailbox_name, fetch_unprocessed_messages


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def login(self, address, password):
        self.calls.append(("login", address, password))
        return "OK", [b"authenticated"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        uid, query = args
        message = self.messages[uid]
        if query == "(X-GM-MSGID INTERNALDATE)":
            date = message["date"].strftime("%d-%b-%Y %H:%M:%S %z")
            return "OK", [f'1 (UID {uid.decode()} X-GM-MSGID {message["id"]} INTERNALDATE "{date}")'.encode()]
        if query == "(BODY.PEEK[])":
            return "OK", [(b"1 (BODY[]", message["body"]), b")"]
        raise AssertionError(f"Unexpected IMAP command: {command} {args}")


def test_imap_preserves_existing_gmail_ids_and_reads_without_marking_seen(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    message = EmailMessage()
    message["Subject"] = "LinkedIn 職缺"
    message.set_content("plain fallback")
    message.add_alternative('<a href="https://www.linkedin.com/jobs/view/123456/">職缺</a>', subtype="html")
    message.add_attachment("private attachment", filename="notes.txt")
    old_api_id = "abcdef0123456789"
    fresh_api_id = "abcdef012345678a"
    server = FakeIMAP({
        b"1": {"id": int(old_api_id, 16), "date": now, "body": b"already processed"},
        b"2": {"id": int(fresh_api_id, 16), "date": now, "body": message.as_bytes()},
        b"3": {"id": 99, "date": now - timedelta(days=31), "body": b"too old"},
    })
    monkeypatch.setenv("GMAIL_ADDRESS", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr("jsn.gmail.imaplib.IMAP4_SSL", lambda host, timeout: server)

    result = list(fetch_unprocessed_messages({"label_name": "LinkedIn Job Alerts", "lookback_days": 30}, {old_api_id}))

    assert len(result) == 1
    assert result[0].message_id == fresh_api_id
    assert result[0].internal_date == now.isoformat()
    assert result[0].subject == "LinkedIn 職缺"
    assert "<a href=" in result[0].body
    assert "plain fallback" not in result[0].body
    assert "private attachment" not in result[0].body
    assert ("login", "user@example.com", "abcdefghijklmnop") in server.calls
    assert ("select", '"LinkedIn Job Alerts"', True) in server.calls
    assert [call[1] for call in server.calls if call[0] == "FETCH" and call[2] == "(BODY.PEEK[])"] == [b"2"]
    assert server.closed


def test_mailbox_name_supports_unicode_and_quoted_characters():
    assert _mailbox_name('職缺 & "jobs"') == '"&gHd,Og- &- \\"jobs\\""'
    with pytest.raises(ValueError, match="control characters"):
        _mailbox_name("bad\r\nlabel")


def test_missing_credentials_fail_before_connecting(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="GMAIL_ADDRESS, GMAIL_APP_PASSWORD"):
        list(fetch_unprocessed_messages({}, set()))


def test_invalid_login_reports_setup_error(monkeypatch):
    server = FakeIMAP({})
    def fail_login(address, password):
        raise imaplib.IMAP4.error("authentication failed")
    server.login = fail_login
    monkeypatch.setenv("GMAIL_ADDRESS", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr("jsn.gmail.imaplib.IMAP4_SSL", lambda host, timeout: server)
    with pytest.raises(RuntimeError, match="check the address and app password"):
        list(fetch_unprocessed_messages({}, set()))
    assert server.closed


def test_incomplete_message_metadata_fails_safely(monkeypatch):
    server = FakeIMAP({b"1": {}})
    original_uid = server.uid
    def missing_id(command, *args):
        if command == "FETCH":
            return "OK", [b"1 (UID 1)"]
        return original_uid(command, *args)
    server.uid = missing_id
    monkeypatch.setenv("GMAIL_ADDRESS", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr("jsn.gmail.imaplib.IMAP4_SSL", lambda host, timeout: server)
    with pytest.raises(RuntimeError, match="incomplete message metadata"):
        list(fetch_unprocessed_messages({}, set()))


def test_max_messages_reads_only_the_newest_matching_email(monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    message = EmailMessage()
    message.set_content("example")
    server = FakeIMAP({
        b"1": {"id": 1, "date": now, "body": message.as_bytes()},
        b"2": {"id": 2, "date": now, "body": message.as_bytes()},
    })
    monkeypatch.setenv("GMAIL_ADDRESS", "user@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr("jsn.gmail.imaplib.IMAP4_SSL", lambda host, timeout: server)

    result = list(fetch_unprocessed_messages({}, set(), max_messages=1))

    assert [item.message_id for item in result] == ["2"]
    assert [call[1] for call in server.calls if call[0] == "FETCH" and call[2] == "(BODY.PEEK[])"] == [b"2"]
    with pytest.raises(ValueError, match="max_messages"):
        list(fetch_unprocessed_messages({}, set(), max_messages=0))
