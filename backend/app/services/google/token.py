"""Async Google OAuth access-token provider.

Exchanges the long-lived refresh token (for centralagentai@gmail.com) for a
short-lived access token via the OAuth token endpoint, caching it until just
before expiry. Kept async + dependency-light (httpx) so it composes with the
rest of the app; shared by the Calendar and Gmail clients.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.services.http import request_with_retries

log = get_logger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EXPIRY_SKEW_SECONDS = 60  # refresh a bit early

_lock = asyncio.Lock()
_cached_token: str | None = None
_cached_expiry: float = 0.0


class GoogleAuthError(RuntimeError):
    pass


async def get_access_token(*, force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing via the refresh token as needed."""
    global _cached_token, _cached_expiry

    now = time.monotonic()
    if not force_refresh and _cached_token and now < _cached_expiry:
        return _cached_token

    async with _lock:
        now = time.monotonic()
        if not force_refresh and _cached_token and now < _cached_expiry:
            return _cached_token

        if not (
            settings.google_oauth_client_id
            and settings.google_oauth_client_secret
            and settings.google_oauth_refresh_token
        ):
            raise GoogleAuthError("Google OAuth client/refresh-token not configured")

        data = {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "refresh_token": settings.google_oauth_refresh_token,
            "grant_type": "refresh_token",
        }
        resp = await request_with_retries(
            "POST", _TOKEN_URL, data=data, timeout=httpx.Timeout(10.0, connect=5.0)
        )
        if resp.status_code != 200:
            log.error("google_token_refresh_failed", status=resp.status_code, body=resp.text[:300])
            raise GoogleAuthError(f"token refresh failed ({resp.status_code}): {resp.text[:200]}")

        payload = resp.json()
        _cached_token = payload["access_token"]
        _cached_expiry = time.monotonic() + int(payload.get("expires_in", 3600)) - _EXPIRY_SKEW_SECONDS
        log.info("google_token_refreshed", expires_in=payload.get("expires_in"))
        return _cached_token
