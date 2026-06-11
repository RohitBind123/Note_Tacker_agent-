"""Rolling meeting-memory builder.

Extracts a structured memory — rolling summary + decisions / action items /
risks / open questions — from the live transcript using Gemini structured
output, and upserts it into the single ``MeetingMemory`` row for the meeting.

Two cost/quality guards:

  - Delta guard: the transcript only grows during a meeting, so we skip the
    (paid) model call when it has not grown by at least ``MIN_GROWTH_CHARS``
    since the last refresh. ``transcript_chars`` on the row records the coverage
    point of the last successful build.
  - Insufficient-content guard: a transcript below ``_MIN_CHARS`` is not sent to
    the model; we write an empty memory rather than invite hallucinated owners
    or decisions (mirrors the analyzer's policy).

The upsert is idempotent on ``meeting_id`` (one row per meeting,
``ON CONFLICT DO UPDATE``), so concurrent refresh passes converge instead of
duplicating.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.copilot_models import MeetingMemory
from app.logging_config import get_logger
from app.services.http import request_with_retries

log = get_logger(__name__)

_MIN_CHARS = 40
# Don't pay for a rebuild unless this many new transcript chars accumulated.
MIN_GROWTH_CHARS = 400
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["task"],
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "decisions", "action_items", "risks", "open_questions"],
}

_PROMPT = """You are maintaining a live, evolving memory of a meeting that is still \
in progress. From the transcript so far, extract the current state.

Rules:
- Use ONLY what is present in the transcript. Never invent facts, owners, dates, or numbers.
- If a category has nothing yet, return an empty list for it.
- "summary": 2-4 sentences on what has been discussed and where the meeting stands now.
- "decisions": concrete decisions that have actually been made (not proposals still open).
- "action_items": each has an optional "owner" (only if a person is clearly named) and a "task".
- "open_questions": questions raised but not yet resolved in the transcript.
- The transcript may be partial or mixed-language; write the memory in clear English.

Transcript so far:
\"\"\"
{transcript}
\"\"\"
"""


@dataclass(frozen=True)
class MeetingMemoryData:
    """Structured memory extracted from a transcript snapshot."""

    summary: str
    decisions: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    insufficient_content: bool = False


def _empty_memory() -> MeetingMemoryData:
    return MeetingMemoryData(
        summary="Not enough has been said yet to summarise.",
        insufficient_content=True,
    )


def should_rebuild(
    *, current_chars: int, covered_chars: int | None, force: bool
) -> bool:
    """Decide whether a memory rebuild is worth the (paid) model call.

    Rebuild when forced, when no memory exists yet, or when the transcript has
    grown by at least ``MIN_GROWTH_CHARS`` since the last covered point. Pure so
    the cost-guard policy is unit-testable without a DB.
    """
    if force or covered_chars is None:
        return True
    return current_chars - covered_chars >= MIN_GROWTH_CHARS


class MeetingMemoryBuilder:
    """Builds structured meeting memory via Gemini structured output."""

    def __init__(
        self, *, model: str | None = None, api_key: str | None = None, api_base: str | None = None
    ) -> None:
        self._model = model or settings.gemini_model
        self._key = api_key or settings.gemini_api_key
        self._base = (api_base or settings.gemini_api_base).rstrip("/")
        if not self._key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

    async def build(self, transcript_text: str) -> MeetingMemoryData:
        text = (transcript_text or "").strip()
        if len(text) < _MIN_CHARS:
            log.info("copilot_memory_too_short", chars=len(text))
            return _empty_memory()

        url = f"{self._base}/models/{self._model}:generateContent?key={self._key}"
        body = {
            "contents": [{"parts": [{"text": _PROMPT.format(transcript=text)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _MEMORY_SCHEMA,
                "temperature": 0.2,
            },
        }
        log.info("copilot_memory_request", model=self._model, transcript_chars=len(text))
        resp = await request_with_retries("POST", url, json=body, timeout=_TIMEOUT)
        if resp.status_code != 200:
            log.error("copilot_memory_failed", status=resp.status_code, body=resp.text[:300])
            raise RuntimeError(
                f"meeting memory build failed ({resp.status_code}): {resp.text[:200]}"
            )
        return _parse_memory(resp.json())


def _parse_memory(data: dict) -> MeetingMemoryData:
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw_text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        log.error("copilot_memory_parse_failed", error=str(exc))
        raise RuntimeError(f"could not parse meeting memory response: {exc}") from exc
    return MeetingMemoryData(
        summary=str(parsed.get("summary") or ""),
        decisions=list(parsed.get("decisions") or []),
        action_items=list(parsed.get("action_items") or []),
        risks=list(parsed.get("risks") or []),
        open_questions=list(parsed.get("open_questions") or []),
        insufficient_content=False,
    )


async def refresh_memory(
    db: AsyncSession,
    meeting_id: int,
    transcript_text: str,
    *,
    builder: MeetingMemoryBuilder | None = None,
    force: bool = False,
) -> MeetingMemory | None:
    """Rebuild + upsert a meeting's memory if the transcript grew enough.

    Returns the (refreshed or unchanged) row, or None if the transcript is too
    short to build anything yet. Idempotent: upserts the single
    ``meeting_id`` row. Skips the model call when growth since the last refresh
    is below ``MIN_GROWTH_CHARS`` unless ``force`` is set (e.g. final pass).
    """
    text = (transcript_text or "").strip()
    chars = len(text)

    existing = (
        await db.execute(select(MeetingMemory).where(MeetingMemory.meeting_id == meeting_id))
    ).scalar_one_or_none()

    covered = existing.transcript_chars if existing is not None else None
    if not should_rebuild(current_chars=chars, covered_chars=covered, force=force):
        log.info(
            "copilot_memory_skip_no_growth",
            meeting_id=meeting_id,
            chars=chars,
            covered=covered,
        )
        return existing

    if chars < _MIN_CHARS:
        return existing

    builder = builder or MeetingMemoryBuilder()
    memory = await builder.build(text)

    now = datetime.now(UTC)
    values = {
        "meeting_id": meeting_id,
        "summary": memory.summary,
        "decisions": memory.decisions,
        "action_items": memory.action_items,
        "risks": memory.risks,
        "open_questions": memory.open_questions,
        "transcript_chars": chars,
        "model_used": builder._model,  # noqa: SLF001 - record provenance
        "refreshed_at": now,
    }
    stmt = pg_insert(MeetingMemory).values(**values)
    update_cols = {k: v for k, v in values.items() if k != "meeting_id"}
    stmt = stmt.on_conflict_do_update(
        index_elements=["meeting_id"], set_=update_cols
    )
    await db.execute(stmt)
    await db.commit()
    log.info(
        "copilot_memory_refreshed",
        meeting_id=meeting_id,
        chars=chars,
        decisions=len(memory.decisions),
        action_items=len(memory.action_items),
        open_questions=len(memory.open_questions),
    )
    return (
        await db.execute(select(MeetingMemory).where(MeetingMemory.meeting_id == meeting_id))
    ).scalar_one_or_none()
