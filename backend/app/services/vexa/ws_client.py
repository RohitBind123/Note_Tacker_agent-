"""Vexa real-time WebSocket client (chat.received + transcript.mutable).

The PREFERRED live channel for the copilot — push beats polling. Because the
exact live envelope can only be confirmed against a running meeting, the parser
is deliberately defensive (accepts several field spellings) and the manager
(Batch 6) can fall back to REST polling via ``COPILOT_LIVE_CHANNEL=poll``.

This module keeps the wire-format logic in pure functions
(``parse_ws_event`` / ``build_subscribe_payload``) so they are unit-testable
without a live socket; ``VexaWebSocketClient`` is the thin reconnecting loop.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import websockets

from app.config import settings
from app.logging_config import get_logger
from app.services.vexa.provider import ChatMessage

log = get_logger(__name__)

# Event type prefixes we care about (defensive: match by suffix too).
CHAT_RECEIVED = "chat.received"
TRANSCRIPT_MUTABLE = "transcript.mutable"


@dataclass
class VexaWsEvent:
    """A parsed WebSocket event."""

    type: str
    platform: str | None
    native_id: str | None
    data: dict
    raw: dict = field(default_factory=dict)

    @property
    def is_chat(self) -> bool:
        return self.type.endswith("chat.received") or self.type == "chat"

    @property
    def is_transcript(self) -> bool:
        return "transcript" in self.type

    def as_chat_message(self) -> ChatMessage | None:
        """Extract a ChatMessage from a chat event, or None if not chat-shaped."""
        if not self.is_chat:
            return None
        src = self.data if isinstance(self.data, dict) else {}
        ts = src.get("timestamp", src.get("time"))
        text = str(src.get("text") or src.get("content") or src.get("message") or "")
        sender = str(src.get("sender") or src.get("speaker") or src.get("from") or "").strip()
        if not text:
            return None
        return ChatMessage(
            sender=sender,
            text=text,
            timestamp=None if ts is None else str(ts),
            is_from_bot=bool(src.get("is_from_bot") or src.get("from_bot") or False),
            raw=src,
        )


def _meeting_pair(raw: dict) -> tuple[str | None, str | None]:
    """Pull (platform, native_id) from whatever shape the envelope uses."""
    meeting = raw.get("meeting")
    if isinstance(meeting, dict):
        native = (
            meeting.get("native_id")
            or meeting.get("native_meeting_id")
            or meeting.get("id")
        )
        return meeting.get("platform"), (None if native is None else str(native))
    native = raw.get("native_id") or raw.get("native_meeting_id")
    return raw.get("platform"), (None if native is None else str(native))


def parse_ws_event(raw: str | bytes | dict) -> VexaWsEvent | None:
    """Parse a raw WS frame into a VexaWsEvent, or None if unrecognisable.

    Pure + defensive: tolerates JSON string/bytes/dict, and ``type``/``event``/
    ``action`` for the event name. Returns None for control frames (acks,
    pings, subscribe confirmations) that carry no event type.
    """
    if isinstance(raw, (str, bytes)):
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        obj = raw
    if not isinstance(obj, dict):
        return None
    etype = obj.get("type") or obj.get("event") or obj.get("event_type")
    if not etype or not isinstance(etype, str):
        return None
    platform, native_id = _meeting_pair(obj)
    data = obj.get("data")
    if not isinstance(data, dict):
        # Flat envelope: treat the frame (minus routing keys) as the payload.
        data = {k: v for k, v in obj.items() if k not in {"type", "event", "event_type", "meeting"}}
    return VexaWsEvent(type=etype, platform=platform, native_id=native_id, data=data, raw=obj)


def build_subscribe_payload(pairs: list[tuple[str, str]], *, action: str = "subscribe") -> str:
    """Build the subscribe/unsubscribe JSON frame for a set of meetings."""
    return json.dumps(
        {
            "action": action,
            "meetings": [{"platform": p, "native_id": n} for p, n in pairs],
        }
    )


EventHandler = Callable[[VexaWsEvent], Awaitable[None]]


class VexaWebSocketClient:
    """Reconnecting WS client that fans parsed events to a handler.

    Subscriptions are tracked locally and re-sent on every (re)connect, so a
    dropped socket transparently resumes all active meetings.
    """

    def __init__(self, *, ws_url: str | None = None, api_key: str | None = None) -> None:
        self._url = ws_url or settings.vexa_ws_url
        self._key = api_key or settings.vexa_api_key
        self._subs: set[tuple[str, str]] = set()
        self._ws: websockets.ClientConnection | None = None
        self._connected = asyncio.Event()

    async def subscribe(self, platform: str, native_id: str) -> None:
        self._subs.add((platform, native_id))
        await self._send(build_subscribe_payload([(platform, native_id)]))

    async def unsubscribe(self, platform: str, native_id: str) -> None:
        self._subs.discard((platform, native_id))
        await self._send(build_subscribe_payload([(platform, native_id)], action="unsubscribe"))

    async def _send(self, frame: str) -> None:
        ws = self._ws
        if ws is None:
            return  # not connected yet; _resubscribe will replay on connect
        try:
            await ws.send(frame)
        except Exception as exc:  # noqa: BLE001 - best-effort; reconnect replays
            log.warning("vexa_ws_send_failed", error=str(exc))

    async def _resubscribe(self) -> None:
        if self._subs:
            await self._send(build_subscribe_payload(sorted(self._subs)))

    async def run(self, handler: EventHandler, stop_event: asyncio.Event) -> None:
        """Connect, (re)subscribe, and fan events to ``handler`` until stopped."""
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    additional_headers={"X-API-Key": self._key},
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = 1.0
                    log.info("vexa_ws_connected", url=self._url, subs=len(self._subs))
                    await self._resubscribe()
                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        evt = parse_ws_event(raw)
                        if evt is None:
                            continue
                        try:
                            await handler(evt)
                        except Exception as exc:  # noqa: BLE001 - one bad event must not kill the loop
                            log.error("vexa_ws_handler_error", type=evt.type, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - connection error: back off + retry
                log.warning("vexa_ws_disconnected", error=str(exc))
            finally:
                self._ws = None
                self._connected.clear()
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
        log.info("vexa_ws_stopped")
