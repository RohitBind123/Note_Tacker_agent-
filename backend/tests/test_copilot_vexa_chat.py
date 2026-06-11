"""Unit tests for the Phase 2 Vexa chat I/O (Batch 2): get_chat / send_chat / set_webhook."""
import httpx
import respx

from app.services.vexa.cloud_provider import CloudVexaProvider

BASE = "https://api.cloud.vexa.ai"
NID = "aex-ihfj-gvg"


def _provider() -> CloudVexaProvider:
    return CloudVexaProvider(api_base=BASE, api_key="vxa_bot_test")


@respx.mock
async def test_get_chat_parses_messages():
    body = {
        "messages": [
            {"sender": "Priya", "text": "@centralagent what did we decide?",
             "timestamp": 1718000000.5, "is_from_bot": False},
            {"sender": "CentralAgent", "text": "We decided X.",
             "timestamp": 1718000005, "is_from_bot": True},
        ]
    }
    respx.get(f"{BASE}/bots/google_meet/{NID}/chat").mock(
        return_value=httpx.Response(200, json=body)
    )
    msgs = await _provider().get_chat(NID)
    assert len(msgs) == 2
    assert msgs[0].sender == "Priya"
    assert msgs[0].text.startswith("@centralagent")
    assert msgs[0].timestamp == "1718000000.5"  # stringified, lossless
    assert msgs[0].is_from_bot is False
    assert msgs[1].is_from_bot is True


@respx.mock
async def test_get_chat_404_returns_empty():
    # Bot not in a meeting yet -> quiet empty, not an error.
    respx.get(f"{BASE}/bots/google_meet/{NID}/chat").mock(return_value=httpx.Response(404))
    assert await _provider().get_chat(NID) == []


@respx.mock
async def test_get_chat_tolerates_bare_list_payload():
    respx.get(f"{BASE}/bots/google_meet/{NID}/chat").mock(
        return_value=httpx.Response(200, json=[{"sender": "A", "text": "hi"}])
    )
    msgs = await _provider().get_chat(NID)
    assert len(msgs) == 1 and msgs[0].text == "hi"


@respx.mock
async def test_send_chat_ok():
    route = respx.post(f"{BASE}/bots/google_meet/{NID}/chat").mock(
        return_value=httpx.Response(200, json={"status": "sent"})
    )
    assert await _provider().send_chat(NID, "hello team") is True
    assert route.called
    assert b"hello team" in route.calls.last.request.content


@respx.mock
async def test_send_chat_failure_returns_false():
    # 400 is not retried -> fast, returns False (caller marks interaction FAILED).
    respx.post(f"{BASE}/bots/google_meet/{NID}/chat").mock(return_value=httpx.Response(400))
    assert await _provider().send_chat(NID, "hello") is False


@respx.mock
async def test_set_webhook_ok():
    route = respx.put(f"{BASE}/user/webhook").mock(return_value=httpx.Response(200, json={}))
    ok = await _provider().set_webhook("https://x.dev/webhooks/vexa", "shh")
    assert ok is True
    body = route.calls.last.request.content
    assert b"webhook_url" in body and b"webhook_secret" in body
