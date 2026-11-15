import json

from jsn import scoring


def test_invalid_json_is_retried_and_summaries_are_preserved(monkeypatch):
    responses = iter([
        "invalid JSON",
        json.dumps({
            "score": 80,
            "location_status": "confirmed_taiwan",
            "location_evidence": "Taipei",
            "schedule_status": "unknown",
            "work_mode_status": "unknown",
            "responsibilities": ["設計服務"],
            "requirements": ["具備 Python 經驗"],
            "responsibilities_evidence": "Design services",
            "requirements_evidence": "Python experience",
        }),
    ])
    calls = []
    class Client:
        def generate(self, **kwargs):
            calls.append(kwargs)
            return next(responses)
    monkeypatch.setattr(scoring, "create_llm_client", lambda config: Client())
    job = {
        "title": "Engineer",
        "company": "Example",
        "location": "Taipei",
        "source_text": "Design services. Python experience.",
        "original_url": "https://www.linkedin.com/jobs/view/123/",
    }
    config = {"llm": {}, "profile": {"skills": []}, "filters": {}}

    result = scoring.analyze_job(job, config)

    assert len(calls) == 2
    assert "previous response was not valid JSON" in calls[1]["system_prompt"]
    assert result["fields"]["responsibilities"] == ["設計服務"]
    assert result["fields"]["requirements"] == ["具備 Python 經驗"]
    assert result["location_status"] == "confirmed_taiwan"
