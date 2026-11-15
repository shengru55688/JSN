from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import GmailMessage, JobCandidate


JOB_ID_PATTERN = re.compile(r"/jobs/view/(\d+)", re.IGNORECASE)
CTA_TEXT = {
    "view job",
    "see job",
    "view",
    "apply",
    "easy apply",
    "查看職缺",
    "查看工作",
    "立即應徵",
}


def _job_url(href: str) -> str:
    absolute = urljoin("https://www.linkedin.com", href)
    parsed = urlparse(absolute)
    if "/redir/redirect" in parsed.path:
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target:
            absolute = unquote(target)
    return absolute


def _linkedin_job_id(href: str) -> str:
    url = _job_url(href)
    match = JOB_ID_PATTERN.search(url)
    if match:
        return match.group(1)
    current_id = parse_qs(urlparse(url).query).get("currentJobId", [""])[0]
    return current_id if current_id.isdigit() else ""


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _card_for(anchor: Tag, job_id: str) -> Tag:
    title = _clean_text(anchor.get_text(" ", strip=True))
    best = anchor.parent if isinstance(anchor.parent, Tag) else anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
            continue
        linked_ids = {
            _linkedin_job_id(link.get("href", ""))
            for link in parent.find_all("a", href=True)
        }
        linked_ids.discard("")
        if linked_ids and linked_ids != {job_id}:
            continue
        text = _clean_text(parent.get_text(" ", strip=True))
        if len(text) > len(title) + 8 and len(text) <= 1800:
            best = parent
            if len(text) >= 80:
                break
    return best


def _job_links(soup: BeautifulSoup) -> list[Tag]:
    return [
        anchor
        for anchor in soup.find_all("a", href=True)
        if _linkedin_job_id(anchor.get("href", ""))
    ]


def _candidate_for(
    anchor: Tag, message: GmailMessage, job_id: str
) -> JobCandidate:
    title = _clean_text(anchor.get_text(" ", strip=True))
    if title.casefold() in CTA_TEXT:
        title = ""

    card = _card_for(anchor, job_id)
    lines = [_clean_text(line) for line in card.stripped_strings]
    lines = [line for line in lines if line and line.casefold() not in CTA_TEXT]
    if title:
        lines = [line for line in lines if line != title]

    company = ""
    for link in card.find_all("a", href=True):
        if "/company/" in link.get("href", ""):
            company = _clean_text(link.get_text(" ", strip=True))
            if company:
                break
    if not company and lines:
        company = lines[0]
    if company in lines:
        lines.remove(company)

    if lines and "·" in lines[0]:
        # LinkedIn alert cards may prefix the location with the company name.
        lines[0] = _clean_text(lines[0].split("·", 1)[1])
    location = lines[0] if lines else ""
    card_text = "\n".join(lines)
    source_text = (
        f"Title: {title or '未提供'}\n"
        f"Company: {company or '未提供'}\n"
        f"Location: {location or '未提供'}\n"
        f"{card_text}"
    ).strip()
    original_url = _job_url(anchor.get("href", ""))
    stable = job_id or f"{title}|{company}|{location}|{original_url}"
    job_key = hashlib.sha256(stable.casefold().encode("utf-8")).hexdigest()

    return JobCandidate(
        job_key=job_key,
        linkedin_job_id=job_id,
        title=title or "未提供",
        company=company or "未提供",
        location=location or "未提供",
        original_url=original_url,
        source_text=source_text,
        received_at=message.internal_date,
        gmail_message_id=message.message_id,
    )


def parse_linkedin_alert(message: GmailMessage) -> list[JobCandidate]:
    soup = BeautifulSoup(message.body, "html.parser")
    by_job_id: dict[str, JobCandidate] = {}
    for anchor in _job_links(soup):
        job_id = _linkedin_job_id(anchor.get("href", ""))
        candidate = _candidate_for(anchor, message, job_id)
        previous = by_job_id.get(job_id)
        if previous is None or (previous.title == "未提供" and candidate.title != "未提供"):
            by_job_id[job_id] = candidate

    return list(by_job_id.values())
