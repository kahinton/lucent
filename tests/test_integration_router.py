"""Tests for lucent.integrations.router — FastAPI endpoints.

Tests admin CRUD, webhook, pairing, and link management endpoints using
mocked dependencies (pool, repos, encryptor, deps).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from lucent.integrations.models import (
    IntegrationStatus,
    IntegrationType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid4())


def _make_current_user(
    *,
    user_id: UUID | None = None,
    org_id: UUID | None = None,
    role: str = "admin",
) -> MagicMock:
    u = MagicMock()
    u.id = user_id or uuid4()
    u.organization_id = org_id or uuid4()
    u.role = role
    u.email = "test@example.com"
    u.display_name = "Test"
    return u


def _integration_row(
    *,
    integration_id: str | None = None,
    organization_id: str | None = None,
    type: str = "slack",
    status: str = "active",
) -> dict[str, Any]:
    org = UUID(organization_id) if organization_id else uuid4()
    return {
        "id": UUID(integration_id) if integration_id else uuid4(),
        "organization_id": org,
        "type": type,
        "status": status,
        "encrypted_config": b"enc",
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


def _link_row(
    *,
    link_id: str | None = None,
    organization_id: str | None = None,
    user_id: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": UUID(link_id) if link_id else uuid4(),
        "organization_id": UUID(organization_id) if organization_id else uuid4(),
        "integration_id": uuid4(),
        "user_id": UUID(user_id) if user_id else uuid4(),
        "provider": "slack",
        "external_user_id": "U_EXT_123",
        "external_workspace_id": None,
        "status": status,
        "verification_method": "pairing_code",
        "linked_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def _challenge_row(*, integration_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    return {
        "id": _uuid(),
        "integration_id": integration_id or _uuid(),
        "user_id": user_id or _uuid(),
        "code_hash": "$2b$12$fakehash",
        "status": "pending",
        "expires_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
    }


# ===========================================================================
# Webhook endpoint
# ===========================================================================


class TestWebhookEndpoint:
    """Tests for POST /webhook/{provider}."""

    @pytest.mark.asyncio
    async def test_slack_url_verification(self) -> None:
        """Slack URL verification challenge returns synchronously."""
        from lucent.integrations.router import receive_webhook

        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps({
            "type": "url_verification",
            "challenge": "test_challenge_123",
        }).encode())
        request.scope = {"headers": []}
        request.url = MagicMock()
        request.url.path = "/webhook/slack"

        bg = MagicMock()

        with patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool.return_value = MagicMock()
            result = await receive_webhook("slack", request, bg)

        assert result == {"challenge": "test_challenge_123"}

    @pytest.mark.asyncio
    async def test_non_slack_enqueues_background(self) -> None:
        """Non-Slack providers enqueue background task and return accepted."""
        from lucent.integrations.router import receive_webhook

        request = MagicMock()
        request.body = AsyncMock(return_value=b'{"test": true}')
        request.scope = {"headers": []}
        request.url = MagicMock()
        request.url.path = "/webhook/discord"

        bg = MagicMock()

        with patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool.return_value = MagicMock()
            result = await receive_webhook("discord", request, bg)

        assert result == {"status": "accepted"}
        bg.add_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_slack_non_verification_enqueues(self) -> None:
        """Slack events that aren't url_verification enqueue background."""
        from lucent.integrations.router import receive_webhook

        request = MagicMock()
        request.body = AsyncMock(return_value=json.dumps({
            "type": "event_callback",
            "event": {"type": "message"},
        }).encode())
        request.scope = {"headers": []}
        request.url = MagicMock()
        request.url.path = "/webhook/slack"

        bg = MagicMock()

        with patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool.return_value = MagicMock()
            result = await receive_webhook("slack", request, bg)

        assert result == {"status": "accepted"}
        bg.add_task.assert_called_once()


# ===========================================================================
# Admin CRUD — Create Integration
# ===========================================================================


class TestCreateIntegration:
    """Tests for POST /api/v1/integrations."""

    @pytest.mark.asyncio
    async def test_create_integration_success(self) -> None:
        from lucent.integrations.models import IntegrationCreate
        from lucent.integrations.router import create_integration

        user = _make_current_user()
        org_id = str(user.organization_id)

        body = IntegrationCreate(
            type=IntegrationType.SLACK,
            config={"bot_token": "xoxb-test", "signing_secret": "sec"},
        )

        row = _integration_row(organization_id=org_id)

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router._get_encryptor") as mock_enc,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_active_by_type = AsyncMock(return_value=None)
            repo.create = AsyncMock(return_value=row)
            mock_repo_cls.return_value = repo
            mock_enc.return_value = MagicMock(encrypt=MagicMock(return_value=b"enc"))

            result = await create_integration(body, user)

        assert result.type == "slack"
        assert result.status == "active"

    @pytest.mark.asyncio
    async def test_create_integration_conflict(self) -> None:
        """409 when active integration already exists."""
        from fastapi import HTTPException

        from lucent.integrations.models import IntegrationCreate
        from lucent.integrations.router import create_integration

        user = _make_current_user()
        body = IntegrationCreate(
            type=IntegrationType.SLACK,
            config={"bot_token": "t", "signing_secret": "s"},
        )

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router._get_encryptor") as mock_enc,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_active_by_type = AsyncMock(return_value=_integration_row())
            mock_repo_cls.return_value = repo
            mock_enc.return_value = MagicMock()

            with pytest.raises(HTTPException) as exc_info:
                await create_integration(body, user)
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_create_integration_no_org(self) -> None:
        """400 when user has no organization."""
        from fastapi import HTTPException

        from lucent.integrations.models import IntegrationCreate
        from lucent.integrations.router import create_integration

        user = _make_current_user()
        user.organization_id = None
        body = IntegrationCreate(
            type=IntegrationType.SLACK,
            config={"bot_token": "t", "signing_secret": "s"},
        )

        with pytest.raises(HTTPException) as exc_info:
            await create_integration(body, user)
        assert exc_info.value.status_code == 400


# ===========================================================================
# Admin CRUD — List / Get
# ===========================================================================


class TestListGetIntegrations:
    """Tests for GET endpoints."""

    @pytest.mark.asyncio
    async def test_list_integrations(self) -> None:
        from lucent.integrations.router import list_integrations

        user = _make_current_user()
        rows = [_integration_row(), _integration_row()]

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.list_by_org = AsyncMock(return_value=rows)
            mock_repo_cls.return_value = repo

            result = await list_integrations(user)

        assert result.total_count == 2
        assert len(result.integrations) == 2

    @pytest.mark.asyncio
    async def test_get_integration_found(self) -> None:
        from lucent.integrations.router import get_integration

        user = _make_current_user()
        int_id = uuid4()
        row = _integration_row(integration_id=str(int_id))

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=row)
            mock_repo_cls.return_value = repo

            result = await get_integration(int_id, user)

        assert str(result.id) == str(int_id)

    @pytest.mark.asyncio
    async def test_get_integration_not_found(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.router import get_integration

        user = _make_current_user()

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=None)
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await get_integration(uuid4(), user)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_list_integrations_no_org(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.router import list_integrations

        user = _make_current_user()
        user.organization_id = None

        with pytest.raises(HTTPException) as exc_info:
            await list_integrations(user)
        assert exc_info.value.status_code == 400


# ===========================================================================
# Admin CRUD — Update
# ===========================================================================


class TestUpdateIntegration:
    """Tests for PATCH /api/v1/integrations/{id}."""

    @pytest.mark.asyncio
    async def test_update_channels(self) -> None:
        from lucent.integrations.models import IntegrationUpdate
        from lucent.integrations.router import update_integration

        user = _make_current_user()
        int_id = uuid4()
        existing = _integration_row(integration_id=str(int_id))
        updated = dict(existing)
        updated["allowed_channels"] = ["C_NEW"]

        body = IntegrationUpdate(allowed_channels=["C_NEW"])

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=existing)
            repo.update = AsyncMock(return_value=updated)
            mock_repo_cls.return_value = repo

            result = await update_integration(int_id, body, user)

        assert result.allowed_channels == ["C_NEW"]

    @pytest.mark.asyncio
    async def test_update_not_found(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.models import IntegrationUpdate
        from lucent.integrations.router import update_integration

        user = _make_current_user()
        body = IntegrationUpdate(allowed_channels=["C1"])

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=None)
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await update_integration(uuid4(), body, user)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_status_disable(self) -> None:
        from lucent.integrations.models import IntegrationUpdate
        from lucent.integrations.router import update_integration

        user = _make_current_user()
        int_id = uuid4()
        existing = _integration_row(integration_id=str(int_id), status="active")
        disabled = dict(existing)
        disabled["status"] = "disabled"

        body = IntegrationUpdate(status=IntegrationStatus.DISABLED)

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.UserLinkRepo") as mock_link_cls,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=existing)
            repo.disable = AsyncMock(return_value=disabled)
            mock_repo_cls.return_value = repo

            link_repo = MagicMock()
            link_repo.bulk_orphan_by_integration = AsyncMock(return_value=2)
            mock_link_cls.return_value = link_repo

            result = await update_integration(int_id, body, user)

        assert result.status == "disabled"
        link_repo.bulk_orphan_by_integration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_status_invalid_transition(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.models import IntegrationUpdate
        from lucent.integrations.router import update_integration

        user = _make_current_user()
        int_id = uuid4()
        existing = _integration_row(integration_id=str(int_id), status="deleted")

        body = IntegrationUpdate(status=IntegrationStatus.ACTIVE)

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=existing)
            repo.activate = AsyncMock(return_value=None)  # Transition fails
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await update_integration(int_id, body, user)
            assert exc_info.value.status_code == 409


# ===========================================================================
# Admin CRUD — Delete
# ===========================================================================


class TestDeleteIntegration:
    """Tests for DELETE /api/v1/integrations/{id}."""

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        from lucent.integrations.router import delete_integration

        user = _make_current_user()
        int_id = uuid4()
        row = _integration_row(status="deleted")

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.UserLinkRepo") as mock_link_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.soft_delete = AsyncMock(return_value=row)
            mock_repo_cls.return_value = repo

            link_repo = MagicMock()
            link_repo.bulk_orphan_by_integration = AsyncMock(return_value=0)
            mock_link_cls.return_value = link_repo

            result = await delete_integration(int_id, user)

        assert result["status"] == "deleted"
        assert result["id"] == str(int_id)

    @pytest.mark.asyncio
    async def test_delete_not_found(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.router import delete_integration

        user = _make_current_user()

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.soft_delete = AsyncMock(return_value=None)
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await delete_integration(uuid4(), user)
            assert exc_info.value.status_code == 404


# ===========================================================================
# Pairing endpoints
# ===========================================================================


class TestPairingEndpoints:
    """Tests for pairing code generation and verification."""

    @pytest.mark.asyncio
    async def test_generate_pairing_code(self) -> None:
        from lucent.integrations.models import PairingChallengeCreate
        from lucent.integrations.router import generate_pairing_code

        user = _make_current_user(role="member")
        int_id = uuid4()
        body = PairingChallengeCreate(integration_id=int_id)

        integration = _integration_row(integration_id=str(int_id), status="active")
        challenge = _challenge_row(integration_id=str(int_id), user_id=str(user.id))

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.PairingChallengeRepo"),
            patch("lucent.integrations.identity.PairingChallengeService.generate", new_callable=AsyncMock) as mock_gen,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=integration)
            mock_repo_cls.return_value = repo

            mock_gen.return_value = (challenge, "secret-code-xyz")

            result = await generate_pairing_code(body, user)

        assert result.code == "secret-code-xyz"
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_generate_pairing_code_inactive_integration(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.models import PairingChallengeCreate
        from lucent.integrations.router import generate_pairing_code

        user = _make_current_user()
        body = PairingChallengeCreate(integration_id=uuid4())

        integration = _integration_row(status="disabled")

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.PairingChallengeRepo"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=integration)
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await generate_pairing_code(body, user)
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_generate_pairing_code_not_found(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.models import PairingChallengeCreate
        from lucent.integrations.router import generate_pairing_code

        user = _make_current_user()
        body = PairingChallengeCreate(integration_id=uuid4())

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.PairingChallengeRepo"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=None)
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await generate_pairing_code(body, user)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_generate_pairing_rate_limited(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.models import PairingChallengeCreate
        from lucent.integrations.router import generate_pairing_code

        user = _make_current_user()
        body = PairingChallengeCreate(integration_id=uuid4())

        integration = _integration_row(status="active")

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.PairingChallengeRepo"),
            patch("lucent.integrations.identity.PairingChallengeService.generate", new_callable=AsyncMock) as mock_gen,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=integration)
            mock_repo_cls.return_value = repo

            mock_gen.side_effect = ValueError("Rate limit exceeded")

            with pytest.raises(HTTPException) as exc_info:
                await generate_pairing_code(body, user)
            assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_verify_pairing_code_success(self) -> None:
        from lucent.integrations.router import verify_pairing_code

        user = _make_current_user()
        int_id = _uuid()
        integration = _integration_row(integration_id=int_id)

        body = {"code": "my-code", "integration_id": int_id}

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.IntegrationService") as mock_svc_cls,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=integration)
            mock_repo_cls.return_value = repo

            svc = MagicMock()
            svc.verify_link_code = AsyncMock(return_value=True)
            mock_svc_cls.return_value = svc

            result = await verify_pairing_code(body, user)

        assert result["linked"] is True

    @pytest.mark.asyncio
    async def test_verify_pairing_code_invalid(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.router import verify_pairing_code

        user = _make_current_user()
        int_id = _uuid()
        integration = _integration_row(integration_id=int_id)

        body = {"code": "wrong-code", "integration_id": int_id}

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.IntegrationRepo") as mock_repo_cls,
            patch("lucent.integrations.router.IntegrationService") as mock_svc_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.get_by_id = AsyncMock(return_value=integration)
            mock_repo_cls.return_value = repo

            svc = MagicMock()
            svc.verify_link_code = AsyncMock(return_value=False)
            mock_svc_cls.return_value = svc

            with pytest.raises(HTTPException) as exc_info:
                await verify_pairing_code(body, user)
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_missing_fields(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.router import verify_pairing_code

        user = _make_current_user()

        with pytest.raises(HTTPException) as exc_info:
            await verify_pairing_code({"code": "x"}, user)
        assert exc_info.value.status_code == 422


# ===========================================================================
# Admin link management
# ===========================================================================


class TestLinkManagement:
    """Tests for admin link CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_list_user_links(self) -> None:
        from lucent.integrations.router import list_user_links

        user = _make_current_user()
        rows = [_link_row(), _link_row()]

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.UserLinkRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.list_by_org = AsyncMock(return_value=rows)
            mock_repo_cls.return_value = repo

            result = await list_user_links(user)

        assert result.total_count == 2

    @pytest.mark.asyncio
    async def test_list_user_links_by_integration(self) -> None:
        from lucent.integrations.router import list_user_links

        user = _make_current_user()
        int_id = uuid4()
        rows = [_link_row()]

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.UserLinkRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.list_by_integration = AsyncMock(return_value=rows)
            mock_repo_cls.return_value = repo

            result = await list_user_links(user, integration_id=int_id)

        assert result.total_count == 1
        repo.list_by_integration.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_user_link(self) -> None:
        from lucent.integrations.models import UserLinkCreate
        from lucent.integrations.router import create_user_link

        user = _make_current_user()
        int_id = uuid4()
        target_user_id = uuid4()
        integration = _integration_row(integration_id=str(int_id))
        link = _link_row(status="active")

        body = UserLinkCreate(
            integration_id=int_id,
            user_id=target_user_id,
            external_user_id="U_SLACK_1",
        )

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.UserLinkRepo") as mock_link_cls,
            patch("lucent.integrations.router.IntegrationRepo") as mock_int_cls,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            int_repo = MagicMock()
            int_repo.get_by_id = AsyncMock(return_value=integration)
            mock_int_cls.return_value = int_repo

            link_repo = MagicMock()
            link_repo.create = AsyncMock(return_value=link)
            link_repo.activate = AsyncMock(return_value=link)
            mock_link_cls.return_value = link_repo

            result = await create_user_link(body, user)

        assert result.status == "active"
        link_repo.activate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_user_link_integration_not_found(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.models import UserLinkCreate
        from lucent.integrations.router import create_user_link

        user = _make_current_user()
        body = UserLinkCreate(
            integration_id=uuid4(),
            user_id=uuid4(),
            external_user_id="U_1",
        )

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.UserLinkRepo"),
            patch("lucent.integrations.router.IntegrationRepo") as mock_int_cls,
        ):
            mock_pool.return_value = MagicMock()
            int_repo = MagicMock()
            int_repo.get_by_id = AsyncMock(return_value=None)
            mock_int_cls.return_value = int_repo

            with pytest.raises(HTTPException) as exc_info:
                await create_user_link(body, user)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_revoke_user_link(self) -> None:
        from lucent.integrations.router import revoke_user_link

        user = _make_current_user()
        link_id = uuid4()

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.UserLinkRepo") as mock_repo_cls,
            patch("lucent.integrations.router.AuditRepository"),
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.revoke = AsyncMock(return_value=_link_row(status="revoked"))
            mock_repo_cls.return_value = repo

            result = await revoke_user_link(link_id, user)

        assert result["status"] == "revoked"

    @pytest.mark.asyncio
    async def test_revoke_user_link_not_found(self) -> None:
        from fastapi import HTTPException

        from lucent.integrations.router import revoke_user_link

        user = _make_current_user()

        with (
            patch("lucent.integrations.router.get_pool", new_callable=AsyncMock) as mock_pool,
            patch("lucent.integrations.router.UserLinkRepo") as mock_repo_cls,
        ):
            mock_pool.return_value = MagicMock()
            repo = MagicMock()
            repo.revoke = AsyncMock(return_value=None)
            mock_repo_cls.return_value = repo

            with pytest.raises(HTTPException) as exc_info:
                await revoke_user_link(uuid4(), user)
            assert exc_info.value.status_code == 404


# ===========================================================================
# Helper functions
# ===========================================================================


class TestRouterHelpers:
    """Tests for _get_encryptor, _integration_to_response, _link_to_response."""

    def test_integration_to_response(self) -> None:
        from lucent.integrations.router import _integration_to_response

        row = _integration_row()
        result = _integration_to_response(row)
        assert result.type == row["type"]
        assert result.status == row["status"]
        # Never exposes encrypted_config
        assert not hasattr(result, "encrypted_config") or getattr(result, "encrypted_config", None) is None

    def test_integration_to_response_json_channels(self) -> None:
        from lucent.integrations.router import _integration_to_response

        row = _integration_row()
        row["allowed_channels"] = '["C1","C2"]'
        result = _integration_to_response(row)
        assert result.allowed_channels == ["C1", "C2"]

    def test_link_to_response(self) -> None:
        from lucent.integrations.router import _link_to_response

        row = _link_row()
        result = _link_to_response(row)
        assert result.provider == "slack"
        assert result.status == "active"

    def test_get_encryptor_missing_key(self) -> None:
        from lucent.integrations.router import _get_encryptor

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("lucent.integrations.router.FernetEncryptor", side_effect=Exception("no key")),
        ):
            with pytest.raises(Exception):
                _get_encryptor()
