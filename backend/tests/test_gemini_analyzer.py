import json

import httpx
import respx

from app.services.gemini.analyzer import GeminiAnalyzer

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _analyzer() -> GeminiAnalyzer:
    return GeminiAnalyzer(model="gemini-2.5-flash", api_key="test-key", api_base=BASE)


async def test_short_transcript_returns_insufficient_without_calling_model():
    # No respx route registered -> if it tried to call the API, it would error.
    report = await _analyzer().analyze("hi")
    assert report["insufficient_content"] is True
    assert report["summary"]
    assert report["decisions"] == [] and report["action_items"] == []


@respx.mock
async def test_analyze_parses_structured_json():
    model_json = {
        "summary": "Team discussed the backend deploy and QA timeline.",
        "decisions": ["Ship to production on Friday"],
        "action_items": [{"owner": "John", "task": "Deploy backend"}, {"task": "Finish QA"}],
        "risks": ["QA not complete"],
        "next_steps": ["Complete testing", "Release Friday"],
    }
    gemini_response = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(model_json)}]}}]
    }
    respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json=gemini_response)
    )
    report = await _analyzer().analyze(
        "John: backend is deployed. Jane: QA starts tomorrow, ship Friday. " * 2
    )
    assert report["insufficient_content"] is False
    assert report["summary"].startswith("Team discussed")
    assert len(report["action_items"]) == 2
    assert report["decisions"] == ["Ship to production on Friday"]
