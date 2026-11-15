from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .llm import create_llm_client


UNKNOWN = "未提供"
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
    if not value or not evidence or evidence.casefold() not in source_text.casefold():
        return UNKNOWN, ""
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
        "You evaluate LinkedIn job listings for a job seeker. Use only the supplied job data, "
        "which may include an alert email and a fetched job description. "
        "Do not open links or invent missing facts. Return one JSON object only. For each extracted "
        "field, include a short verbatim evidence quote copied from source_text. If the supplied data does "
        "not state a fact, use an empty value and empty evidence. location_status must be "
        "confirmed_taiwan, unknown, or outside_taiwan. schedule_status must be normal, unknown, "
        "or irregular. work_mode_status must be match, unknown, or mismatch. Return score from 0 "
        "to 100, matched_skills as a list, and a concise rationale. Required extracted fields are "
        "responsibilities, requirements, benefits, schedule, and work_mode."
    )
    client = create_llm_client(llm_config)
    content = client.generate(
        system_prompt=system_prompt,
        user_prompt=json.dumps(instructions, ensure_ascii=False),
    )
    raw = json.loads(_response_content(content))

    location_status = raw.get("location_status", "unknown")
    schedule_status = raw.get("schedule_status", "unknown")
    work_mode_status = raw.get("work_mode_status", "unknown")
    if location_status not in VALID_LOCATION:
        location_status = "unknown"
    if schedule_status not in VALID_SCHEDULE:
        schedule_status = "unknown"
    if work_mode_status not in VALID_WORK_MODE:
        work_mode_status = "unknown"

    extracted: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for field in ("responsibilities", "requirements", "benefits", "schedule", "work_mode"):
        extracted[field], evidence[field] = _verified_field(raw, field, source_text)

    location_evidence = str(raw.get("location_evidence", "") or "").strip()
    if not location_evidence or location_evidence.casefold() not in source_text.casefold():
        location_status = "unknown"
        location_evidence = ""
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
