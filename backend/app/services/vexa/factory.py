"""Provider selection. Today: cloud only. Later: switch by config/env."""
from __future__ import annotations

from app.services.vexa.cloud_provider import CloudVexaProvider
from app.services.vexa.provider import BotProvider


def get_provider() -> BotProvider:
    """Return the active BotProvider implementation."""
    return CloudVexaProvider()
