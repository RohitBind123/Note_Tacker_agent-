"""Unit tests for the copilot 'thinking' acknowledgement helper.

The ack is best-effort feedback posted to chat before the slow retrieval + LLM
step. This pins the three behaviours that matter: it sends when enabled, sends
nothing when disabled, and a provider failure is swallowed (never breaks the
answer path). A stub provider stands in for Vexa — no DB is touched, so this is a
pure unit test; the full mention round-trip is covered by the live E2E.
"""
from types import SimpleNamespace

from app.config import settings
from app.services.copilot.router import _send_thinking_ack


class _StubProvider:
    """Records send_chat calls; optionally raises to simulate Vexa being down."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail = fail

    async def send_chat(self, native_meeting_id: str, text: str, *, platform: str = "google_meet") -> bool:
        self.calls.append((native_meeting_id, text, platform))
        if self._fail:
            raise RuntimeError("vexa unreachable")
        return True


def _meeting() -> SimpleNamespace:
    return SimpleNamespace(native_meeting_id="abc-defg-hij", platform="google_meet")


async def test_ack_sent_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "copilot_thinking_ack_enabled", True)
    monkeypatch.setattr(settings, "copilot_thinking_ack_text", "thinking...")
    provider = _StubProvider()

    ok = await _send_thinking_ack(provider, _meeting())

    assert ok is True
    assert provider.calls == [("abc-defg-hij", "thinking...", "google_meet")]


async def test_ack_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "copilot_thinking_ack_enabled", False)
    provider = _StubProvider()

    ok = await _send_thinking_ack(provider, _meeting())

    assert ok is False
    assert provider.calls == []  # disabled -> no chat call at all


def test_ack_disabled_by_default():
    # Meet chat is append-only, so the placeholder can never be replaced by the
    # answer; it must stay off unless a deployment explicitly opts in via env.
    assert settings.copilot_thinking_ack_enabled is False


async def test_ack_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(settings, "copilot_thinking_ack_enabled", True)
    monkeypatch.setattr(settings, "copilot_thinking_ack_text", "thinking...")
    provider = _StubProvider(fail=True)

    # Must NOT raise — a failed acknowledgement can never break the answer path.
    ok = await _send_thinking_ack(provider, _meeting())

    assert ok is False
    assert len(provider.calls) == 1  # it tried exactly once
