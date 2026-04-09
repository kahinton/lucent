"""Provider abstraction layer — registry, response model, and platform stubs.

The core types (IntegrationEvent, IntegrationAdapter) live in their canonical
modules (models.py, base.py). This module adds:

- ``AdapterResponse`` — normalized response dataclass for adapter output
- ``DiscordAdapter`` — stub (NotImplementedError) for future Discord support
- ``AdapterRegistry`` — register and look up adapters by platform name
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

from lucent.integrations.base import IntegrationAdapter, IntegrationError
from lucent.integrations.models import IntegrationEvent

# Re-export core types for convenience
__all__ = [
    "AdapterResponse",
    "AdapterRegistry",
    "DiscordAdapter",
    "IntegrationAdapter",
    "IntegrationEvent",
    "IntegrationError",
]


# ---------------------------------------------------------------------------
# Normalized response dataclass
# ---------------------------------------------------------------------------


@dataclass
class AdapterResponse:
    """Normalized response to send back to any platform.

    Adapters translate this into platform-specific payloads via
    ``IntegrationAdapter.format_response()``.  Distinct from the Pydantic
    ``IntegrationResponse`` model used for REST API responses.
    """

    text: str
    thread_id: str | None = None
    ephemeral: bool = False
    blocks: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Discord stub
# ---------------------------------------------------------------------------


class DiscordAdapter:
    """Stub Discord adapter — not yet implemented.

    Will implement the ``IntegrationAdapter`` protocol for Discord using
    Ed25519 signature verification (``X-Signature-Ed25519`` header) and
    the Discord REST API.
    """

    @property
    def platform(self) -> str:
        return "discord"

    async def verify_signature(self, request: Request) -> bool:
        raise NotImplementedError("Discord adapter not yet implemented")

    async def parse_event(self, request: Request) -> IntegrationEvent:
        raise NotImplementedError("Discord adapter not yet implemented")

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        raise NotImplementedError("Discord adapter not yet implemented")

    async def format_response(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError("Discord adapter not yet implemented")


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Registry for looking up platform adapters by name.

    Typical usage::

        registry = AdapterRegistry()
        registry.register(SlackAdapter(...))
        adapter = registry.get_or_raise("slack")
    """

    def __init__(self) -> None:
        self._adapters: dict[str, IntegrationAdapter] = {}

    def register(self, adapter: IntegrationAdapter) -> None:
        """Register an adapter, keyed by its ``platform`` property."""
        self._adapters[adapter.platform] = adapter

    def get(self, platform: str) -> IntegrationAdapter | None:
        """Look up an adapter by platform name, or ``None`` if not found."""
        return self._adapters.get(platform)

    def get_or_raise(self, platform: str) -> IntegrationAdapter:
        """Look up an adapter, raising ``IntegrationError`` if not found."""
        adapter = self._adapters.get(platform)
        if adapter is None:
            raise IntegrationError(
                f"No adapter registered for platform: {platform}",
                platform=platform,
            )
        return adapter

    def unregister(self, platform: str) -> bool:
        """Remove an adapter. Returns ``True`` if it was present."""
        return self._adapters.pop(platform, None) is not None

    @property
    def platforms(self) -> list[str]:
        """Return list of registered platform names."""
        return list(self._adapters.keys())

    def __contains__(self, platform: str) -> bool:
        return platform in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)
