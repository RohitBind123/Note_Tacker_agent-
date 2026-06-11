"""Unit tests for the rolling meeting-memory builder (Batch 4).

The DB upsert path is integration-tested against real Neon in the E2E batch;
here we pin the model wire shape (prompt build + structured-JSON parse), the
insufficient-content guard, and the delta-growth guard logic.
"""
import json

import httpx
import respx

from app.services.copilot.memory import (
    MIN_GROWTH_CHARS,
    MeetingMemoryBuilder,
    MeetingMemoryData,
    _parse_memory,
    should_rebuild,
)

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _builder() -> MeetingMemoryBuilder:
    return MeetingMemoryBuilder(model="gemini-2.5-flash", api_key="test-key", api_base=BASE)


async def test_short_transcript_returns_empty_without_calling_model():
    # No respx route registered -> a model call would raise.
    memory = await _builder().build("hi")
    assert memory.insufficient_content is True
    assert memory.decisions == [] and memory.action_items == []
    assert memory.summary


@respx.mock
async def test_build_parses_structured_memory():
    model_json = {
        "summary": "The team is mid-discussion on the Q3 launch plan.",
        "decisions": ["Launch date moved to July 15"],
        "action_items": [{"owner": "Priya", "task": "Draft the rollout email"}, {"task": "Book QA"}],
        "risks": ["QA window is tight"],
        "open_questions": ["Who signs off on pricing?"],
    }
    gemini_response = {"candidates": [{"content": {"parts": [{"text": json.dumps(model_json)}]}}]}
    route = respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json=gemini_response)
    )
    memory = await _builder().build(
        "Priya: lets move launch to July 15. Sam: QA window is tight. " * 3
    )
    assert memory.insufficient_content is False
    assert memory.summary.startswith("The team is mid-discussion")
    assert memory.decisions == ["Launch date moved to July 15"]
    assert len(memory.action_items) == 2
    assert memory.open_questions == ["Who signs off on pricing?"]
    # prompt carried the transcript + asked for open_questions
    sent = route.calls.last.request.content
    assert b"open_questions" in sent
    assert b"Transcript so far" in sent


def test_parse_memory_tolerates_missing_optional_lists():
    # Model returns only summary -> lists default to empty, not None.
    data = {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "ok"})}]}}]}
    memory = _parse_memory(data)
    assert isinstance(memory, MeetingMemoryData)
    assert memory.summary == "ok"
    assert memory.decisions == [] and memory.risks == [] and memory.open_questions == []


def test_min_growth_threshold_is_sane():
    # Guard constant must be a positive, meaningful number of characters.
    assert MIN_GROWTH_CHARS >= 100


def test_should_rebuild_when_no_memory_exists_yet():
    assert should_rebuild(current_chars=50, covered_chars=None, force=False) is True


def test_should_rebuild_when_force_even_without_growth():
    # Final pass: rebuild regardless of how little grew.
    assert should_rebuild(current_chars=1000, covered_chars=999, force=True) is True


def test_should_skip_rebuild_below_growth_threshold():
    assert (
        should_rebuild(
            current_chars=1000, covered_chars=1000 - (MIN_GROWTH_CHARS - 1), force=False
        )
        is False
    )


def test_should_rebuild_at_growth_threshold():
    assert (
        should_rebuild(
            current_chars=1000, covered_chars=1000 - MIN_GROWTH_CHARS, force=False
        )
        is True
    )
