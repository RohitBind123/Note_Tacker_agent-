"""Unit tests for the mention trigger parser (Batch 5) — pure routing logic."""
import pytest

from app.services.copilot.triggers import parse_mention

TRIGGERS = ["@centralagent"]
MULTI = ["@centralagent", "@ca"]


@pytest.mark.parametrize(
    "text,expected_question",
    [
        ("@centralagent summarize so far", "summarize so far"),
        ("hey @centralagent what did we decide?", "hey what did we decide?"),
        ("recap please @centralagent", "recap please"),
        ("@centralagent: list the action items", "list the action items"),
        ("@centralagent, who owns the deploy?", "who owns the deploy?"),
        ("@CentralAgent CASE insensitive", "CASE insensitive"),
        ("@centralagent   extra   spaces   here", "extra spaces here"),
    ],
)
def test_mention_extracts_question(text, expected_question):
    parsed = parse_mention(text, TRIGGERS)
    assert parsed.is_mention is True
    assert parsed.trigger == "@centralagent"
    assert parsed.question == expected_question


def test_non_mention_is_not_routed():
    parsed = parse_mention("just chatting about the roadmap", TRIGGERS)
    assert parsed.is_mention is False
    assert parsed.question == "just chatting about the roadmap"
    assert parsed.trigger is None


def test_handle_only_message_has_empty_question():
    parsed = parse_mention("@centralagent", TRIGGERS)
    assert parsed.is_mention is True
    assert parsed.question == ""


def test_handle_only_with_trailing_punct_has_empty_question():
    parsed = parse_mention("@centralagent ??", TRIGGERS)
    assert parsed.is_mention is True
    # "??" survives as the question (it's content, not edge punctuation we strip
    # around the handle) — but a bare handle + only edge punct is empty.
    assert parse_mention("@centralagent :", TRIGGERS).question == ""


def test_multiple_triggers_any_matches_and_all_stripped():
    parsed = parse_mention("@ca quick recap @centralagent", MULTI)
    assert parsed.is_mention is True
    assert parsed.question == "quick recap"


def test_empty_text_is_not_a_mention():
    parsed = parse_mention("", TRIGGERS)
    assert parsed.is_mention is False
    assert parsed.question == ""


def test_empty_triggers_never_match():
    parsed = parse_mention("@centralagent hello", [])
    assert parsed.is_mention is False
