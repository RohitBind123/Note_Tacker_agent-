import httpx
import respx

from app.services.vexa.cloud_provider import CloudVexaProvider

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
