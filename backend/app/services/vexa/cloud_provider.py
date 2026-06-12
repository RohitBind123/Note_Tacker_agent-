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
from app.services.http import request_with_retries
from app.services.vexa.provider import (
    BotDispatchResult,
    BotProvider,
    BotStatusResult,
    ChatMessage,
    ProviderError,
    TranscriptResult,
)

log = get_logger(__name__)

# Timeouts on every external boundary (connect, read).
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# POST /bots can take 10-20s for Vexa to bring a bot up; give the create real
# headroom so we don't time out (then fail/retry) before Vexa even responds —
# the root cause of a stranded live bot in prod (meeting #870).
_JOIN_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Vexa statuses that mean the bot is gone — never adopt one of these on a 409.
_TERMINAL_VEXA_STATUSES = {"completed", "failed", "stopped"}


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
        # Make the bot leave promptly once everyone else is gone, so insights are
        # delivered soon after the meeting ends (Vexa's 15-min default is too slow).
        payload["automatic_leave"] = {
            "max_time_left_alone": settings.vexa_leave_when_alone_seconds * 1000,
            "no_one_joined_timeout": settings.vexa_no_one_joined_timeout_seconds * 1000,
        }
        log.info(
            "vexa_join_request",
            native_meeting_id=native_meeting_id,
            platform=platform,
            leave_when_alone_s=settings.vexa_leave_when_alone_seconds,
        )
        # POST /bots is a non-idempotent CREATE: do NOT retry it (retries=0). A
        # retry after a slow-but-successful create makes a duplicate / a 409 and
        # orphans the live bot. If the create 409s ("already exists") or our read
        # times out, a bot may already be running for this code -> reconcile via
        # GET /bots and ADOPT it instead of failing.
        try:
            resp = await request_with_retries(
                "POST",
                f"{self._base}/bots",
                headers=self._headers(),
                json=payload,
                timeout=_JOIN_TIMEOUT,
                retries=0,
            )
        except httpx.TransportError as exc:
            adopted = await self._find_existing_bot(native_meeting_id, platform)
            if adopted is not None:
                log.info(
                    "vexa_join_adopted_after_timeout",
                    native_meeting_id=native_meeting_id,
                    vexa_bot_id=adopted.vexa_bot_id,
                    status=adopted.status,
                )
                return adopted
            log.error("vexa_join_timeout", native_meeting_id=native_meeting_id, error=str(exc))
            raise ProviderError("vexa join timed out and no existing bot found") from exc

        if resp.status_code in (200, 201):
            data = resp.json()
            result = BotDispatchResult(
                vexa_bot_id=str(data.get("id", "")),
                status=data.get("status", "requested"),
                raw=data,
            )
            log.info("vexa_join_ok", vexa_bot_id=result.vexa_bot_id, status=result.status)
            return result

        if resp.status_code == 409:
            adopted = await self._find_existing_bot(native_meeting_id, platform)
            if adopted is not None:
                log.info(
                    "vexa_join_adopted_on_conflict",
                    native_meeting_id=native_meeting_id,
                    vexa_bot_id=adopted.vexa_bot_id,
                    status=adopted.status,
                )
                return adopted
            log.error(
                "vexa_join_conflict_no_bot",
                native_meeting_id=native_meeting_id,
                body=resp.text[:300],
            )
            raise ProviderError(
                "vexa join conflict but no existing bot found",
                status_code=409,
                body=resp.text,
            )

        log.error("vexa_join_failed", status=resp.status_code, body=resp.text[:300])
        raise ProviderError("vexa join failed", status_code=resp.status_code, body=resp.text)

    async def _find_existing_bot(
        self, native_meeting_id: str, platform: str
    ) -> BotDispatchResult | None:
        """Find a live (non-terminal) Vexa bot for this code so it can be adopted.

        Used when POST /bots reports the bot already exists (409) or the create
        timed out: the bot may already be running, so GET /bots and pick the most
        recent non-terminal match rather than stranding it as FAILED_JOIN.
        """
        try:
            resp = await request_with_retries(
                "GET", f"{self._base}/bots", headers=self._headers(), timeout=_TIMEOUT
            )
        except httpx.TransportError as exc:
            log.warning(
                "vexa_find_existing_failed",
                native_meeting_id=native_meeting_id,
                error=str(exc),
            )
            return None
        if resp.status_code != 200:
            return None
        alive = [
            m
            for m in resp.json().get("meetings", [])
            if m.get("native_meeting_id") == native_meeting_id
            and m.get("platform") == platform
            and str(m.get("status", "")).lower() not in _TERMINAL_VEXA_STATUSES
        ]
        if not alive:
            return None
        latest = max(alive, key=lambda x: x.get("id", 0))
        return BotDispatchResult(
            vexa_bot_id=str(latest.get("id", "")),
            status=latest.get("status", "requested"),
            raw=latest,
        )

    async def get_status(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> BotStatusResult | None:
        resp = await request_with_retries(
            "GET", f"{self._base}/bots", headers=self._headers(), timeout=_TIMEOUT
        )
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
        resp = await request_with_retries(
            "GET", url, headers=self._headers(), timeout=httpx.Timeout(30.0, connect=5.0)
        )
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
        resp = await request_with_retries("DELETE", url, headers=self._headers(), timeout=_TIMEOUT)
        ok = resp.status_code in (200, 202, 204, 409)  # 409 = already stopping/stopped
        log.info("vexa_stop", native_meeting_id=native_meeting_id, status=resp.status_code, ok=ok)
        return ok

    # --- Phase 2: interactive copilot chat I/O ---

    async def get_chat(
        self, native_meeting_id: str, *, platform: str = "google_meet"
    ) -> list[ChatMessage]:
        url = f"{self._base}/bots/{platform}/{native_meeting_id}/chat"
        resp = await request_with_retries("GET", url, headers=self._headers(), timeout=_TIMEOUT)
        # 404 = bot not in a meeting yet / no chat surface: treat as "no messages"
        # rather than an error so the poll loop stays quiet before the bot joins.
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            log.error("vexa_get_chat_failed", status=resp.status_code, body=resp.text[:300])
            raise ProviderError("vexa get_chat failed", status_code=resp.status_code, body=resp.text)
        data = resp.json()
        raw_messages = _extract_chat_messages(data)
        messages = [_parse_chat_message(m) for m in raw_messages if isinstance(m, dict)]
        log.info("vexa_get_chat_ok", native_meeting_id=native_meeting_id, messages=len(messages))
        return messages

    async def send_chat(
        self, native_meeting_id: str, text: str, *, platform: str = "google_meet"
    ) -> bool:
        url = f"{self._base}/bots/{platform}/{native_meeting_id}/chat"
        resp = await request_with_retries(
            "POST", url, headers=self._headers(), json={"text": text}, timeout=_TIMEOUT
        )
        ok = resp.status_code in (200, 201, 202)
        if ok:
            log.info("vexa_send_chat_ok", native_meeting_id=native_meeting_id, chars=len(text))
        else:
            log.error(
                "vexa_send_chat_failed",
                native_meeting_id=native_meeting_id,
                status=resp.status_code,
                body=resp.text[:300],
            )
        return ok

    async def set_webhook(self, webhook_url: str, webhook_secret: str) -> bool:
        url = f"{self._base}/user/webhook"
        resp = await request_with_retries(
            "PUT",
            url,
            headers=self._headers(),
            json={"webhook_url": webhook_url, "webhook_secret": webhook_secret},
            timeout=_TIMEOUT,
        )
        ok = resp.status_code in (200, 201, 204)
        if ok:
            log.info("vexa_set_webhook_ok", webhook_url=webhook_url)
        else:
            log.error("vexa_set_webhook_failed", status=resp.status_code, body=resp.text[:300])
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


def _extract_chat_messages(data: dict | list) -> list:
    """Normalise a /chat payload to a list of message dicts (shape varies)."""
    if isinstance(data, dict):
        for key in ("messages", "chat", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    return []


def _parse_chat_message(m: dict) -> ChatMessage:
    """Map a raw Vexa chat dict to a ChatMessage (defensive on field names)."""
    ts = m.get("timestamp", m.get("time"))
    return ChatMessage(
        sender=str(m.get("sender") or m.get("speaker") or m.get("from") or "").strip(),
        text=str(m.get("text") or m.get("content") or m.get("message") or ""),
        timestamp=None if ts is None else str(ts),
        is_from_bot=bool(m.get("is_from_bot") or m.get("from_bot") or False),
        raw=m,
    )
