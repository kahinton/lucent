"""Database access layer for integrations subsystem.

Provides a unified IntegrationRepository that wraps the integration, user_link,
and pairing_challenge repositories with audit logging. Follows the project
pattern of DB modules in ``src/lucent/db/``.

For lower-level repo operations, see ``lucent.integrations.repositories``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.audit import AuditRepository
from lucent.integrations.models import IntegrationStatus, UserLinkStatus
from lucent.integrations.repositories import (
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)

logger = logging.getLogger(__name__)


class IntegrationRepository:
    """Unified database access for integrations, user links, and pairing challenges.

    Composes the three underlying repos and adds audit logging for
    mutation operations. All queries are org-scoped for tenant isolation.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self._integrations = IntegrationRepo(pool)
        self._user_links = UserLinkRepo(pool)
        self._challenges = PairingChallengeRepo(pool)
        self._audit = AuditRepository(pool)

    # -----------------------------------------------------------------------
    # Integrations CRUD
    # -----------------------------------------------------------------------

    async def create_integration(
        self,
        *,
        organization_id: str,
        type: str,
        encrypted_config: bytes,
        created_by: str,
        external_workspace_id: str | None = None,
        allowed_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new integration and log an audit event."""
        result = await self._integrations.create(
            organization_id=organization_id,
            type=type,
            encrypted_config=encrypted_config,
            created_by=created_by,
            external_workspace_id=external_workspace_id,
            allowed_channels=allowed_channels,
        )
        await self._audit_safe(
            "integration_created",
            UUID(organization_id),
            user_id=UUID(created_by),
            integration_id=result["id"],
            context={"type": type},
        )
        return result

    async def get_integration(
        self,
        integration_id: str,
        organization_id: str,
    ) -> dict[str, Any] | None:
        """Get a single integration by ID (org-scoped)."""
        return await self._integrations.get_by_id(integration_id, organization_id)

    async def get_active_integration(
        self,
        organization_id: str,
        platform: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the active integration for an org+platform+workspace."""
        return await self._integrations.get_active_by_type(
            organization_id, platform, external_workspace_id,
        )

    async def list_integrations(
        self,
        organization_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List integrations for an org."""
        return await self._integrations.list_by_org(
            organization_id, status=status, limit=limit,
        )

    async def update_integration(
        self,
        integration_id: str,
        organization_id: str,
        *,
        updated_by: str,
        allowed_channels: list[str] | None = None,
        encrypted_config: bytes | None = None,
    ) -> dict[str, Any] | None:
        """Update mutable fields on an integration and log audit."""
        result = await self._integrations.update(
            integration_id,
            organization_id,
            updated_by=updated_by,
            allowed_channels=allowed_channels,
            encrypted_config=encrypted_config,
        )
        if result:
            await self._audit_safe(
                "integration_updated",
                UUID(organization_id),
                user_id=UUID(updated_by),
                integration_id=result["id"],
            )
        return result

    async def disable_integration(
        self,
        integration_id: str,
        organization_id: str,
        *,
        updated_by: str,
    ) -> dict[str, Any] | None:
        """Disable an integration and log audit."""
        result = await self._integrations.disable(
            integration_id, organization_id, updated_by=updated_by,
        )
        if result:
            await self._audit_safe(
                "integration_disabled",
                UUID(organization_id),
                user_id=UUID(updated_by),
                integration_id=result["id"],
            )
        return result

    async def revoke_integration(
        self,
        integration_id: str,
        organization_id: str,
        *,
        updated_by: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Revoke an integration, orphan its links, and log audit."""
        result = await self._integrations.revoke(
            integration_id, organization_id,
            updated_by=updated_by, reason=reason,
        )
        if result:
            await self._user_links.bulk_orphan_by_integration(
                integration_id, organization_id,
            )
            await self._audit_safe(
                "integration_revoked",
                UUID(organization_id),
                user_id=UUID(updated_by),
                integration_id=result["id"],
                context={"reason": reason} if reason else None,
            )
        return result

    # -----------------------------------------------------------------------
    # User Links CRUD
    # -----------------------------------------------------------------------

    async def create_user_link(
        self,
        *,
        organization_id: str,
        integration_id: str,
        user_id: str,
        provider: str,
        external_user_id: str,
        verification_method: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new user link."""
        return await self._user_links.create(
            organization_id=organization_id,
            integration_id=integration_id,
            user_id=user_id,
            provider=provider,
            external_user_id=external_user_id,
            verification_method=verification_method,
            external_workspace_id=external_workspace_id,
        )

    async def get_user_link(
        self,
        link_id: str,
        organization_id: str,
    ) -> dict[str, Any] | None:
        """Get a single user link by ID (org-scoped)."""
        return await self._user_links.get_by_id(link_id, organization_id)

    async def resolve_user_link(
        self,
        provider: str,
        external_user_id: str,
        external_workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the active link for an external identity tuple."""
        return await self._user_links.resolve_identity(
            provider=provider,
            external_user_id=external_user_id,
            external_workspace_id=external_workspace_id,
        )

    async def list_user_links(
        self,
        organization_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List all user links for an org."""
        return await self._user_links.list_by_org(
            organization_id, status=status, limit=limit,
        )

    async def list_links_for_user(
        self,
        user_id: str,
        organization_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List links for a specific user (org-scoped)."""
        return await self._user_links.list_by_user(
            user_id, organization_id, status=status,
        )

    async def activate_user_link(
        self,
        link_id: str,
        organization_id: str,
    ) -> dict[str, Any] | None:
        """Activate a user link and log audit."""
        result = await self._user_links.activate(link_id, organization_id)
        if result:
            await self._audit_safe(
                "link_activated",
                UUID(organization_id),
                user_id=result.get("user_id"),
                integration_id=result.get("integration_id"),
            )
        return result

    async def revoke_user_link(
        self,
        link_id: str,
        organization_id: str,
        *,
        revoked_by: str | None = None,
    ) -> dict[str, Any] | None:
        """Revoke a user link and log audit."""
        result = await self._user_links.revoke(
            link_id, organization_id, revoked_by=revoked_by,
        )
        if result:
            await self._audit_safe(
                "link_revoked",
                UUID(organization_id),
                user_id=result.get("user_id"),
                integration_id=result.get("integration_id"),
                context={"revoked_by": revoked_by} if revoked_by else None,
            )
        return result

    # -----------------------------------------------------------------------
    # Pairing Challenges
    # -----------------------------------------------------------------------

    async def create_challenge(
        self,
        *,
        integration_id: str,
        user_id: str,
        code_hash: str,
        expires_at: datetime,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Create a pairing challenge."""
        return await self._challenges.create(
            integration_id=integration_id,
            user_id=user_id,
            code_hash=code_hash,
            expires_at=expires_at,
            max_attempts=max_attempts,
        )

    async def get_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        """Get a pairing challenge by ID."""
        return await self._challenges.get_by_id(challenge_id)

    # -----------------------------------------------------------------------
    # Audit helpers
    # -----------------------------------------------------------------------

    async def log_integration_event(
        self,
        event_type: str,
        organization_id: UUID,
        *,
        user_id: UUID | None = None,
        integration_id: UUID | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Log an integration audit event."""
        return await self._audit.log_integration_event(
            event_type=event_type,
            organization_id=organization_id,
            user_id=user_id,
            integration_id=integration_id,
            context=context,
            notes=notes,
        )

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    async def _audit_safe(
        self,
        event_type: str,
        organization_id: UUID,
        *,
        user_id: UUID | None = None,
        integration_id: UUID | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Log an audit event, swallowing errors."""
        try:
            int_id = (
                UUID(str(integration_id))
                if integration_id and not isinstance(integration_id, UUID)
                else integration_id
            )
            uid = (
                UUID(str(user_id))
                if user_id and not isinstance(user_id, UUID)
                else user_id
            )
            await self._audit.log_integration_event(
                event_type=event_type,
                organization_id=organization_id,
                user_id=uid,
                integration_id=int_id,
                context=context,
            )
        except Exception:
            logger.warning(
                "Failed to log integration audit event: %s", event_type,
                exc_info=True,
            )
