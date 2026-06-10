"""CloudVexaProvider — talks to api.cloud.vexa.ai.

Endpoints (verified live):
  POST   /bots                                  -> dispatch a bot
  GET    /bots                                  -> list running bots/meetings
  GET    /transcripts/{platform}/{native_id}    -> transcript
  DELETE /meetings/{platform}/{native_id}       -> stop the bot (leave call)
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.services.vexa.provider import (
    BotDispatchResult,
    BotProvider,
    BotStatusResult,
    ProviderError,
    TranscriptResult,
)

log = get_logger(__name__)

# Timeouts on every external boundary (connect, read).
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class CloudVexaProvider(BotProvider):
    def __init__(self, *, api_base: str | None = None, api_key: str | None = None) -> None:
        self._base = (api_base or settings.vexa_api_base).rstrip("/")
        self._key = api_key or settings.vexa_api_key
        if not self._key:
            raise ProviderError("VEXA_API_KEY is not configured")

    def _headers(self) -> dict:
        return {"X-API-Key": self._key, "Content-Type": "application/json"}

    async def join(
        self, native_meeting_id: str, *, platform: str = "google_meet", bot_name: str | None = None
    ) -> BotDispatchResult:
        payload = {"platform": platform, "native_meeting_id": native_meeting_id}
        if bot_name:
            payload["bot_name"] = bot_name
        log.info("vexa_join_request", native_meeting_id=native_meeting_id, platform=platform)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{self._base}/bots", headers=self._headers(), json=payload)
        if resp.status_code not in (200, 201):
            log.error("vexa_join_failed", status=resp.status_code, body=resp.text[:300])
            raise ProviderError("vexa join failed", status_code=resp.status_code, body=resp.text)
        data = resp.json()
        result = BotDispatchResult(
            vexa_bot_id=str(data.get("id", "")),
            status=data.get("status", "requested"),
            raw=data,
        )
        log.info("vexa_join_ok", vexa_bot_id=result.vexa_bot_id, status=result.status)
        return result

    async def get_status(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> BotStatusResult | None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{self._base}/bots", headers=self._headers())
        if resp.status_code != 200:
            log.error("vexa_status_failed", status=resp.status_code, body=resp.text[:300])
            raise ProviderError("vexa status failed", status_code=resp.status_code, body=resp.text)
        meetings = resp.json().get("meetings", [])
        for m in meetings:
            if m.get("native_meeting_id") == native_meeting_id and m.get("platform") == platform:
                data = m.get("data") or {}
                status = BotStatusResult(
                    status=m.get("status", "unknown"),
                    participants_count=data.get("participants_count"),
                    has_recording=bool(data.get("has_recording")),
                    raw=m,
                )
                log.info(
                    "vexa_status",
                    native_meeting_id=native_meeting_id,
                    status=status.status,
                    participants=status.participants_count,
                )
                return status
        log.info("vexa_status_inactive", native_meeting_id=native_meeting_id)
        return None

    async def get_transcript(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> TranscriptResult:
        url = f"{self._base}/transcripts/{platform}/{native_meeting_id}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code != 200:
            log.error("vexa_transcript_failed", status=resp.status_code, body=resp.text[:300])
            raise ProviderError(
                "vexa transcript failed", status_code=resp.status_code, body=resp.text
            )
        data = resp.json()
        segments = _extract_segments(data)
        full_text = _segments_to_text(segments)
        log.info(
            "vexa_transcript_ok",
            native_meeting_id=native_meeting_id,
            segments=len(segments),
            chars=len(full_text),
        )
        return TranscriptResult(segments=segments, full_text=full_text, raw=data)

    async def stop(self, native_meeting_id: str, *, platform: str = "google_meet") -> bool:
        url = f"{self._base}/meetings/{platform}/{native_meeting_id}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(url, headers=self._headers())
        ok = resp.status_code in (200, 202, 204, 409)  # 409 = already stopping/stopped
        log.info("vexa_stop", native_meeting_id=native_meeting_id, status=resp.status_code, ok=ok)
        return ok


def _extract_segments(data: dict) -> list:
    """Vexa transcript payloads vary slightly; normalise to a list of segments."""
    if isinstance(data, dict):
        for key in ("segments", "transcripts", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    return []


def _segments_to_text(segments: list) -> str:
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        speaker = seg.get("speaker") or seg.get("speaker_name") or seg.get("participant") or ""
        text = seg.get("text") or seg.get("content") or ""
        if not text:
            continue
        lines.append(f"{speaker}: {text}".strip(" :") if speaker else text)
    return "\n".join(lines)
