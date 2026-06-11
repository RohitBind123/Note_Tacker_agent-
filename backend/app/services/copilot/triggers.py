"""Mention trigger parsing (pure).

A chat message is routed to the copilot only when it contains a configured
trigger (default ``@centralagent``, case-insensitive). This module decides that,
and strips the handle to recover the actual question, with no I/O so the routing
logic is fully unit-testable.

The handle can appear anywhere ("hey @centralagent recap?" or "recap @CentralAgent")
and we tolerate the punctuation people type around mentions (``@centralagent:``,
``@centralagent,``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Punctuation commonly trailing/leading a stripped handle that isn't part of the
# question itself.
_EDGE_PUNCT = " \t\r\n:,>-—–"


@dataclass(frozen=True)
class MentionParse:
    """Result of inspecting a chat message for a copilot trigger."""

    is_mention: bool
    question: str
    trigger: str | None


def _matched_trigger(text_lower: str, triggers: list[str]) -> str | None:
    for trigger in triggers:
        if trigger and trigger in text_lower:
            return trigger
    return None


def parse_mention(text: str, triggers: list[str]) -> MentionParse:
    """Detect a trigger in ``text`` and extract the remaining question.

    ``triggers`` must already be normalised (lowercased, non-empty) — i.e. the
    value of ``settings.copilot_triggers``. Returns ``is_mention=False`` when no
    trigger is present; an empty ``question`` (with ``is_mention=True``) when the
    message is only the handle with nothing to ask.
    """
    raw = text or ""
    lowered = raw.lower()
    trigger = _matched_trigger(lowered, triggers)
    if trigger is None:
        return MentionParse(is_mention=False, question=raw.strip(), trigger=None)

    # Remove every case-insensitive occurrence of each trigger, then tidy up the
    # whitespace/punctuation people leave around a handle.
    cleaned = raw
    for t in triggers:
        if not t:
            continue
        cleaned = re.sub(re.escape(t), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(_EDGE_PUNCT).strip()
    return MentionParse(is_mention=True, question=cleaned, trigger=trigger)
