"""Shared async HTTP with timeouts + bounded retries.

Centralizes the "every external boundary has a deadline and retries transient
failures" rule. Retries on transport errors and 5xx/429 with exponential
backoff; never retries 4xx (those are our fault, not transient).
"""
from __future__ import annotations

import asyncio

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_RETRY_STATUS = {429, 500, 502, 503, 504}


async def request_with_retries(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json: dict | None = None,
    data: dict | None = None,
    params: dict | None = None,
    timeout: httpx.Timeout | None = None,
    retries: int = 2,
    backoff_base: float = 0.5,
) -> httpx.Response:
    """Perform an HTTP request with retries on transient failures."""
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= retries:
        try:
            async with httpx.AsyncClient(timeout=timeout or _DEFAULT_TIMEOUT) as client:
                resp = await client.request(
                    method, url, headers=headers, json=json, data=data, params=params
                )
            if resp.status_code in _RETRY_STATUS and attempt < retries:
                log.warning(
                    "http_retry_status", url=url, status=resp.status_code, attempt=attempt
                )
            else:
                return resp
        except httpx.TransportError as exc:  # connect/read/timeout/network
            last_exc = exc
            if attempt >= retries:
                log.error("http_transport_failed", url=url, error=str(exc), attempt=attempt)
                raise
            log.warning("http_retry_transport", url=url, error=str(exc), attempt=attempt)
        await asyncio.sleep(backoff_base * (2**attempt))
        attempt += 1
    # Exhausted retries on retryable status -> return the last response.
    if last_exc:
        raise last_exc
    return resp
