"""Unit tests for the insight-email recipient resolver."""
from types import SimpleNamespace

from app.services.orchestrator import resolve_recipients

BOT = "centralagentai@gmail.com"


def _m(organizer=None, attendees=None):
    return SimpleNamespace(organizer_email=organizer, attendees=attendees)


def test_organizer_only_mode_ignores_attendees():
    m = _m(organizer="priya@x.com", attendees=["a@x.com", "b@x.com"])
    assert resolve_recipients(m, mode="organizer", bot_email=BOT) == ["priya@x.com"]


def test_all_attendees_mode_includes_everyone_organizer_first():
    m = _m(organizer="priya@x.com", attendees=["a@x.com", "b@x.com"])
    assert resolve_recipients(m, mode="all_attendees", bot_email=BOT) == [
        "priya@x.com",
        "a@x.com",
        "b@x.com",
    ]


def test_all_attendees_excludes_bot_and_dedupes_case_insensitively():
    m = _m(
        organizer="Priya@X.com",
        attendees=["a@x.com", "PRIYA@x.com", BOT.upper(), "a@x.com"],
    )
    assert resolve_recipients(m, mode="all_attendees", bot_email=BOT) == [
        "Priya@X.com",
        "a@x.com",
    ]


def test_falls_back_to_organizer_when_no_attendees_stored():
    m = _m(organizer="priya@x.com", attendees=None)
    assert resolve_recipients(m, mode="all_attendees", bot_email=BOT) == ["priya@x.com"]


def test_empty_when_no_organizer_and_no_attendees():
    assert resolve_recipients(_m(), mode="all_attendees", bot_email=BOT) == []


# --------------------------------------------------------------------------- #
# No-reply filtering + fallback (the instant-meet recipient bug)              #
# --------------------------------------------------------------------------- #


def test_noreply_organizer_is_dropped():
    # An instant-meet row stores meetings-noreply@google.com as "organizer".
    # It must never be emailed.
    m = _m(organizer="meetings-noreply@google.com")
    assert resolve_recipients(m, mode="organizer", bot_email=BOT) == []


def test_noreply_organizer_falls_back_to_configured_address():
    m = _m(organizer="meetings-noreply@google.com")
    assert resolve_recipients(
        m, mode="organizer", bot_email=BOT, fallback="me@dualite.dev"
    ) == ["me@dualite.dev"]


def test_fallback_used_when_no_organizer():
    assert resolve_recipients(
        _m(), mode="organizer", bot_email=BOT, fallback="me@dualite.dev"
    ) == ["me@dualite.dev"]


def test_fallback_honoured_even_when_equal_to_bot_email():
    # User explicitly chose the bot's own inbox as the fallback; unlike the
    # organizer path (which excludes the bot), the fallback is honoured.
    assert resolve_recipients(_m(), mode="organizer", bot_email=BOT, fallback=BOT) == [BOT]


def test_real_organizer_takes_precedence_over_fallback():
    m = _m(organizer="priya@x.com")
    assert resolve_recipients(
        m, mode="organizer", bot_email=BOT, fallback="me@dualite.dev"
    ) == ["priya@x.com"]


def test_blank_fallback_yields_empty():
    assert resolve_recipients(_m(), mode="organizer", bot_email=BOT, fallback="") == []


def test_noreply_fallback_is_itself_rejected():
    # A misconfigured no-reply fallback must not resurrect the bug.
    assert (
        resolve_recipients(
            _m(), mode="organizer", bot_email=BOT, fallback="noreply@google.com"
        )
        == []
    )
