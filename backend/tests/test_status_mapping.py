"""Guard the Vexa-status -> domain-status mapping.

Regression test for the bug where Vexa's "completed" (recording finished) was
mapped to our terminal COMPLETED, skipping the transcript -> Gemini -> email
pipeline. Every "meeting ended" Vexa status must route through PROCESSING; our
COMPLETED is owned solely by send_report_email.
"""
from app.db.models import MeetingStatus
from app.services.orchestrator import _VEXA_TO_STATUS


def test_no_vexa_status_maps_to_completed():
    # COMPLETED means "insights emailed" — it must never come from a provider poll.
    assert MeetingStatus.COMPLETED not in _VEXA_TO_STATUS.values()


def test_ended_statuses_route_to_processing():
    for ended in ("completed", "stopped", "processing"):
        assert _VEXA_TO_STATUS[ended] is MeetingStatus.PROCESSING


def test_in_flight_statuses():
    assert _VEXA_TO_STATUS["active"] is MeetingStatus.ACTIVE
    for joining in ("requested", "joining", "awaiting_admission"):
        assert _VEXA_TO_STATUS[joining] is MeetingStatus.JOINING
