import httpx
import respx

from app.services.http import request_with_retries

URL = "https://example.test/api"


@respx.mock
async def test_retries_on_503_then_succeeds():
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    resp = await request_with_retries("GET", URL, retries=2, backoff_base=0)
    assert resp.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_no_retry_on_400():
    route = respx.get(URL).mock(return_value=httpx.Response(400))
    resp = await request_with_retries("GET", URL, retries=2, backoff_base=0)
    assert resp.status_code == 400
    assert route.call_count == 1  # 4xx is our fault, never retried


@respx.mock
async def test_retries_on_transport_error_then_succeeds():
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"ok": True})]
    )
    resp = await request_with_retries("GET", URL, retries=2, backoff_base=0)
    assert resp.status_code == 200
    assert route.call_count == 2
