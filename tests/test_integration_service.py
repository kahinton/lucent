"""Tests for IntegrationService — resolve_user, create_link_code,
verify_link_code, get_integration methods.

Uses mocked repos and pool to test the service layer in isolation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from lucent.integrations.identity import (
    IdentityResolver,
    IdentityResult,
    PairingChallengeService,
)
from lucent.integrations.models import UserLinkStatus, VerificationMethod
from lucent.integrations.service import IntegrationService


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_user(
    *,
    user_id: str | None = None,
    organization_id: str | None = None,
    is_active: bool = True,
    role: str = "member",
) -> dict[str, Any]:
    return {
        "id": user_id or str(uuid4()),
        "external_id": "ext_123",
        "provider": "local",
        "organization_id": UUID(organization_id) if organization_id else uuid4(),
        "email": "test@example.com",
        "display_name": "Test User",
        "avatar_url": None,
        "provider_metadata": {},
        "is_active": is_active,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_login_at": None,
        "role": role,
    }


def _make_link(
    *,
    link_id: str | None = None,
    user_id: str | None = None,
    integration_id: str | None = None,
    organization_id: str | None = None,
    provider: str = "slack",
    external_user_id: str = "U_EXT_123",
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": link_id or str(uuid4()),
        "user_id": user_id or str(uuid4()),
        "integration_id": integration_id or str(uuid4()),
        "organization_id": organization_id or str(uuid4()),
        "provider": provider,
        "external_user_id": external_user_id,
        "external_workspace_id": None,
        "status": status,
        "verification_method": VerificationMethod.PAIRING_CODE.value,
        "linked_at": datetime.now(timezone.utc),
    }


def _make_integration(
    *,
    integration_id: str | None = None,
    organization_id: str | None = None,
    type: str = "slack",
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": UUID(integration_id) if integration_id else uuid4(),
        "organization_id": UUID(organization_id) if organization_id else uuid4(),
        "type": type,
        "status": status,
        "encrypted_config": b"encrypted",
        "config_version": 1,
        "external_workspace_id": None,
        "allowed_channels": [],
        "created_by": uuid4(),
        "updated_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "disabled_at": None,
        "revoked_at": None,
        "revoke_reason": None,
    }


class _FakeRecord(dict):
    """Minimal asyncpg.Record standin for pool query results."""

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


def _make_pool(link_row: dict | None = None) -> MagicMock:
    """Create a mock asyncpg Pool that returns link_row for fetchrow."""
    pool = MagicMock()
    conn = AsyncMock()

    record = _FakeRecord(link_row) if link_row else None
    conn.fetchrow = AsyncMock(return_value=record)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


def _make_service(pool: MagicMock | None = None) -> IntegrationService:
    """Build an IntegrationService with all dependencies mocked."""
    if pool is None:
        pool = _make_pool()

    # Patch external dependencies that IntegrationService.__init__ imports
    with (
        patch("lucent.integrations.service.get_rate_limiter") as mock_rl,
        patch("lucent.integrations.service.UserRepository") as mock_user_cls,
        patch("lucent.integrations.service.AuditRepository"),
        patch("lucent.integrations.service.IntegrationRepo") as mock_int_cls,
        patch("lucent.integrations.service.UserLinkRepo") as mock_link_cls,
        patch("lucent.integrations.service.PairingChallengeRepo") as mock_ch_cls,
        patch("lucent.integrations.service.IdentityResolver") as mock_resolver_cls,
        patch("lucent.integrations.service.PairingChallengeService") as mock_pcs_cls,
    ):
        mock_rl.return_value = MagicMock()
        svc = IntegrationService(pool)

    return svc


# ===========================================================================
# resolve_user
# ===========================================================================


class TestResolveUser:
    """Tests for IntegrationService.resolve_user."""

    async def test_resolve_user_active_link(self):
        """Returns the Lucent user when an active link exists."""
        user_id = str(uuid4())
        integration_id = str(uuid4())
        user = _make_user(user_id=user_id)

        link_row = {
            "user_id": UUID(user_id),
            "status": "active",
        }
        pool = _make_pool(link_row)
        svc = _make_service(pool)
        svc._user_repo = MagicMock()
        svc._user_repo.get_by_id = AsyncMock(return_value=user)

        result = await svc.resolve_user(integration_id, "U_SLACK_123")
        assert result is not None
        assert result["id"] == user_id
        svc._user_repo.get_by_id.assert_awaited_once_with(UUID(user_id))

    async def test_resolve_user_no_link(self):
        """Returns None when no active link exists."""
        pool = _make_pool(None)
        svc = _make_service(pool)

        result = await svc.resolve_user(str(uuid4()), "U_UNKNOWN")
        assert result is None

    async def test_resolve_user_inactive_user(self):
        """Returns None (via user repo) when the linked user is inactive."""
        user_id = str(uuid4())
        link_row = {"user_id": UUID(user_id), "status": "active"}
        pool = _make_pool(link_row)
        svc = _make_service(pool)
        svc._user_repo = MagicMock()
        svc._user_repo.get_by_id = AsyncMock(return_value=None)

        result = await svc.resolve_user(str(uuid4()), "U_SLACK_123")
        assert result is None


# ===========================================================================
# create_link_code
# ===========================================================================


class TestCreateLinkCode:
    """Tests for IntegrationService.create_link_code."""

    async def test_create_link_code_returns_plaintext(self):
        """Returns a plaintext pairing code string."""
        svc = _make_service()
        challenge = {"id": str(uuid4()), "expires_at": datetime.now(timezone.utc)}
        plaintext = "test-code-abc123"

        svc._challenge_repo = MagicMock()
        with patch(
            "lucent.integrations.service.PairingChallengeService"
        ) as mock_cls:
            mock_pcs = MagicMock()
            mock_pcs.generate = AsyncMock(return_value=(challenge, plaintext))
            mock_cls.return_value = mock_pcs

            result = await svc.create_link_code(str(uuid4()), str(uuid4()))
            assert result == plaintext
            mock_pcs.generate.assert_awaited_once()

    async def test_create_link_code_rate_limit_propagates(self):
        """ValueError from rate-limited code generation propagates."""
        svc = _make_service()
        svc._challenge_repo = MagicMock()

        with patch(
            "lucent.integrations.service.PairingChallengeService"
        ) as mock_cls:
            mock_pcs = MagicMock()
            mock_pcs.generate = AsyncMock(
                side_effect=ValueError("Rate limit exceeded")
            )
            mock_cls.return_value = mock_pcs

            with pytest.raises(ValueError, match="Rate limit"):
                await svc.create_link_code(str(uuid4()), str(uuid4()))


# ===========================================================================
# verify_link_code
# ===========================================================================


class TestVerifyLinkCode:
    """Tests for IntegrationService.verify_link_code."""

    async def test_verify_valid_code(self):
        """Returns True when code is valid and link is activated."""
        svc = _make_service()
        svc._identity_resolver = MagicMock()
        svc._identity_resolver.redeem_code = AsyncMock(
            return_value=IdentityResult(
                resolved=True,
                user_id=str(uuid4()),
                organization_id=str(uuid4()),
            )
        )

        result = await svc.verify_link_code(
            "valid-code",
            "U_SLACK_123",
            integration_id=str(uuid4()),
            organization_id=str(uuid4()),
            provider="slack",
        )
        assert result is True
        svc._identity_resolver.redeem_code.assert_awaited_once()

    async def test_verify_invalid_code(self):
        """Returns False when code verification fails."""
        svc = _make_service()
        svc._identity_resolver = MagicMock()
        svc._identity_resolver.redeem_code = AsyncMock(
            return_value=IdentityResult(resolved=False)
        )

        result = await svc.verify_link_code(
            "wrong-code",
            "U_SLACK_123",
            integration_id=str(uuid4()),
            organization_id=str(uuid4()),
            provider="slack",
        )
        assert result is False

    async def test_verify_passes_workspace_id(self):
        """external_workspace_id is forwarded to redeem_code."""
        svc = _make_service()
        svc._identity_resolver = MagicMock()
        svc._identity_resolver.redeem_code = AsyncMock(
            return_value=IdentityResult(resolved=True, user_id=str(uuid4()))
        )

        workspace_id = "W_12345"
        await svc.verify_link_code(
            "code",
            "U_SLACK",
            integration_id=str(uuid4()),
            organization_id=str(uuid4()),
            provider="slack",
            external_workspace_id=workspace_id,
        )

        call_kwargs = svc._identity_resolver.redeem_code.call_args.kwargs
        assert call_kwargs["external_workspace_id"] == workspace_id


# ===========================================================================
# get_integration
# ===========================================================================


class TestGetIntegration:
    """Tests for IntegrationService.get_integration."""

    async def test_get_active_integration(self):
        """Returns the active integration for org+platform."""
        org_id = str(uuid4())
        integration = _make_integration(organization_id=org_id)
        svc = _make_service()
        svc._integration_repo = MagicMock()
        svc._integration_repo.get_active_by_type = AsyncMock(
            return_value=integration
        )

        result = await svc.get_integration(org_id, "slack")
        assert result is not None
        assert result["type"] == "slack"
        assert result["status"] == "active"
        svc._integration_repo.get_active_by_type.assert_awaited_once_with(
            org_id, "slack", None,
        )

    async def test_get_integration_not_found(self):
        """Returns None when no active integration exists."""
        svc = _make_service()
        svc._integration_repo = MagicMock()
        svc._integration_repo.get_active_by_type = AsyncMock(return_value=None)

        result = await svc.get_integration(str(uuid4()), "discord")
        assert result is None

    async def test_get_integration_with_workspace(self):
        """Passes external_workspace_id through to the repo."""
        svc = _make_service()
        svc._integration_repo = MagicMock()
        svc._integration_repo.get_active_by_type = AsyncMock(
            return_value=_make_integration()
        )

        workspace = "T_WORKSPACE"
        await svc.get_integration(str(uuid4()), "slack", workspace)

        call_args = svc._integration_repo.get_active_by_type.call_args
        assert call_args[0][2] == workspace


# ===========================================================================
# DB access layer — IntegrationRepository
# ===========================================================================


class TestIntegrationRepository:
    """Basic tests for src/lucent/db/integrations.IntegrationRepository."""

    async def test_construction(self):
        """IntegrationRepository initializes sub-repos from pool."""
        from lucent.db.integrations import IntegrationRepository

        pool = MagicMock()
        repo = IntegrationRepository(pool)
        assert repo.pool is pool
        assert repo._integrations is not None
        assert repo._user_links is not None
        assert repo._challenges is not None
        assert repo._audit is not None

    async def test_get_active_integration_delegates(self):
        """get_active_integration delegates to IntegrationRepo."""
        from lucent.db.integrations import IntegrationRepository

        pool = MagicMock()
        repo = IntegrationRepository(pool)
        repo._integrations.get_active_by_type = AsyncMock(
            return_value=_make_integration()
        )

        result = await repo.get_active_integration(str(uuid4()), "slack")
        assert result is not None
        repo._integrations.get_active_by_type.assert_awaited_once()

    async def test_resolve_user_link_delegates(self):
        """resolve_user_link delegates to UserLinkRepo."""
        from lucent.db.integrations import IntegrationRepository

        pool = MagicMock()
        repo = IntegrationRepository(pool)
        repo._user_links.resolve_identity = AsyncMock(return_value=_make_link())

        result = await repo.resolve_user_link("slack", "U_EXT_123")
        assert result is not None
        repo._user_links.resolve_identity.assert_awaited_once()

    async def test_audit_safe_swallows_errors(self):
        """_audit_safe doesn't raise even if audit logging fails."""
        from lucent.db.integrations import IntegrationRepository

        pool = MagicMock()
        repo = IntegrationRepository(pool)
        repo._audit.log_integration_event = AsyncMock(
            side_effect=Exception("DB down")
        )

        # Should not raise
        await repo._audit_safe(
            "integration_created",
            uuid4(),
            user_id=uuid4(),
        )

    async def test_create_integration_audits(self):
        """create_integration calls the integration repo and logs audit."""
        from lucent.db.integrations import IntegrationRepository

        pool = MagicMock()
        repo = IntegrationRepository(pool)

        org_id = str(uuid4())
        created_by = str(uuid4())
        integration = _make_integration(organization_id=org_id)
        repo._integrations.create = AsyncMock(return_value=integration)
        repo._audit.log_integration_event = AsyncMock()

        result = await repo.create_integration(
            organization_id=org_id,
            type="slack",
            encrypted_config=b"enc",
            created_by=created_by,
        )
        assert result == integration
        repo._integrations.create.assert_awaited_once()
        repo._audit.log_integration_event.assert_awaited_once()
