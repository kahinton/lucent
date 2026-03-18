"""Base protocol for platform integration adapters (Slack, Discord, etc.).

Each platform implements this protocol to handle inbound webhooks,
outbound messaging, and response formatting.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from starlette.requests import Request

from lucent.integrations.models import IntegrationEvent


@runtime_checkable
class IntegrationAdapter(Protocol):
    """Protocol that all platform adapters must implement.

    Adapters are stateless — platform config (tokens, signing secrets) is
    passed at construction time or loaded from the encrypted_config column.
    """

    @property
    def platform(self) -> str:
        """Return the platform identifier (e.g. 'slack', 'discord')."""
        ...

    async def verify_signature(self, request: Request) -> bool:
        """Verify the inbound webhook signature.

        Must be called *before* any payload parsing or user lookup.
        Returns True if the signature is valid, False otherwise.
        """
        ...

    async def parse_event(self, request: Request) -> IntegrationEvent:
        """Parse a verified webhook request into a normalized IntegrationEvent.

        Raises ValueError if the payload cannot be parsed.
        """
        ...

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Send a message to a channel, optionally in a thread.

        Returns the platform-specific message ID of the sent message.
        Raises IntegrationError on failure.
        """
        ...

    async def format_response(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Format a response string into the platform's native payload structure.

        If ephemeral=True, the response should only be visible to the
        requesting user (platform support varies).
        """
        ...


class IntegrationError(Exception):
    """Base exception for integration adapter failures."""

    def __init__(
        self,
        message: str,
        *,
        platform: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.platform = platform
        self.retryable = retryable
