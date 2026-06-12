import httpx
import pytest
import respx

from app.services.vexa.cloud_provider import CloudVexaProvider
from app.services.vexa.provider import ProviderError

BASE = "https://api.cloud.vexa.ai"


def _provider() -> CloudVexaProvider:
    return CloudVexaProvider(api_base=BASE, api_key="vxa_bot_test")


@respx.mock
async def test_join_maps_response():
    respx.post(f"{BASE}/bots").mock(
        return_value=httpx.Response(201, json={"id": 14837, "status": "requested"})
    )
    res = await _provider().join("aex-ihfj-gvg", bot_name="Bot")
    assert res.vexa_bot_id == "14837"
    assert res.status == "requested"


@respx.mock
async def test_get_status_found_and_not_found():
    payload = {
        "meetings": [
            {
                "native_meeting_id": "aex-ihfj-gvg",
                "platform": "google_meet",
                "status": "active",
                "data": {"participants_count": 2, "has_recording": True},
            }
        ]
    }
    route = respx.get(f"{BASE}/bots").mock(return_value=httpx.Response(200, json=payload))
    res = await _provider().get_status("aex-ihfj-gvg")
    assert res is not None and res.status == "active" and res.participants_count == 2

    route.mock(return_value=httpx.Response(200, json={"meetings": []}))
    assert await _provider().get_status("aex-ihfj-gvg") is None


@respx.mock
async def test_get_transcript_parses_segments():
    body = {"segments": [{"speaker": "John", "text": "Hello"}, {"speaker": "Jane", "text": "Hi"}]}
    respx.get(f"{BASE}/transcripts/google_meet/aex-ihfj-gvg").mock(
        return_value=httpx.Response(200, json=body)
    )
    res = await _provider().get_transcript("aex-ihfj-gvg")
    assert len(res.segments) == 2
    assert res.full_text == "John: Hello\nJane: Hi"


@respx.mock
async def test_stop_treats_409_as_ok():
    respx.delete(f"{BASE}/meetings/google_meet/aex-ihfj-gvg").mock(
        return_value=httpx.Response(409)
    )
    assert await _provider().stop("aex-ihfj-gvg") is True


# --- join reconciliation: a 409 / timeout means a bot may already exist ------
# (regression for prod meeting #870 — a retried POST /bots got 409 and the live
# bot was wrongly marked FAILED_JOIN.)

_ADOPT_PAYLOAD = {
    "meetings": [
        # an OLD terminal bot for the same code -> must be ignored
        {"id": 14000, "native_meeting_id": "aex-ihfj-gvg", "platform": "google_meet", "status": "completed"},
        # the live one -> must be adopted
        {"id": 15070, "native_meeting_id": "aex-ihfj-gvg", "platform": "google_meet", "status": "active"},
        # a live bot for a DIFFERENT code -> must be ignored
        {"id": 99, "native_meeting_id": "other-code", "platform": "google_meet", "status": "active"},
    ]
}


@respx.mock
async def test_join_409_adopts_existing_live_bot():
    post = respx.post(f"{BASE}/bots").mock(
        return_value=httpx.Response(
            409, json={"detail": "An active or requested meeting already exists"}
        )
    )
    respx.get(f"{BASE}/bots").mock(return_value=httpx.Response(200, json=_ADOPT_PAYLOAD))

    res = await _provider().join("aex-ihfj-gvg", bot_name="Bot")

    assert res.vexa_bot_id == "15070"  # adopted the live bot, not the completed one
    assert res.status == "active"
    assert post.call_count == 1  # the non-idempotent create is NOT retried


@respx.mock
async def test_join_409_with_no_live_bot_raises():
    respx.post(f"{BASE}/bots").mock(return_value=httpx.Response(409, json={"detail": "conflict"}))
    # Only terminal / other-code bots exist -> nothing to adopt.
    respx.get(f"{BASE}/bots").mock(
        return_value=httpx.Response(
            200,
            json={"meetings": [
                {"id": 14000, "native_meeting_id": "aex-ihfj-gvg", "platform": "google_meet", "status": "completed"},
            ]},
        )
    )
    with pytest.raises(ProviderError):
        await _provider().join("aex-ihfj-gvg")


@respx.mock
async def test_join_timeout_adopts_existing_bot_without_retrying_create():
    # The first POST is slow (Vexa created the bot but our read timed out). We must
    # NOT fire a second create; instead reconcile via GET /bots and adopt.
    post = respx.post(f"{BASE}/bots").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{BASE}/bots").mock(return_value=httpx.Response(200, json=_ADOPT_PAYLOAD))

    res = await _provider().join("aex-ihfj-gvg")

    assert res.vexa_bot_id == "15070"
    assert res.status == "active"
    assert post.call_count == 1  # created at most once despite the timeout


@respx.mock
async def test_join_timeout_with_no_bot_raises():
    respx.post(f"{BASE}/bots").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{BASE}/bots").mock(return_value=httpx.Response(200, json={"meetings": []}))
    with pytest.raises(ProviderError):
        await _provider().join("aex-ihfj-gvg")
