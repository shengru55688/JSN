from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


MAX_DESCRIPTION_LENGTH = 12_000
MIN_DESCRIPTION_LENGTH = 80
MAX_REDIRECTS = 5
REDIRECT_CODES = {301, 302, 303, 307, 308}
DESCRIPTION_SELECTORS = (
    ".show-more-less-html__markup",
    ".jobs-description-content__text",
    ".jobs-description__content",
    ".jobs-box__html-content",
    ".description__text",
)
LOGIN_PAGE_MARKERS = (
    "login-loading-card",
    "passwordless-email-login",
    "authwall",
)


def _is_linkedin_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    linkedin_host = hostname == "linkedin.com" or hostname.endswith(".linkedin.com")
    return (
        parsed.scheme == "https"
        and linkedin_host
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _plain_text(markup: str) -> str:
    soup = BeautifulSoup(markup, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _job_postings(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _job_postings(item)
        return
    if not isinstance(value, dict):
        return

    job_type = value.get("@type", [])
    if isinstance(job_type, str):
        job_type = [job_type]
    if "JobPosting" in job_type:
        yield value

    for key in ("@graph", "mainEntity"):
        if key in value:
            yield from _job_postings(value[key])


def _extract_json_ld(soup: BeautifulSoup) -> str:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for posting in _job_postings(data):
            description = posting.get("description")
            if isinstance(description, str):
                text = _plain_text(description)
                if len(text) >= MIN_DESCRIPTION_LENGTH:
                    return text
    return ""


def _extract_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    description = _extract_json_ld(soup)
    if description:
        return description[:MAX_DESCRIPTION_LENGTH]

    for selector in DESCRIPTION_SELECTORS:
        element = soup.select_one(selector)
        if element is None:
            continue
        description = _plain_text(str(element))
        if len(description) >= MIN_DESCRIPTION_LENGTH:
            return description[:MAX_DESCRIPTION_LENGTH]
    return ""


def _is_login_page(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in LOGIN_PAGE_MARKERS)


def _get_linkedin_page(
    client: httpx.Client, url: str
) -> tuple[httpx.Response | None, str | None]:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _is_linkedin_url(current_url):
            return None, "unsupported_url"
        response = client.get(current_url)
        if response.status_code not in REDIRECT_CODES:
            return response, None

        location = response.headers.get("location")
        if not location:
            return None, "redirect_without_location"
        current_url = urljoin(current_url, location)

    return None, "too_many_redirects"


def _fetch_description_from_url(
    client: httpx.Client, url: str
) -> tuple[str | None, str]:
    if not _is_linkedin_url(url):
        return None, "unsupported_url"
    response, error_status = _get_linkedin_page(client, url)
    if response is None:
        return None, error_status or "request_error"
    if response.status_code != 200:
        return None, f"http_{response.status_code}"
    if "html" not in response.headers.get("content-type", "").lower():
        return None, "not_html"

    description = _extract_description(response.text)
    if description:
        return description, "found"
    if _is_login_page(response.text):
        return None, "login_required"
    return None, "description_not_found"


def fetch_job_description(
    url: str, linkedin_job_id: str = "", timeout_seconds: float = 20
) -> tuple[str | None, str]:
    """Fetch a public job description without authentication or browser cookies."""
    urls = []
    if re.fullmatch(r"\d+", linkedin_job_id):
        urls.append(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{linkedin_job_id}"
        )
    urls.append(url)
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as client:
            last_status = "unsupported_url"
            for candidate_url in dict.fromkeys(urls):
                description, status = _fetch_description_from_url(client, candidate_url)
                if description:
                    return description, status
                last_status = status
                if status == "login_required":
                    continue
            return None, last_status
    except httpx.HTTPError:
        return None, "request_error"
