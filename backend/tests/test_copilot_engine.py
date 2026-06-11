"""Unit tests for the copilot Q&A engine (Batch 5).

Pins the pure context-block rendering and the single Gemini answer call (mocked
via respx). The full router DB round-trip is integration-tested in the E2E batch.
"""
import httpx
import respx

from app.services.copilot.engine import (
    MAX_ANSWER_CHARS,
    CopilotContext,
    CopilotEngine,
    build_context_block,
)

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _engine() -> CopilotEngine:
    return CopilotEngine(
        model="gemini-2.5-flash", api_key="test-key", api_base=BASE, bot_name="CentralAgent"
    )


# --- pure context rendering ---


def test_empty_context_renders_explicit_placeholder():
    block = build_context_block(CopilotContext())
    assert "No meeting content has been captured yet" in block


def test_context_block_includes_all_sections():
    ctx = CopilotContext(
        meeting_title="Q3 Launch Sync",
        memory_summary="Discussing the launch plan.",
        decisions=["Launch July 15"],
        action_items=[{"owner": "Priya", "task": "Draft email"}, {"task": "Book QA"}],
        open_questions=["Who signs off on pricing?"],
        transcript_snippets=["Priya: lets move to July 15"],
        recent_chat=["Sam: sounds good"],
    )
    block = build_context_block(ctx)
    assert "Q3 Launch Sync" in block
    assert "Discussing the launch plan." in block
    assert "- Launch July 15" in block
    assert "- Priya: Draft email" in block  # owner attributed
    assert "- Book QA" in block  # no owner -> task only
    assert "Who signs off on pricing?" in block
    assert "Priya: lets move to July 15" in block
    assert "Sam: sounds good" in block


def test_action_item_without_owner_renders_task_only():
    block = build_context_block(CopilotContext(action_items=[{"task": "Ship it"}]))
    assert "- Ship it" in block
    assert ":" not in block.split("Action items:")[1].split("- Ship it")[0].strip()


# --- Gemini answer call ---


@respx.mock
async def test_answer_returns_grounded_text():
    gemini_response = {
        "candidates": [{"content": {"parts": [{"text": "We decided to launch on July 15."}]}}]
    }
    route = respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json=gemini_response)
    )
    ctx = CopilotContext(decisions=["Launch July 15"])
    answer = await _engine().answer("what did we decide?", ctx)
    assert answer == "We decided to launch on July 15."
    sent = route.calls.last.request.content
    assert b"Launch July 15" in sent  # context was grounded into the prompt
    assert b"CentralAgent" in sent  # persona present


@respx.mock
async def test_answer_is_length_capped():
    long_text = "x" * (MAX_ANSWER_CHARS + 500)
    respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": long_text}]}}]}
        )
    )
    answer = await _engine().answer("ramble", CopilotContext())
    assert len(answer) <= MAX_ANSWER_CHARS


@respx.mock
async def test_answer_empty_model_text_raises():
    respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
    )
    import pytest

    with pytest.raises(RuntimeError, match="empty text"):
        await _engine().answer("q", CopilotContext())
