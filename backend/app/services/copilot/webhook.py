"""Vexa webhook verification + envelope parsing (pure, no I/O).

Vexa signs each webhook delivery so we can prove it really came from Vexa and
hasn't been tampered with or replayed:

  Authorization:        Bearer <webhook_secret>
  X-Webhook-Signature:  sha256=<hex hmac>
  X-Webhook-Timestamp:  <unix seconds, as sent>

The signature is ``HMAC-SHA256(secret, f"{timestamp}.".encode() + raw_body)``
hex-encoded — note the signed material is the *exact* timestamp header string,
a literal ``.``, then the raw request bytes. Binding the timestamp into the MAC
is what makes replay protection meaningful: an attacker can't reuse an old,
validly-signed body under a fresh timestamp.

Envelope shape (api_version 2026-03-01):
    {
      "event_id":   "...",          # idempotency anchor — dedup on this
      "event_type": "meeting.completed",
      "api_version":"2026-03-01",
      "created_at": "...",
      "data": {"meeting": {"platform": "...", "native_meeting_id": "..."}}
    }

``meeting.completed`` is the only default-on event; it means "recording done,
ready for us to run our pipeline" and is what lets the webhook finalize a
meeting instantly instead of waiting for the next scheduler tick.

Everything here is pure so the security-critical comparison and the defensive
field extraction are unit-testable without a live request.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

# How far the X-Webhook-Timestamp may drift from our clock before we treat the
# delivery as a replay. Generous enough for clock skew + delivery latency.
DEFAULT_MAX_SKEW_SECONDS = 300

EVENT_MEETING_COMPLETED = "meeting.completed"


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """The expected ``sha256=<hex>`` signature for a delivery.

    Signed material is ``f"{timestamp}.".encode() + raw_body`` — timestamp first
    (so it's authenticated), then a literal dot separator, then the raw bytes.
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def verify_signature(
    secret: str, timestamp: str | None, raw_body: bytes, signature_header: str | None
) -> bool:
    """Constant-time check that ``signature_header`` matches the computed HMAC.

    Fails closed: a missing secret, timestamp, or signature -> False. Uses
    ``hmac.compare_digest`` so the comparison time does not leak how many leading
    bytes matched.
    """
    if not secret or not timestamp or not signature_header:
        return False
    expected = compute_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature_header)


def is_fresh_timestamp(
    timestamp: str | None, now_epoch: float, *, max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS
) -> bool:
    """True if ``timestamp`` (unix seconds) is within ``max_skew_seconds`` of now.

    Rejects un-parseable or far-skewed timestamps (replay window). ``now_epoch``
    is injected so this stays pure/testable.
    """
    if timestamp is None:
        return False
    try:
        ts = float(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    return abs(now_epoch - ts) <= max_skew_seconds


@dataclass(frozen=True)
class VexaWebhookEvent:
    """The fields we act on, lifted out of the webhook envelope."""

    event_id: str
    event_type: str
    platform: str | None = None
    native_meeting_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_meeting_completed(self) -> bool:
        return self.event_type == EVENT_MEETING_COMPLETED


def _meeting_identity(meeting: dict) -> tuple[str | None, str | None]:
    """Pull (platform, native_meeting_id) out of the meeting object, defensively.

    Vexa's meeting object mirrors the /bots dispatch payload, but field names
    have drifted across versions, so accept the common aliases.
    """
    platform = meeting.get("platform") or meeting.get("platform_name")
    native_id = (
        meeting.get("native_meeting_id")
        or meeting.get("native_id")
        or meeting.get("meeting_id")
        or meeting.get("id")
    )
    return (
        str(platform) if platform is not None else None,
        str(native_id) if native_id is not None else None,
    )


def parse_webhook_event(body: dict) -> VexaWebhookEvent | None:
    """Parse the envelope into a ``VexaWebhookEvent``, or None if it's not one.

    Returns None when the payload has no ``event_type`` (not a recognizable Vexa
    webhook) so the endpoint can reject it without raising.
    """
    if not isinstance(body, dict):
        return None
    event_type = body.get("event_type")
    if not event_type:
        return None
    event_id = str(body.get("event_id") or "")
    data = body.get("data") or {}
    meeting = data.get("meeting") if isinstance(data, dict) else None
    platform, native_id = _meeting_identity(meeting) if isinstance(meeting, dict) else (None, None)
    return VexaWebhookEvent(
        event_id=event_id,
        event_type=str(event_type),
        platform=platform,
        native_meeting_id=native_id,
        raw=body,
    )
