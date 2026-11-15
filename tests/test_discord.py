from jsn.discord import MAX_MESSAGE_LENGTH, _chunks, _job_block


def test_job_block_marks_missing_details_and_includes_original_link() -> None:
    job = {
        "title": "Backend Engineer",
        "company": "Example Co.",
        "location": "Taipei, Taiwan",
        "original_url": "https://www.linkedin.com/jobs/view/123456/",
        "analysis": {
            "decision": "review",
            "score": 75,
            "location_status": "confirmed_taiwan",
            "schedule_status": "unknown",
            "work_mode_status": "unknown",
            "matched_skills": [],
            "fields": {
                "schedule": "未提供",
                "work_mode": "未提供",
                "responsibilities": "未提供",
                "requirements": "未提供",
                "benefits": "未提供",
            },
            "rationale": "快訊沒有提供完整職務資訊",
        },
    }

    block = _job_block(job)

    assert "未提供" in block
    assert "[原始職缺](https://www.linkedin.com/jobs/view/123456/)" in block


def test_chunks_preserve_report_content_within_discord_limit() -> None:
    text = "\n".join(["第一則內容"] + ["職缺摘要。" * 100] * 4)

    chunks = _chunks(text)

    assert "\n".join(chunks) == text
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
