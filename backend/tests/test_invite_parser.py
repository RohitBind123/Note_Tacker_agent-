"""Unit tests for the Gmail invite parser (pure — no I/O)."""
from datetime import timezone

import pytest

from app.services.gmail.invite_parser import parse


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _parse(*, subject="", from_addr="", body=""):
    return parse(subject=subject, from_addr=from_addr, body_text=body)


# --------------------------------------------------------------------------- #
# Meet link extraction                                                         #
# --------------------------------------------------------------------------- #

class TestMeetLinkExtraction:
    def test_full_url_in_body(self):
        result = _parse(body="Join here: https://meet.google.com/abc-defg-hij")
        assert result is not None
        assert result.native_meeting_id == "abc-defg-hij"
        assert result.meet_url == "https://meet.google.com/abc-defg-hij"

    def test_url_with_query_string(self):
        result = _parse(body="https://meet.google.com/xyz-uvwx-yza?authuser=0&hs=1")
        assert result is not None
        assert result.native_meeting_id == "xyz-uvwx-yza"

    def test_bare_code_in_body(self):
        result = _parse(body="Your meeting code is pqr-stuv-wxy")
        assert result is not None
        assert result.native_meeting_id == "pqr-stuv-wxy"

    def test_no_meet_link_returns_none(self):
        result = _parse(subject="Hi there", body="Come to our office at 5pm")
        assert result is None

    def test_non_meet_google_url_returns_none(self):
        result = _parse(body="https://docs.google.com/document/d/1234")
        assert result is None


# --------------------------------------------------------------------------- #
# Title extraction from subject                                                #
# --------------------------------------------------------------------------- #

class TestTitleExtraction:
    def test_strips_invitation_prefix(self):
        r = _parse(
            subject="Invitation: Project Kickoff @ Mon Jun 10, 2026",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.title == "Project Kickoff"

    def test_strips_updated_invitation_prefix(self):
        r = _parse(
            subject="Updated invitation: Q3 Planning @ Thu Jun 12, 2026 2pm",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.title == "Q3 Planning"

    def test_strips_notification_prefix(self):
        r = _parse(
            subject="Notification: Team Standup @ Daily",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.title == "Team Standup"

    def test_plain_subject_kept_as_is(self):
        r = _parse(
            subject="You've been invited to a video call",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.title == "You've been invited to a video call"

    def test_empty_subject_gives_none_title(self):
        r = _parse(subject="", body="https://meet.google.com/abc-defg-hij")
        assert r.title is None


# --------------------------------------------------------------------------- #
# Organizer email from From header                                             #
# --------------------------------------------------------------------------- #

class TestOrganizerExtraction:
    def test_extracts_from_display_name_format(self):
        r = _parse(
            from_addr="Alice Smith <alice@example.com>",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.organizer_email == "alice@example.com"

    def test_calendar_notification_sender_is_not_an_organizer(self):
        # calendar-notification@google.com is Google's automated sender, not a
        # human. The real organizer is resolved by the calendar poller via the
        # Calendar API, so the parser must not store this address.
        r = _parse(
            from_addr="calendar-notification@google.com",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.organizer_email is None

    def test_meetings_noreply_sender_is_not_an_organizer(self):
        # The exact bug: a meet.google.com "Add people" invite is From
        # meetings-noreply@google.com. Storing it as organizer mailed the insight
        # into Google's no-reply void. It must resolve to None.
        r = _parse(
            from_addr="meetings-noreply@google.com",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.organizer_email is None

    def test_generic_noreply_sender_is_dropped(self):
        r = _parse(
            from_addr="No Reply <noreply@example.com>",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.organizer_email is None

    def test_real_human_sender_is_kept(self):
        r = _parse(
            from_addr="Priya <priya@acme.com>",
            body="https://meet.google.com/abc-defg-hij",
        )
        assert r.organizer_email == "priya@acme.com"

    def test_empty_from_gives_none(self):
        r = _parse(from_addr="", body="https://meet.google.com/abc-defg-hij")
        assert r.organizer_email is None


# --------------------------------------------------------------------------- #
# Scheduled time extraction                                                    #
# --------------------------------------------------------------------------- #

class TestStartTimeExtraction:
    def test_iso_timestamp_with_z(self):
        r = _parse(body="When: 2026-06-12T10:00:00Z https://meet.google.com/abc-defg-hij")
        assert r.start_time is not None
        assert r.start_time.year == 2026
        assert r.start_time.tzinfo == timezone.utc

    def test_iso_timestamp_with_offset(self):
        r = _parse(body="Start: 2026-06-12T15:30:00+05:30 https://meet.google.com/abc-defg-hij")
        assert r.start_time is not None
        # Should be converted to UTC (10:00 UTC)
        assert r.start_time.hour == 10

    def test_no_timestamp_returns_none_start(self):
        r = _parse(body="Join us now https://meet.google.com/abc-defg-hij")
        assert r.start_time is None

    def test_end_time_always_none(self):
        r = _parse(body="2026-06-12T10:00:00Z https://meet.google.com/abc-defg-hij")
        assert r.end_time is None


# --------------------------------------------------------------------------- #
# Real-shaped Google invite bodies                                             #
# --------------------------------------------------------------------------- #

class TestRealShapedBodies:
    _CALENDAR_INVITE_BODY = """
    You have been invited to the following event.

    Title: Sprint Review
    When: 2026-06-14T09:00:00Z
    Joining info: https://meet.google.com/mno-pqrs-tuv

    View your event at https://calendar.google.com/calendar/event?eid=xxx
    """

    _INSTANT_MEET_BODY = """
    You've been invited to a video call.

    To join the video call, click this link:
    https://meet.google.com/abc-defg-hij

    Otherwise, to join by phone, dial +1 555-000-0000 and enter this PIN: 123 456 789#
    """

    _HTML_FALLBACK_BODY = """
    <html><body>
    <a href="https://meet.google.com/xyz-uvwx-yza">Join Google Meet</a>
    </body></html>
    """

    def test_calendar_invite_body(self):
        r = _parse(
            subject="Invitation: Sprint Review @ Sat Jun 14, 2026",
            from_addr="calendar-notification@google.com",
            body=self._CALENDAR_INVITE_BODY,
        )
        assert r is not None
        assert r.native_meeting_id == "mno-pqrs-tuv"
        assert r.title == "Sprint Review"
        assert r.start_time is not None

    def test_instant_meet_body(self):
        r = _parse(
            subject="You've been invited to a video call",
            from_addr="noreply@google.com",
            body=self._INSTANT_MEET_BODY,
        )
        assert r is not None
        assert r.native_meeting_id == "abc-defg-hij"
        assert r.start_time is None  # no scheduled time in instant-meet invite

    def test_html_body_fallback(self):
        r = _parse(body=self._HTML_FALLBACK_BODY)
        assert r is not None
        assert r.native_meeting_id == "xyz-uvwx-yza"
