from app.db.models import Meeting, MeetingReport
from app.services import email_template


def _meeting() -> Meeting:
    return Meeting(
        id=1, platform="google_meet", native_meeting_id="abc-defg-hij",
        meet_url="https://meet.google.com/abc-defg-hij", title="Weekly Sync",
    )


def test_subject():
    assert email_template.build_subject(_meeting()) == "Meeting Insights — Weekly Sync"


def test_empty_sections_render_none_noted():
    report = MeetingReport(meeting_id=1, summary="A short summary.",
                           decisions=[], action_items=[], risks=[], next_steps=[])
    html = email_template.build_html(_meeting(), report)
    assert "A short summary." in html
    assert html.count("None noted.") == 4  # decisions, action items, risks, next steps


def test_action_items_with_owner_and_escaping():
    report = MeetingReport(
        meeting_id=1,
        summary="<script>alert(1)</script>",
        decisions=["Ship Friday"],
        action_items=[{"owner": "John", "task": "Deploy"}, {"task": "QA"}],
        risks=[], next_steps=[],
    )
    html = email_template.build_html(_meeting(), report)
    assert "<strong>John</strong> — Deploy" in html
    assert "<li style=\"margin:4px 0;\">QA</li>" in html
    # summary is HTML-escaped (no raw script tag)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
