"""Google Meet URL helpers.

Vexa joins by ``native_meeting_id`` — the ``abc-defg-hij`` code in a Meet URL.
This module extracts/validates that code so we never pass a raw URL where an id
is expected (a real bug class).
"""
from __future__ import annotations

import re

# Google Meet codes are lowercase letters in a 3-4-3 grouping, e.g. "abc-defg-hij".
_MEET_CODE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b")


class InvalidMeetUrl(ValueError):
    """Raised when no valid Google Meet code can be extracted."""


def parse_native_meeting_id(value: str) -> str:
    """Extract the ``abc-defg-hij`` code from a Meet URL or bare code.

    Accepts full URLs (``https://meet.google.com/abc-defg-hij``), URLs with
    query/fragment, or the bare code itself. Raises ``InvalidMeetUrl`` otherwise.
    """
    if not value or not isinstance(value, str):
        raise InvalidMeetUrl(f"empty or non-string Meet value: {value!r}")
    match = _MEET_CODE.search(value.strip().lower())
    if not match:
        raise InvalidMeetUrl(f"no Google Meet code found in: {value!r}")
    return match.group(1)


def build_meet_url(native_meeting_id: str) -> str:
    """Canonical Meet URL for a code (validates the code in the process)."""
    code = parse_native_meeting_id(native_meeting_id)
    return f"https://meet.google.com/{code}"
