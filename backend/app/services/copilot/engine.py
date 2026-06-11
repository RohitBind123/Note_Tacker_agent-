"""Copilot Q&A engine.

Given a participant's question and the assembled grounding context (retrieved
transcript chunks + rolling meeting memory + recent chat + meeting metadata),
produce a short, chat-appropriate answer with Gemini.

Two halves, deliberately split so the risky part is unit-testable:
  - ``build_context_block`` (pure): turns the structured grounding into the text
    the model sees. No I/O.
  - ``CopilotEngine.answer``: the single Gemini call.

Grounding discipline (mirrors the analyzer/memory no-hallucination policy): the
prompt instructs the model to answer ONLY from the provided context and to say
so plainly when the answer isn't there — a meeting copilot that invents
decisions is worse than one that admits it didn't catch something.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.services.http import request_with_retries

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(45.0, connect=5.0)
# Hard cap on the reply so the bot never floods the meeting chat.
MAX_ANSWER_CHARS = 1500


@dataclass(frozen=True)
class CopilotContext:
    """Everything the engine grounds an answer on."""

    meeting_title: str | None = None
    memory_summary: str | None = None
    decisions: list[str] | None = None
    action_items: list[dict] | None = None
    open_questions: list[str] | None = None
    transcript_snippets: list[str] | None = None
    recent_chat: list[str] | None = None


_PROMPT = """You are {bot_name}, a meeting copilot answering a participant in the \
live meeting chat. Answer their question using ONLY the context below.

Rules:
- Be concise and direct — this is a chat reply, not a report. 1-4 sentences usually.
- Use ONLY the provided context. Do NOT invent decisions, owners, dates, or numbers.
- If the answer is not in the context, say so plainly (e.g. "I haven't caught that
  being discussed yet") rather than guessing.
- When listing action items or decisions, attribute owners only if the context names them.
- Plain text only (this is posted into a chat box); no markdown headings.

{context}

Participant's question:
{question}
"""


def _bullet_list(label: str, items: list | None, *, render=str) -> str:
    if not items:
        return ""
    lines = "\n".join(f"- {render(item)}" for item in items)
    return f"{label}:\n{lines}\n"


def _render_action_item(item: dict) -> str:
    if not isinstance(item, dict):
        return str(item)
    owner = (item.get("owner") or "").strip()
    task = (item.get("task") or "").strip()
    return f"{owner}: {task}" if owner else task


def build_context_block(ctx: CopilotContext) -> str:
    """Render the grounding context into the prompt's context section (pure)."""
    parts: list[str] = []
    if ctx.meeting_title:
        parts.append(f"Meeting: {ctx.meeting_title}\n")
    if ctx.memory_summary:
        parts.append(f"Meeting so far:\n{ctx.memory_summary}\n")
    parts.append(_bullet_list("Decisions made", ctx.decisions))
    parts.append(_bullet_list("Action items", ctx.action_items, render=_render_action_item))
    parts.append(_bullet_list("Open questions", ctx.open_questions))
    if ctx.transcript_snippets:
        joined = "\n---\n".join(ctx.transcript_snippets)
        parts.append(f"Relevant transcript excerpts:\n{joined}\n")
    parts.append(_bullet_list("Recent chat", ctx.recent_chat))

    body = "\n".join(p for p in parts if p).strip()
    if not body:
        return "Context:\n(No meeting content has been captured yet.)"
    return f"Context:\n{body}"


class CopilotEngine:
    """Generates a grounded chat answer via Gemini."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        bot_name: str | None = None,
    ) -> None:
        self._model = model or settings.gemini_model
        self._key = api_key or settings.gemini_api_key
        self._base = (api_base or settings.gemini_api_base).rstrip("/")
        self._bot_name = bot_name or settings.copilot_bot_name
        if not self._key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

    @property
    def model(self) -> str:
        return self._model

    async def answer(self, question: str, ctx: CopilotContext) -> str:
        """Return a grounded, length-capped answer to ``question``."""
        prompt = _PROMPT.format(
            bot_name=self._bot_name,
            context=build_context_block(ctx),
            question=question.strip(),
        )
        url = f"{self._base}/models/{self._model}:generateContent?key={self._key}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 512},
        }
        log.info("copilot_answer_request", model=self._model, question_chars=len(question))
        resp = await request_with_retries("POST", url, json=body, timeout=_TIMEOUT)
        if resp.status_code != 200:
            log.error("copilot_answer_failed", status=resp.status_code, body=resp.text[:300])
            raise RuntimeError(
                f"copilot answer failed ({resp.status_code}): {resp.text[:200]}"
            )
        text = _extract_text(resp.json())
        if not text:
            raise RuntimeError("copilot answer returned empty text")
        return text[:MAX_ANSWER_CHARS].strip()


def _extract_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
