from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx


MAX_MESSAGE_LENGTH = 1800
SUPPRESS_EMBEDS_FLAG = 4
UNKNOWN = "未提供"
LOCATION_LABELS = {
    "confirmed_taiwan": "確認在台灣",
    "unknown": "待確認",
    "outside_taiwan": "不在台灣",
}
SCHEDULE_LABELS = {"normal": "一般工時", "unknown": "待確認", "irregular": "非一般工時"}
WORK_MODE_LABELS = {"match": "符合偏好", "unknown": "待確認", "mismatch": "不符合偏好"}


def _safe(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or UNKNOWN)).strip()
    text = text.replace("@everyone", "@\u200beveryone")
    text = text.replace("@here", "@\u200bhere")
    return text[:limit] or UNKNOWN


def _format_summary(value: Any) -> str:
    if not isinstance(value, list):
        return _safe(value, MAX_MESSAGE_LENGTH - 50)

    items = [
        _safe(item, MAX_MESSAGE_LENGTH - 50)
        for item in value
        if isinstance(item, str) and item.strip() and item.strip() != UNKNOWN
    ]
    if not items:
        return UNKNOWN
    return "\n" + "\n".join(f"  - {item}" for item in items)


def _job_block(job: dict[str, Any]) -> str:
    analysis = job["analysis"]
    fields = analysis["fields"]
    decision = "符合" if analysis["decision"] == "pass" else "待確認"
    raw_location_status = analysis["location_status"]
    location_label = LOCATION_LABELS.get(raw_location_status, "待確認")
    location_annotation = (
        "" if raw_location_status == "confirmed_taiwan" else f"（{location_label}）"
    )
    schedule_status = SCHEDULE_LABELS.get(analysis["schedule_status"], "待確認")
    work_mode_status = WORK_MODE_LABELS.get(analysis["work_mode_status"], "待確認")
    skills = "、".join(analysis["matched_skills"]) or UNKNOWN
    original_url = quote(
        str(job["original_url"]).strip(), safe=":/?&=%#[]@!$'*,;+"
    )
    return "\n".join(
        (
            f"## {_safe(job['title'], 160)}｜{_safe(job['company'], 120)}",
            f"- 評估：{decision}｜匹配分數：{analysis['score']}/100",
            f"- 地點：{_safe(job['location'], 200)}{location_annotation}",
            f"- 工作時間：{_safe(fields.get('schedule'))}（{schedule_status}）",
            f"- WFH／工作型態：{_safe(fields.get('work_mode'))}（{work_mode_status}）",
            f"- 符合技能：{skills}",
            f"- 工作內容：{_format_summary(fields.get('responsibilities'))}",
            f"- 工作要求：{_format_summary(fields.get('requirements'))}",
            f"- 福利：{_safe(fields.get('benefits'))}",
            f"- 判斷摘要：{_safe(analysis['rationale'], 300)}",
            f"- [原始職缺]({original_url})",
        )
    )


def _chunks(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        if len(line) > limit:
            raise ValueError(
                "A report line exceeds Discord's message limit; refusing to truncate it"
            )
        if len(current) + len(line) + 1 <= limit:
            current = f"{current}\n{line}" if current else line
            continue
        if current:
            chunks.append(current)
        current = line

    if current:
        chunks.append(current)
    return chunks or ["本週沒有新的 LinkedIn 職缺快訊。"]


def _post_forum_thread(
    webhook: str,
    thread_name: str,
    content: str,
    replies: list[str] | None = None,
    suppress_embeds: bool = False,
) -> str:
    chunks = _chunks(content)
    reply_chunks = [*chunks[1:], *(replies or [])]
    if any(len(reply) > MAX_MESSAGE_LENGTH for reply in reply_chunks):
        raise ValueError("A Forum reply exceeds Discord's message limit")
    with httpx.Client(timeout=30) as client:
        first_payload: dict[str, Any] = {
            "thread_name": thread_name[:100],
            "content": chunks[0],
            "allowed_mentions": {"parse": []},
        }
        if suppress_embeds:
            first_payload["flags"] = SUPPRESS_EMBEDS_FLAG
        first = client.post(
            webhook,
            params={"wait": "true"},
            json=first_payload,
        )
        _check_discord_response(first)
        response_data = first.json()
        thread_id = str(response_data.get("channel_id", ""))
        if not thread_id:
            raise ValueError("Discord did not return the Forum thread ID")

        for chunk in reply_chunks:
            reply_payload: dict[str, Any] = {
                "content": chunk,
                "allowed_mentions": {"parse": []},
            }
            if suppress_embeds:
                reply_payload["flags"] = SUPPRESS_EMBEDS_FLAG
            response = client.post(
                webhook,
                params={"wait": "true", "thread_id": thread_id},
                json=reply_payload,
            )
            _check_discord_response(response)

    return thread_id


def _get_webhook(webhook_env: str) -> str:
    webhook = os.getenv(webhook_env, "").strip()
    if not webhook:
        raise RuntimeError(f"Set the {webhook_env} environment variable")
    return webhook


def _check_discord_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = json.dumps(response.json(), ensure_ascii=False)
    except ValueError:
        detail = response.text
    detail = detail[:1200] or "No error details returned"
    raise RuntimeError(
        f"Discord webhook returned HTTP {response.status_code}: {detail}"
    )


def post_weekly_report(
    report: list[dict[str, Any]],
    week_title: str,
    webhook_env: str,
) -> str:
    webhook = _get_webhook(webhook_env)

    pass_count = sum(item["analysis"]["decision"] == "pass" for item in report)
    review_count = len(report) - pass_count
    header = (
        f"# {week_title}\n"
        f"本週收錄 {len(report)} 筆：符合 {pass_count}，待確認 {review_count}。"
    )
    if report:
        content = header
        replies: list[str] = []
        for item in report:
            job_block = _job_block(item)
            job_chunks = _chunks(job_block)
            if len(job_chunks) > 1:
                continuation_label = (
                    f"【續文｜{_safe(item['title'], 60)}｜{_safe(item['company'], 60)}】"
                )
                continuation_reserve = len(continuation_label) + 12
                job_chunks = _chunks(
                    job_block, MAX_MESSAGE_LENGTH - continuation_reserve
                )
                continuation_count = len(job_chunks) - 1
                for part_number in range(1, len(job_chunks)):
                    job_chunks[part_number] = (
                        f"{continuation_label} "
                        f"{part_number}/{continuation_count}\n"
                        f"{job_chunks[part_number]}"
                    )
            replies.extend(job_chunks)
    else:
        content = f"{header}\n\n本週沒有符合或待確認的職缺。"
        replies = []
    return _post_forum_thread(
        webhook, week_title, content, replies, suppress_embeds=True
    )


def post_discord_test(webhook_env: str = "DISCORD_FORUM_WEBHOOK_URL") -> str:
    webhook = _get_webhook(webhook_env)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    thread_name = f"JSN Discord Webhook 測試 {timestamp}"
    content = (
        "# JSN Discord Webhook 測試\n"
        "這是一則獨立連線測試。若你看到這則貼文，代表 JSN 能透過此 Webhook "
        "在 Forum 頻道建立討論串。這則訊息不包含職缺資料。"
    )
    return _post_forum_thread(webhook, thread_name, content)
