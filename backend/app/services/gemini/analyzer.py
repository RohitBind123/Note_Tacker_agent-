"""Gemini transcript analyzer.

Turns a raw meeting transcript into a structured report (summary, decisions,
action items, risks, next steps) using Gemini structured output
(``responseMimeType=application/json`` + ``responseSchema``). The model id comes
from config (``GEMINI_MODEL``), never hardcoded.

Data-quality guards:
  - Empty/too-short transcripts are NOT sent to the model; we return an explicit
    "insufficient content" report instead of inviting hallucination.
  - The prompt instructs the model to extract only what's present and to leave a
    category empty rather than invent owners/dates.
"""
from __future__ import annotations

import json

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_MIN_CHARS = 20
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

# Gemini response schema (OpenAPI subset).
_RESPONSE_SCHEMA = {
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
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "decisions", "action_items", "risks", "next_steps"],
}

_PROMPT = """You are a precise meeting-notes analyst. From the meeting transcript below, \
extract a structured report.

Rules:
- Use ONLY information present in the transcript. Do not invent facts, owners, dates, or numbers.
- If a category has nothing in the transcript, return an empty list for it.
- The transcript may be imperfect or mixed-language; summarize in clear English.
- "summary" should be 2-5 sentences capturing what the meeting was about and its outcome.
- "action_items": each has an optional "owner" (only if a person is clearly named) and a "task".

Transcript:
\"\"\"
{transcript}
\"\"\"
"""


def _insufficient_report() -> dict:
    return {
        "summary": "The transcript was too short or empty to analyze.",
        "decisions": [],
        "action_items": [],
        "risks": [],
        "next_steps": [],
        "insufficient_content": True,
    }


class GeminiAnalyzer:
    def __init__(
        self, *, model: str | None = None, api_key: str | None = None, api_base: str | None = None
    ) -> None:
        self._model = model or settings.gemini_model
        self._key = api_key or settings.gemini_api_key
        self._base = (api_base or settings.gemini_api_base).rstrip("/")
        if not self._key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

    async def analyze(self, transcript_text: str) -> dict:
        text = (transcript_text or "").strip()
        if len(text) < _MIN_CHARS:
            log.warning("gemini_transcript_too_short", chars=len(text))
            return _insufficient_report()

        url = f"{self._base}/models/{self._model}:generateContent?key={self._key}"
        body = {
            "contents": [{"parts": [{"text": _PROMPT.format(transcript=text)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
                "temperature": 0.2,
            },
        }
        log.info("gemini_analyze_request", model=self._model, transcript_chars=len(text))
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            log.error("gemini_analyze_failed", status=resp.status_code, body=resp.text[:300])
            raise RuntimeError(f"gemini analyze failed ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        try:
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            report = json.loads(raw_text)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            log.error("gemini_parse_failed", error=str(exc), body=json.dumps(data)[:300])
            raise RuntimeError(f"could not parse Gemini response: {exc}") from exc

        report["insufficient_content"] = False
        log.info(
            "gemini_analyzed",
            model=self._model,
            decisions=len(report.get("decisions", [])),
            action_items=len(report.get("action_items", [])),
            risks=len(report.get("risks", [])),
        )
        return report
