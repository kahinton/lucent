"""IntegrationService — orchestrates the webhook-to-response pipeline.

Implements the full security pipeline from the integration design (Section 2):
channel allowlist → identity resolution → RBAC → rate limit → sanitize →
set_current_user → dispatch to MCP → format response → send via adapter.

All steps are audited. Errors produce graceful user-facing messages
without leaking internal details.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from asyncpg import Pool

from lucent.auth import set_current_user
from lucent.db.audit import (
    CHANNEL_NOT_ALLOWED,
    IDENTITY_RESOLVED,
    INTEGRATION_COMMAND,
    INTEGRATION_RATE_LIMITED,
    RESOLUTION_FAILED,
    AuditRepository,
)
from lucent.db.user import UserRepository
from lucent.integrations.base import IntegrationAdapter, IntegrationError
from lucent.integrations.identity import IdentityResolver, PairingChallengeService
from lucent.integrations.models import EventType, IntegrationEvent
from lucent.integrations.repositories import (
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)
from lucent.llm.mcp_bridge import MCPToolBridge
from lucent.rate_limit import RateLimiter, get_rate_limiter
from lucent.rbac import Permission, has_permission

logger = logging.getLogger(__name__)

# --- Constants ---

MAX_INPUT_LENGTH = 4000
MCP_DISPATCH_TIMEOUT = 30.0

# User-facing error messages — intentionally vague to avoid leaking internals.
_MSG_NOT_LINKED = (
    "Your account isn't linked to Lucent yet. "
    "Ask your admin for a pairing code, then DM me: `/lucent link <code>`"
)
_MSG_CHANNEL_NOT_ALLOWED = "This channel isn't configured for Lucent commands."
_MSG_RATE_LIMITED = "You're sending requests too quickly. Please wait a moment and try again."
_MSG_PERMISSION_DENIED = "You don't have permission to use Lucent from this integration."
_MSG_INTERNAL_ERROR = "Something went wrong processing your request. Please try again later."
_MSG_EMPTY_COMMAND = "Send a message or command and I'll help you out."

# Control characters to strip (keep \n, \t, \r)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class ServiceResult:
    """Result of processing an integration event through the pipeline."""

    success: bool
    response_text: str
    user_id: UUID | None = None
    organization_id: UUID | None = None
    error_stage: str | None = None


class IntegrationService:
    """Orchestrates the full integration event processing pipeline.

    Constructed with a database pool and optional overrides. Internally
    creates all required repositories and service dependencies.

    Pipeline steps (all audited):
        1. Channel allowlist check
        2. Identity resolution (external user → Lucent user)
        3. RBAC permission check
        4. Rate limit check (unified per-user key)
        5. Input sanitization
        6. set_current_user(ContextVar)
        7. Dispatch to MCP tool pipeline
        8. Format response via adapter
        9. Send response via adapter
    """

    def __init__(
        self,
        pool: Pool,
        *,
        rate_limiter: RateLimiter | None = None,
        mcp_url: str | None = None,
    ) -> None:
        self._pool = pool
        self._rate_limiter = rate_limiter or get_rate_limiter()
        self._mcp_url = mcp_url or os.environ.get(
            "LUCENT_MCP_URL", "http://localhost:8766/mcp"
        )

        # Construct repos from pool
        self._integration_repo = IntegrationRepo(pool)
        self._user_link_repo = UserLinkRepo(pool)
        self._challenge_repo = PairingChallengeRepo(pool)
        self._audit_repo = AuditRepository(pool)
        self._user_repo = UserRepository(pool)

        # Identity resolution sub-services
        challenge_svc = PairingChallengeService(self._challenge_repo)
        self._identity_resolver = IdentityResolver(
            self._user_link_repo, challenge_svc
        )

    # --- Public API: Identity & Config ---

    async def resolve_user(
        self,
        integration_id: str,
        external_user_id: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up the Lucent user linked to an external platform identity.

        Finds the active user_link matching the integration and external user,
        then loads the full user record.

        Returns:
            The user record dict, or None if no active link exists.
        """
        # Query user_links by integration + external_user_id
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM user_links
                WHERE integration_id = $1
                  AND external_user_id = $2
                  AND status = 'active'
                """,
                UUID(integration_id),
                external_user_id,
            )

        if row is None:
            return None

        user_id = row["user_id"]
        return await self._user_repo.get_by_id(
            user_id if isinstance(user_id, UUID) else UUID(str(user_id))
        )

    async def create_link_code(
        self,
        user_id: str,
        integration_id: str,
    ) -> str:
        """Generate a one-time pairing code for identity linking.

        Creates a high-entropy (128-bit) code with a short TTL.
        The code is bcrypt-hashed at rest; only the plaintext is returned.

        Args:
            user_id: The Lucent user ID to link.
            integration_id: The target integration.

        Returns:
            The plaintext pairing code (show once, never stored).

        Raises:
            ValueError: If the user has exceeded the pairing code rate limit.
        """
        challenge_svc = PairingChallengeService(self._challenge_repo)
        _challenge, plaintext_code = await challenge_svc.generate(
            integration_id=integration_id,
            user_id=user_id,
        )
        return plaintext_code

    async def verify_link_code(
        self,
        code: str,
        external_user_id: str,
        *,
        integration_id: str,
        organization_id: str,
        provider: str,
        external_workspace_id: str | None = None,
    ) -> bool:
        """Validate a pairing code and activate the identity link.

        Verifies the code against pending challenges for the integration,
        redeems it, creates the user link, and activates it.

        Args:
            code: The plaintext pairing code.
            external_user_id: The platform user ID claiming the code.
            integration_id: The integration the code was issued for.
            organization_id: The organization context.
            provider: The platform provider (slack/discord).
            external_workspace_id: Optional workspace/guild ID.

        Returns:
            True if the code was valid and the link was activated.
        """
        result = await self._identity_resolver.redeem_code(
            code=code,
            integration_id=integration_id,
            organization_id=organization_id,
            provider=provider,
            external_user_id=external_user_id,
            external_workspace_id=external_workspace_id,
        )
        return result.resolved

    async def get_integration(
        self,
        org_id: str,
        platform: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Load the active integration config for an org+platform.

        Args:
            org_id: The organization UUID.
            platform: The platform type (slack/discord).
            external_workspace_id: Optional workspace filter.

        Returns:
            The integration record dict, or None if not found.
        """
        return await self._integration_repo.get_active_by_type(
            org_id, platform, external_workspace_id,
        )

    # --- Public API: Event Pipeline ---

    async def handle_event(
        self,
        event: IntegrationEvent,
        integration: dict[str, Any],
        adapter: IntegrationAdapter,
    ) -> ServiceResult:
        """Process an integration event through the full pipeline.

        Args:
            event: Normalized event from ``adapter.parse_event()``.
            integration: The integration record (from IntegrationRepo).
            adapter: Platform adapter for formatting and sending responses.

        Returns:
            ServiceResult indicating success/failure and the response sent.
        """
        start_time = time.monotonic()
        org_id = UUID(str(integration["organization_id"]))
        integration_id = UUID(str(integration["id"]))

        # Skip non-actionable events silently
        if event.event_type in (EventType.URL_VERIFICATION, EventType.UNKNOWN):
            return ServiceResult(success=True, response_text="")

        try:
            return await self._run_pipeline(
                event, integration, adapter, org_id, integration_id, start_time
            )
        except IntegrationError:
            raise  # Platform-specific errors propagate to the route handler
        except Exception:
            logger.exception(
                "Integration pipeline error: platform=%s channel=%s",
                event.platform,
                event.channel_id,
            )
            await self._send_ephemeral_safe(adapter, event, _MSG_INTERNAL_ERROR)
            return ServiceResult(
                success=False,
                response_text=_MSG_INTERNAL_ERROR,
                error_stage="internal",
            )

    # --- Pipeline ---

    async def _run_pipeline(
        self,
        event: IntegrationEvent,
        integration: dict[str, Any],
        adapter: IntegrationAdapter,
        org_id: UUID,
        integration_id: UUID,
        start_time: float,
    ) -> ServiceResult:
        """Execute pipeline steps 1–9. Separated for exception handling."""

        # 1. Channel allowlist check
        result = await self._step_channel_allowlist(
            event, integration, adapter, org_id, integration_id
        )
        if result is not None:
            return result

        # 2. Identity resolution
        identity_result, result = await self._step_identity_resolution(
            event, adapter, org_id, integration_id
        )
        if result is not None:
            return result

        user_id = UUID(str(identity_result.user_id))
        user_org_id = UUID(str(identity_result.organization_id))

        # 3. RBAC permission check
        user, result = await self._step_rbac(
            event, adapter, user_id, user_org_id
        )
        if result is not None:
            return result

        # 4. Rate limit check
        result = await self._step_rate_limit(
            event, adapter, user_id, user_org_id, org_id, integration_id
        )
        if result is not None:
            return result

        # 5. Input sanitization
        sanitized_text = self._sanitize_input(event.text)
        if not sanitized_text:
            await self._send_ephemeral_safe(adapter, event, _MSG_EMPTY_COMMAND)
            return ServiceResult(
                success=True,
                response_text=_MSG_EMPTY_COMMAND,
                user_id=user_id,
                organization_id=user_org_id,
            )

        # 6. set_current_user → 7. dispatch → 8. format → 9. send
        set_current_user(user)
        try:
            return await self._step_dispatch_and_respond(
                event,
                adapter,
                integration,
                user,
                user_id,
                user_org_id,
                org_id,
                integration_id,
                sanitized_text,
                start_time,
            )
        finally:
            set_current_user(None)

    # --- Individual pipeline steps ---

    async def _step_channel_allowlist(
        self,
        event: IntegrationEvent,
        integration: dict[str, Any],
        adapter: IntegrationAdapter,
        org_id: UUID,
        integration_id: UUID,
    ) -> ServiceResult | None:
        """Step 1: Check channel is in the integration's allowlist."""
        allowed_channels: list[str] = integration.get("allowed_channels") or []
        # Empty allowlist → all channels permitted
        if allowed_channels and event.channel_id not in allowed_channels:
            await self._audit(
                CHANNEL_NOT_ALLOWED,
                org_id,
                integration_id=integration_id,
                context={
                    "channel_id": event.channel_id,
                    "external_user_id": event.external_user_id,
                    "platform": event.platform,
                },
            )
            await self._send_ephemeral_safe(
                adapter, event, _MSG_CHANNEL_NOT_ALLOWED
            )
            return ServiceResult(
                success=False,
                response_text=_MSG_CHANNEL_NOT_ALLOWED,
                error_stage="channel_allowlist",
            )
        return None

    async def _step_identity_resolution(
        self,
        event: IntegrationEvent,
        adapter: IntegrationAdapter,
        org_id: UUID,
        integration_id: UUID,
    ) -> tuple[Any, ServiceResult | None]:
        """Step 2: Resolve external identity to Lucent user."""
        identity = await self._identity_resolver.resolve(
            provider=event.platform,
            external_user_id=event.external_user_id,
            external_workspace_id=event.external_workspace_id,
        )
        if not identity.resolved:
            await self._audit(
                RESOLUTION_FAILED,
                org_id,
                integration_id=integration_id,
                context={
                    "external_user_id": event.external_user_id,
                    "platform": event.platform,
                    "channel_id": event.channel_id,
                },
            )
            await self._send_ephemeral_safe(adapter, event, _MSG_NOT_LINKED)
            return identity, ServiceResult(
                success=False,
                response_text=_MSG_NOT_LINKED,
                error_stage="identity_resolution",
            )

        await self._audit(
            IDENTITY_RESOLVED,
            org_id,
            user_id=UUID(str(identity.user_id)),
            integration_id=integration_id,
            context={
                "external_user_id": event.external_user_id,
                "platform": event.platform,
            },
        )
        return identity, None

    async def _step_rbac(
        self,
        event: IntegrationEvent,
        adapter: IntegrationAdapter,
        user_id: UUID,
        user_org_id: UUID,
    ) -> tuple[dict[str, Any] | None, ServiceResult | None]:
        """Step 3: Verify user is active and has basic permissions."""
        user = await self._user_repo.get_by_id(user_id)
        if not user or not user.get("is_active"):
            await self._send_ephemeral_safe(
                adapter, event, _MSG_PERMISSION_DENIED
            )
            return None, ServiceResult(
                success=False,
                response_text=_MSG_PERMISSION_DENIED,
                user_id=user_id,
                organization_id=user_org_id,
                error_stage="rbac",
            )

        role = user.get("role", "member")
        if not has_permission(role, Permission.MEMORY_CREATE):
            await self._send_ephemeral_safe(
                adapter, event, _MSG_PERMISSION_DENIED
            )
            return None, ServiceResult(
                success=False,
                response_text=_MSG_PERMISSION_DENIED,
                user_id=user_id,
                organization_id=user_org_id,
                error_stage="rbac",
            )

        return user, None

    async def _step_rate_limit(
        self,
        event: IntegrationEvent,
        adapter: IntegrationAdapter,
        user_id: UUID,
        user_org_id: UUID,
        org_id: UUID,
        integration_id: UUID,
    ) -> ServiceResult | None:
        """Step 4: Unified per-user rate limit check."""
        rate_result = self._rate_limiter.check_rate_limit(user_id)
        if not rate_result.allowed:
            await self._audit(
                INTEGRATION_RATE_LIMITED,
                org_id,
                user_id=user_id,
                integration_id=integration_id,
                context={
                    "platform": event.platform,
                    "channel_id": event.channel_id,
                },
            )
            await self._send_ephemeral_safe(adapter, event, _MSG_RATE_LIMITED)
            return ServiceResult(
                success=False,
                response_text=_MSG_RATE_LIMITED,
                user_id=user_id,
                organization_id=user_org_id,
                error_stage="rate_limit",
            )
        return None

    async def _step_dispatch_and_respond(
        self,
        event: IntegrationEvent,
        adapter: IntegrationAdapter,
        integration: dict[str, Any],
        user: dict[str, Any],
        user_id: UUID,
        user_org_id: UUID,
        org_id: UUID,
        integration_id: UUID,
        sanitized_text: str,
        start_time: float,
    ) -> ServiceResult:
        """Steps 6–9: Dispatch to MCP, format, and send response."""
        # 7. Dispatch to MCP tool pipeline
        mcp_result = await self._dispatch_to_mcp(sanitized_text, user)

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        # Audit the command execution
        await self._audit(
            INTEGRATION_COMMAND,
            org_id,
            user_id=user_id,
            integration_id=integration_id,
            context={
                "platform": event.platform,
                "channel_id": event.channel_id,
                "command_text": sanitized_text[:200],
                "elapsed_ms": elapsed_ms,
                "event_type": event.event_type.value,
            },
        )

        # 8. Format response via adapter
        formatted = await adapter.format_response(mcp_result)

        # 9. Send via adapter (use formatted blocks if available)
        await adapter.send_message(
            event.channel_id,
            mcp_result,
            thread_id=event.thread_id,
            metadata=formatted if isinstance(formatted, dict) else None,
        )

        logger.info(
            "Integration command completed: platform=%s user=%s elapsed_ms=%d",
            event.platform,
            user_id,
            elapsed_ms,
        )

        return ServiceResult(
            success=True,
            response_text=mcp_result,
            user_id=user_id,
            organization_id=user_org_id,
        )

    # --- Helpers ---

    @staticmethod
    def _sanitize_input(text: str) -> str:
        """Sanitize user input from integration channels.

        - Truncates to MAX_INPUT_LENGTH
        - Strips null bytes and non-printable control characters
        - Preserves newlines, tabs, and carriage returns
        """
        if not text:
            return ""
        text = text[:MAX_INPUT_LENGTH]
        text = _CONTROL_CHAR_RE.sub("", text)
        return text.strip()

    async def _dispatch_to_mcp(
        self,
        text: str,
        user: dict[str, Any],
    ) -> str:
        """Dispatch sanitized text to the MCP tool pipeline.

        Phase 1 implementation: routes text to ``search_memories``.
        Future phases will use a full LLM-backed conversation loop.
        """
        bridge = MCPToolBridge(
            mcp_url=self._mcp_url,
            headers={
                "X-User-Id": str(user["id"]),
                "X-Organization-Id": str(user["organization_id"]),
            },
            skip_url_validation=True,  # Internal URL, not user-controllable
        )
        try:
            result = await bridge.call_tool(
                "search_memories",
                {"query": text, "limit": 5},
            )
            return result or "No results found."
        except Exception:
            logger.exception("MCP dispatch failed for text: %.100s", text)
            return _MSG_INTERNAL_ERROR
        finally:
            await bridge.close()

    async def _send_ephemeral_safe(
        self,
        adapter: IntegrationAdapter,
        event: IntegrationEvent,
        message: str,
    ) -> None:
        """Send an ephemeral response, swallowing errors."""
        try:
            await adapter.send_message(
                event.channel_id,
                message,
                thread_id=event.thread_id,
                metadata={"ephemeral": True, "user": event.external_user_id},
            )
        except Exception:
            logger.warning(
                "Failed to send ephemeral message: platform=%s channel=%s",
                event.platform,
                event.channel_id,
            )

    async def _audit(
        self,
        event_type: str,
        organization_id: UUID,
        *,
        user_id: UUID | None = None,
        integration_id: UUID | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> None:
        """Log an audit event. Errors are swallowed to avoid breaking the pipeline."""
        try:
            await self._audit_repo.log_integration_event(
                event_type=event_type,
                organization_id=organization_id,
                user_id=user_id,
                integration_id=integration_id,
                context=context,
                notes=notes,
            )
        except Exception:
            logger.warning(
                "Failed to log audit event: %s", event_type, exc_info=True
            )
