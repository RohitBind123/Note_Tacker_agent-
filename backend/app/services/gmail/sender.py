"""Gmail sender — sends the insights email AS the bot (centralagentai).

Uses the Gmail REST API (users.messages.send) with the bot's OAuth access token
(gmail.send scope). The message is a base64url-encoded MIME payload.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.services.google.token import get_access_token

log = get_logger(__name__)

_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class GmailSendError(RuntimeError):
    pass


def _build_raw(*, to: str, subject: str, html: str, sender: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content("This email contains meeting insights. View it in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def send_html_email(*, to: str, subject: str, html: str) -> str:
    """Send an HTML email; returns the Gmail message id."""
    if not to:
        raise GmailSendError("recipient address is empty")
    sender = settings.bot_google_email or "me"
    raw = _build_raw(to=to, subject=subject, html=html, sender=sender)

    token = await get_access_token()
    log.info("gmail_send_request", to=to, subject=subject)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            _SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"}
        )
    if resp.status_code != 200:
        log.error("gmail_send_failed", status=resp.status_code, body=resp.text[:300])
        raise GmailSendError(f"gmail send failed ({resp.status_code}): {resp.text[:200]}")
    message_id = resp.json().get("id", "")
    log.info("gmail_send_ok", to=to, message_id=message_id)
    return message_id
