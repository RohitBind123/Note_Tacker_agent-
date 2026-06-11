"""Parse a Gmail message into a structured Meet invite.

Pure module — no network, no DB, no side effects. All I/O lives in reader.py
and gmail_scanner.py so this is 100% unit-testable with plain strings.

Google sends several shapes of email when you're invited to a Meet:
  1. A Google Calendar invite  ("Invitation: <Title> @ <time>")
  2. An "Add people" notification from inside an active Meet
     ("You've been invited to a video call" / "You have a new meeting")
  3. An updated-invitation email  ("Updated invitation: ...")

We extract from all three shapes. When in doubt, return None rather than
create a junk meeting row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr

from app.services.meet_url import InvalidMeetUrl, parse_native_meeting_id

# --- Regex patterns -----------------------------------------------------------

# Google Meet URLs with the code embedded.
_MEET_URL_RE = re.compile(
    r"https://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})\b[^\s\"<]*",
    re.IGNORECASE,
)

# Subject prefixes Google adds to invite emails.
_SUBJECT_STRIP_RE = re.compile(
    r"^(?:Updated\s+)?(?:Invitation|Notification|Forwarded\s+invitation)\s*:\s*",
    re.IGNORECASE,
)
# Trailing "@ <date/time>" suffix in subjects like "Kickoff @ Mon Jun 10, 2026"
_SUBJECT_TIME_SUFFIX_RE = re.compile(r"\s+@\s+.+$")

# ISO-8601 timestamps (with or without T-separator, optional fractional seconds, with Z or offset).
_ISO_DT_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\b"
)

# Google's automated senders for Meet/Calendar notifications. These are NEVER a
# real person we can deliver an insight email to: a meet.google.com "Add people"
# invite arrives From meetings-noreply@google.com, and calendar notices come From
# calendar-notification@google.com. The real organizer of a calendar meeting is
# resolved by the calendar poller via the Calendar API; for instant-meet invites
# there is no human address in the headers at all, so we store None and let the
# recipient resolver apply its configured fallback.
_NONHUMAN_SENDERS = frozenset(
    {
        "meetings-noreply@google.com",
        "calendar-notification@google.com",
    }
)


def _human_organizer(from_addr: str) -> str | None:
    """Return the sender address only if it could be a real, emailable human.

    Drops empty headers and Google's automated no-reply / notification senders.
    """
    _, addr = parseaddr(from_addr)
    addr = (addr or "").strip()
    if not addr:
        return None
    low = addr.lower()
    if low in _NONHUMAN_SENDERS or "noreply" in low or "no-reply" in low:
        return None
    return addr


@dataclass(frozen=True)
class ParsedInvite:
    meet_url: str
    native_meeting_id: str
    title: str | None
    organizer_email: str | None
    start_time: datetime | None
    end_time: datetime | None


def parse(
    *,
    subject: str,
    from_addr: str,
    body_text: str,
) -> ParsedInvite | None:
    """Return a ParsedInvite from a decoded Gmail message, or None if not actionable.

    Args:
        subject:   The email's Subject header value.
        from_addr: The email's From header value (raw, e.g. "Alice <alice@x.com>").
        body_text: The decoded plaintext body (or HTML stripped to text — caller's
                   responsibility to prefer text/plain).

    Returns None when:
    - No Google Meet code can be extracted from the body.
    - The extracted code would produce an invalid native_meeting_id.
    """
    # 1. Find the Meet link (mandatory — no link → not actionable).
    meet_url, native_id = _extract_meet(body_text)
    if meet_url is None or native_id is None:
        return None

    # 2. Title: strip known Google prefixes and trailing time suffix from subject.
    title = _extract_title(subject)

    # 3. Organizer: the From header, but ONLY if it is a real human. Google's
    #    notification senders (meetings-noreply@, calendar-notification@) are not
    #    deliverable, so we store None and let the recipient resolver fall back.
    organizer_email = _human_organizer(from_addr)

    # 4. Scheduled time: optional, parse only unambiguous ISO timestamps.
    start_time = _extract_start_time(body_text)

    return ParsedInvite(
        meet_url=meet_url,
        native_meeting_id=native_id,
        title=title,
        organizer_email=organizer_email,
        start_time=start_time,
        end_time=None,  # end time is rarely present in invite emails
    )


# --- Private helpers ----------------------------------------------------------


def _extract_meet(body: str) -> tuple[str | None, str | None]:
    """Return (full_url, native_id) or (None, None) if no Meet code found."""
    m = _MEET_URL_RE.search(body)
    if m:
        raw_url = m.group(0).rstrip(".,;)")
        try:
            native_id = parse_native_meeting_id(raw_url)
            return f"https://meet.google.com/{native_id}", native_id
        except InvalidMeetUrl:
            pass
    # Fallback: bare code anywhere in the body (e.g. "Meeting code: abc-defg-hij")
    try:
        native_id = parse_native_meeting_id(body)
        return f"https://meet.google.com/{native_id}", native_id
    except InvalidMeetUrl:
        return None, None


def _extract_title(subject: str) -> str | None:
    if not subject:
        return None
    cleaned = _SUBJECT_STRIP_RE.sub("", subject).strip()
    cleaned = _SUBJECT_TIME_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned if cleaned else None


def _extract_start_time(body: str) -> datetime | None:
    """Parse the first unambiguous ISO timestamp from the body."""
    m = _ISO_DT_RE.search(body)
    if not m:
        return None
    raw = m.group(1).replace(" ", "T")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        # Ensure tz-aware; if no tzinfo somehow, treat as UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None
