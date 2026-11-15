from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from .llm import create_llm_client


UNKNOWN = "未提供"
LOGGER = logging.getLogger(__name__)
VALID_LOCATION = {"confirmed_taiwan", "unknown", "outside_taiwan"}
VALID_SCHEDULE = {"normal", "unknown", "irregular"}
VALID_WORK_MODE = {"match", "unknown", "mismatch"}


def scoring_config_hash(config: dict[str, Any]) -> str:
    content = json.dumps(
        {"profile": config.get("profile", {}), "filters": config.get("filters", {})},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _response_content(content: str) -> str:
    if not isinstance(content, str):
        raise ValueError("LLM provider response content must be a string")

    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    return content.strip()


def _verified_field(
    result: dict[str, Any], key: str, source_text: str
) -> tuple[str, str]:
    value = str(result.get(key, "") or "").strip()
    evidence = str(result.get(f"{key}_evidence", "") or "").strip()
    if not value or value == UNKNOWN or not evidence:
        return UNKNOWN, ""
    if evidence.casefold() not in source_text.casefold():
        return UNKNOWN, ""
    return value, evidence


def _summary_field(
    result: dict[str, Any], key: str, source_text: str
) -> tuple[list[str] | str, str]:
    raw_items = result.get(key)
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    items = [
        item.strip()
        for item in raw_items
        if isinstance(item, str) and item.strip() and item.strip() != UNKNOWN
    ]
    value: list[str] | str = items if items else UNKNOWN

    evidence = str(result.get(f"{key}_evidence", "") or "").strip()
    if not evidence or evidence.casefold() not in source_text.casefold():
        evidence = ""
    return value, evidence


def build_job_instructions(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    source_text = str(job["source_text"])
    source_lines = source_text.splitlines()
    if source_lines and source_lines[0].startswith("Subject:"):
        source_text = "\n".join(source_lines[1:]).lstrip()
    description = str(job.get("description_text") or "").strip()
    if description:
        source_text = (
            f"{source_text}\n\n"
            "Job description fetched from the LinkedIn posting:\n"
            f"{description}"
        )
    return {
        "profile": config["profile"],
        "filters": config["filters"],
        "job": {
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "source_text": source_text,
            "original_url": job["original_url"],
        },
    }


def analyze_job(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    llm_config = config["llm"]
    instructions = build_job_instructions(job, config)
    source_text = instructions["job"]["source_text"]
    system_prompt = (
        "You evaluate LinkedIn job listings for a job seeker. Use the supplied job data and your "
        "geographic knowledge to interpret locations. Do not open links or invent locations absent "
        "from the data. Return one JSON object only. Set responsibilities and requirements to JSON "
        "arrays of concise Traditional Chinese strings, with one coherent duty or qualification per "
        "item. Let the source determine how many items to include; preserve all important details and "
        "omit only redundant or minor information. Include core duties, required experience and skills, "
        "and relevant standout qualifications. If source_text contains relevant information, do not "
        "return an empty array or '未提供'. Do not copy the full description. Provide a short verbatim "
        "quote in each responsibilities_evidence and requirements_evidence string. For benefits, schedule, "
        "and work_mode, include short verbatim evidence quotes copied from source_text; if the data does "
        "not state one of these facts, use an empty value and empty evidence. Determine location_status "
        "from job.location and source_text: use confirmed_taiwan for a specific location in Taiwan, "
        "outside_taiwan for a specific location outside Taiwan, and unknown if the location is missing "
        "or vague. When confirmed_taiwan, return the specific place in location_evidence. schedule_status "
        "must be normal, unknown, or irregular. work_mode_status must be match, unknown, or mismatch. "
        "Return score from 0 to 100, matched_skills as a list, and a concise rationale. Use double quotes "
        "for all JSON keys and string values; do not include comments, trailing commas, or Markdown."
    )
    user_prompt = json.dumps(instructions, ensure_ascii=False)
    for attempt in range(2):
        client = create_llm_client(llm_config)
        attempt_system_prompt = system_prompt
        if attempt:
            attempt_system_prompt += (
                " The previous response was not valid JSON. Re-evaluate the supplied job data "
                "and return a syntactically valid JSON object with every key and string in "
                "double quotes, without Markdown or trailing commas."
            )
        content = client.generate(
            system_prompt=attempt_system_prompt,
            user_prompt=user_prompt,
        )
        try:
            raw = json.loads(_response_content(content))
            break
        except json.JSONDecodeError as error:
            if attempt == 1:
                raise
            LOGGER.warning(
                "LLM response was invalid JSON at line %d, column %d; retrying once",
                error.lineno,
                error.colno,
            )

    location_status = raw.get("location_status", "unknown")
    schedule_status = raw.get("schedule_status", "unknown")
    work_mode_status = raw.get("work_mode_status", "unknown")
    if location_status not in VALID_LOCATION:
        location_status = "unknown"
    if schedule_status not in VALID_SCHEDULE:
        schedule_status = "unknown"
    if work_mode_status not in VALID_WORK_MODE:
        work_mode_status = "unknown"

    extracted: dict[str, Any] = {}
    evidence: dict[str, str] = {}
    for field in ("responsibilities", "requirements", "benefits", "schedule", "work_mode"):
        if field in {"responsibilities", "requirements"}:
            extracted[field], evidence[field] = _summary_field(raw, field, source_text)
        else:
            extracted[field], evidence[field] = _verified_field(raw, field, source_text)

    location_evidence = str(raw.get("location_evidence", "") or "").strip()
    if location_status == "confirmed_taiwan" and not location_evidence:
        location_status = "unknown"
    evidence["location"] = location_evidence

    if not evidence["schedule"]:
        schedule_status = "unknown"
    if not evidence["work_mode"]:
        work_mode_status = "unknown"

    try:
        score = max(0, min(100, int(float(raw.get("score", 0) or 0))))
    except (TypeError, ValueError):
        score = 0
    min_score = int(config["filters"].get("minimum_score", 60))
    rejected = score < min_score
    if config["filters"].get("require_taiwan_location", True):
        rejected = rejected or location_status == "outside_taiwan"
    if config["filters"].get("require_normal_schedule", True):
        rejected = rejected or schedule_status == "irregular"
    if config["filters"].get("work_mode_required", False):
        rejected = rejected or work_mode_status == "mismatch"

    unknown_required = (
        (config["filters"].get("require_taiwan_location", True)
         and location_status != "confirmed_taiwan")
        or (config["filters"].get("require_normal_schedule", True)
            and schedule_status != "normal")
        or (config["filters"].get("work_mode_required", False)
            and work_mode_status != "match")
    )
    decision = "reject" if rejected else "review" if unknown_required else "pass"

    configured_skills = config["profile"].get("skills", [])
    valid_skills = {str(skill).casefold(): str(skill) for skill in configured_skills}
    matched_skills = [
        valid_skills[item.casefold()]
        for item in raw.get("matched_skills", [])
        if isinstance(item, str) and item.casefold() in valid_skills
    ]

    return {
        "score": score,
        "decision": decision,
        "matched_skills": matched_skills,
        "location_status": location_status,
        "schedule_status": schedule_status,
        "work_mode_status": work_mode_status,
        "fields": extracted,
        "evidence": evidence,
        "rationale": str(raw.get("rationale", ""))[:500] or UNKNOWN,
    }
