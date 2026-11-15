from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class JobCandidate:
    job_key: str
    linkedin_job_id: str
    title: str
    company: str
    location: str
    original_url: str
    source_text: str
    received_at: str
    gmail_message_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobCandidate:
        return cls(**data)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class GmailMessage:
    message_id: str
    internal_date: str
    subject: str
    body: str
