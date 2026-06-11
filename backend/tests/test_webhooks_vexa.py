"""Endpoint tests for POST /webhooks/vexa security + idempotency gates (Batch 6).

Mounts only the webhooks router on a bare app (no lifespan, no DB) and drives it
over httpx's ASGI transport. We exercise every branch that does NOT touch the
database — bad signature, stale timestamp, malformed body, unknown event, and
duplicate delivery. The meeting.completed -> finalize path is DB-driven and is
validated against a real meeting in the E2E batch.
"""
import json
import time

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import webhooks
from app.config import settings
from app.services.copilot.webhook import compute_signature

SECRET = "whsec_endpoint_test"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setattr(settings, "vexa_webhook_secret", SECRET)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(webhooks.router)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _signed_headers(raw: bytes, *, ts: str | None = None, secret: str = SECRET) -> dict:
    ts = ts or str(int(time.time()))
    return {
        "X-Webhook-Signature": compute_signature(secret, ts, raw),
        "X-Webhook-Timestamp": ts,
        "Content-Type": "application/json",
    }


async def test_rejects_bad_signature():
    raw = json.dumps({"event_id": "e1", "event_type": "meeting.started"}).encode()
    headers = _signed_headers(raw, secret="the-wrong-secret")
    async with _client(_app()) as c:
        resp = await c.post("/webhooks/vexa", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_rejects_stale_timestamp_even_with_valid_signature():
    raw = json.dumps({"event_id": "e2", "event_type": "meeting.started"}).encode()
    old_ts = str(int(time.time()) - 10_000)  # well outside the replay window
    headers = _signed_headers(raw, ts=old_ts)  # correctly signed for that old ts
    async with _client(_app()) as c:
        resp = await c.post("/webhooks/vexa", content=raw, headers=headers)
    assert resp.status_code == 401


async def test_malformed_json_is_acknowledged_not_retried():
    raw = b"this is not json"
    headers = _signed_headers(raw)  # signature is over raw bytes, so it verifies
    async with _client(_app()) as c:
        resp = await c.post("/webhooks/vexa", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_unknown_event_without_type_is_ignored():
    raw = json.dumps({"data": {"meeting": {}}}).encode()  # no event_type
    headers = _signed_headers(raw)
    async with _client(_app()) as c:
        resp = await c.post("/webhooks/vexa", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_non_completed_event_is_acknowledged_without_db():
    raw = json.dumps({"event_id": "evt_started_1", "event_type": "meeting.started"}).encode()
    headers = _signed_headers(raw)
    async with _client(_app()) as c:
        resp = await c.post("/webhooks/vexa", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_duplicate_event_id_is_deduped():
    raw = json.dumps({"event_id": "evt_dup_unique_xyz", "event_type": "meeting.started"}).encode()
    headers = _signed_headers(raw)
    async with _client(_app()) as c:
        first = await c.post("/webhooks/vexa", content=raw, headers=headers)
        # Re-sign with a fresh timestamp so only the event_id dedup (not the
        # replay window) is what stops the second delivery.
        second_headers = _signed_headers(raw)
        second = await c.post("/webhooks/vexa", content=raw, headers=second_headers)
    assert first.status_code == 200 and first.json()["status"] == "ok"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
