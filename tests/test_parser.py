from jsn.models import GmailMessage
from jsn.parser import parse_linkedin_alert


def test_parse_linkedin_job_card_fields_and_original_url() -> None:
    message = GmailMessage(
        message_id="message-1",
        internal_date="2026-10-17T00:00:00+00:00",
        subject="New jobs for you",
        body=(
            "<html><body><div>"
            '<a href="https://www.linkedin.com/jobs/view/123456/">'
            "Senior Backend Engineer</a>"
            '<a href="https://www.linkedin.com/company/example/">Example Co.</a>'
            "<span>Taipei, Taiwan</span>"
            "</div></body></html>"
        ),
    )

    jobs = parse_linkedin_alert(message)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Example Co."
    assert job.location == "Taipei, Taiwan"
    assert job.original_url == "https://www.linkedin.com/jobs/view/123456/"
