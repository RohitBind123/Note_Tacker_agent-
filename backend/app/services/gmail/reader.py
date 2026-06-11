"""Async read-only Gmail API client (gmail.readonly scope).

Used by the Gmail invite scanner to list and fetch candidate invite messages.
Mirrors the structure of services/google/calendar.py — same token provider,
same retry helper, same log conventions.

Requires the refresh token to have been granted the gmail.readonly scope.
A 403 insufficientPermissions response means the scope is missing; see
docs/CHALLENGES.md §Gmail scanner rollout for the re-consent procedure.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import httpx

from app.logging_config import get_logger
from app.services.google.token import get_access_token
from app.services.http import request_with_retries

log = get_logger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass
class GmailMessage:
    message_id: str
    subject: str
    from_addr: str
    body_text: str
    internal_date_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)


class GmailReadError(RuntimeError):
    pass


class GmailReader:
    """Read-only access to the bot's Gmail inbox."""

    async def _auth_headers(self) -> dict[str, str]:
        token = await get_access_token()
        return {"Authorization": f"Bearer {token}"}

    async def list_message_ids(
        self,
        query: str,
        *,
        max_results: int = 25,
    ) -> list[str]:
        """Return Gmail message IDs matching ``query``, capped at ``max_results``.

        Uses Gmail's search ``q`` parameter (same syntax as the Gmail search box).
        Paginates via nextPageToken but stops once ``max_results`` is reached so
        we never buffer an unbounded list.
        """
        headers = await self._auth_headers()
        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < max_results:
            params: dict[str, str | int] = {
                "q": query,
                "maxResults": min(max_results - len(ids), 100),
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await request_with_retries(
                "GET",
                f"{_GMAIL_BASE}/messages",
                headers=headers,
                params=params,
                timeout=_TIMEOUT,
            )
            if resp.status_code == 403:
                log.error(
                    "gmail_read_forbidden",
                    hint="refresh token missing gmail.readonly scope",
                    body=resp.text[:300],
                )
                raise GmailReadError("gmail.readonly scope not granted")
            if resp.status_code != 200:
                log.error("gmail_list_failed", status=resp.status_code, body=resp.text[:300])
                resp.raise_for_status()

            data = resp.json()
            for msg in data.get("messages", []):
                ids.append(msg["id"])
                if len(ids) >= max_results:
                    break

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        log.info("gmail_listed_ids", count=len(ids), query=query[:80])
        return ids

    async def get_message(self, message_id: str) -> GmailMessage:
        """Fetch a full message and decode the plaintext body."""
        headers = await self._auth_headers()
        resp = await request_with_retries(
            "GET",
            f"{_GMAIL_BASE}/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            raise GmailReadError(f"message {message_id} not found")
        if resp.status_code != 200:
            log.error("gmail_get_failed", message_id=message_id, status=resp.status_code)
            resp.raise_for_status()

        data = resp.json()
        raw_headers = {
            h["name"]: h["value"]
            for h in (data.get("payload") or {}).get("headers", [])
        }
        body_text = _decode_body(data.get("payload") or {})

        return GmailMessage(
            message_id=message_id,
            subject=raw_headers.get("Subject", ""),
            from_addr=raw_headers.get("From", ""),
            body_text=body_text,
            internal_date_ms=int(data.get("internalDate", 0)),
            headers=raw_headers,
        )


# --- Body decoding ------------------------------------------------------------


def _decode_body(payload: dict) -> str:
    """Walk a Gmail message payload and return decoded plaintext (prefer text/plain).

    Collects all text/plain and text/html leaf parts across every nesting level
    before applying the preference, so a text/plain inside a nested
    multipart/alternative is always chosen over a text/html at the outer level.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_leaf_parts(payload, plain_parts, html_parts)
    return "".join(plain_parts) or "".join(html_parts)


def _collect_leaf_parts(node: dict, plain_acc: list[str], html_acc: list[str]) -> None:
    """Recursively collect decoded text/plain and text/html from a message payload."""
    parts = node.get("parts")
    if not parts:
        # Leaf: decode body data directly.
        data = (node.get("body") or {}).get("data", "")
        if data:
            mime = node.get("mimeType", "")
            if mime == "text/plain":
                plain_acc.append(_b64_decode(data))
            elif mime == "text/html":
                html_acc.append(_b64_decode(data))
        return
    for part in parts:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain_acc.append(_b64_decode((part.get("body") or {}).get("data", "")))
        elif mime == "text/html":
            html_acc.append(_b64_decode((part.get("body") or {}).get("data", "")))
        elif mime.startswith("multipart/"):
            _collect_leaf_parts(part, plain_acc, html_acc)


def _b64_decode(data: str) -> str:
    """Decode a base64url-encoded string to UTF-8 text, ignoring bad bytes."""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""
