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


# ===========================================================================
# handle_event pipeline tests
# ===========================================================================


def _make_event(
    *,
    event_type: str = "message",
    platform: str = "slack",
    external_user_id: str = "U_SLACK_123",
    channel_id: str = "C_GENERAL",
    text: str = "hello lucent",
    thread_id: str | None = None,
    external_workspace_id: str | None = None,
) -> "IntegrationEvent":
    from lucent.integrations.models import EventType, IntegrationEvent

    et = EventType(event_type) if isinstance(event_type, str) else event_type
    return IntegrationEvent(
        event_type=et,
        platform=platform,
        external_user_id=external_user_id,
        channel_id=channel_id,
        text=text,
        thread_id=thread_id,
        external_workspace_id=external_workspace_id,
    )


class _MockAdapter:
    """Mock adapter for pipeline tests."""

    def __init__(self) -> None:
        self.platform = "slack"
        self.verify_signature = AsyncMock(return_value=True)
        self.parse_event = AsyncMock()
        self.send_message = AsyncMock(return_value="msg_ts_123")
        self.format_response = AsyncMock(return_value={"text": "response"})


def _make_pipeline_service(
    *,
    user: dict | None = None,
    link: dict | None = None,
    rate_allowed: bool = True,
    mcp_result: str = "Search results here",
) -> tuple["IntegrationService", MagicMock]:
    """Build an IntegrationService with pipeline dependencies mocked."""
    from lucent.integrations.service import IntegrationService

    pool = _make_pool()

    with (
        patch("lucent.integrations.service.get_rate_limiter") as mock_rl,
        patch("lucent.integrations.service.UserRepository") as mock_user_cls,
        patch("lucent.integrations.service.AuditRepository") as mock_audit_cls,
        patch("lucent.integrations.service.IntegrationRepo"),
        patch("lucent.integrations.service.UserLinkRepo"),
        patch("lucent.integrations.service.PairingChallengeRepo"),
        patch("lucent.integrations.service.PairingChallengeService"),
        patch("lucent.integrations.service.IdentityResolver"),
    ):
        rl = MagicMock()
        rl_result = MagicMock()
        rl_result.allowed = rate_allowed
        rl_result.headers = {}
        rl.check_rate_limit = MagicMock(return_value=rl_result)
        mock_rl.return_value = rl

        svc = IntegrationService(pool)

    # Set up identity resolver
    if link is not None:
        identity = IdentityResult(
            resolved=True,
            user_id=link.get("user_id", str(uuid4())),
            organization_id=link.get("organization_id", str(uuid4())),
            link=link,
        )
    else:
        identity = IdentityResult(resolved=False)

    svc._identity_resolver = MagicMock()
    svc._identity_resolver.resolve = AsyncMock(return_value=identity)

    # Set up user repo
    svc._user_repo = MagicMock()
    svc._user_repo.get_by_id = AsyncMock(return_value=user)

    # Set up rate limiter
    svc._rate_limiter = MagicMock()
    rl_result = MagicMock()
    rl_result.allowed = rate_allowed
    svc._rate_limiter.check_rate_limit = MagicMock(return_value=rl_result)

    # Set up audit
    svc._audit_repo = MagicMock()
    svc._audit_repo.log_integration_event = AsyncMock()

    # Mock MCP dispatch
    svc._dispatch_to_mcp = AsyncMock(return_value=mcp_result)

    return svc, pool


class TestHandleEventSkipNonActionable:
    """handle_event skips URL verification and unknown events."""

    async def test_url_verification_skipped(self):
        svc, _ = _make_pipeline_service()
        event = _make_event(event_type="url_verification")
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True
        assert result.response_text == ""

    async def test_unknown_event_skipped(self):
        svc, _ = _make_pipeline_service()
        event = _make_event(event_type="unknown")
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True
        assert result.response_text == ""


class TestHandleEventChannelAllowlist:
    """Step 1: Channel allowlist check."""

    async def test_allowed_channel_passes(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        event = _make_event(channel_id="C_ALLOWED")
        integration = _make_integration()
        integration["allowed_channels"] = ["C_ALLOWED"]
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True

    async def test_blocked_channel_fails(self):
        svc, _ = _make_pipeline_service()
        event = _make_event(channel_id="C_BLOCKED")
        integration = _make_integration()
        integration["allowed_channels"] = ["C_ALLOWED_ONLY"]
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False
        assert result.error_stage == "channel_allowlist"

    async def test_empty_allowlist_permits_all(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        event = _make_event(channel_id="C_ANY")
        integration = _make_integration()
        integration["allowed_channels"] = []
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True


class TestHandleEventIdentityResolution:
    """Step 2: Identity resolution."""

    async def test_unlinked_user_fails(self):
        svc, _ = _make_pipeline_service(link=None)
        event = _make_event()
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False
        assert result.error_stage == "identity_resolution"
        adapter.send_message.assert_awaited()  # Ephemeral error sent

    async def test_linked_user_passes(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        event = _make_event()
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True
        assert result.user_id is not None


class TestHandleEventRBAC:
    """Step 3: RBAC permission check."""

    async def test_inactive_user_denied(self):
        user = _make_user(is_active=False)
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        # Override to return inactive user
        svc._user_repo.get_by_id = AsyncMock(return_value=user)
        event = _make_event()
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False
        assert result.error_stage == "rbac"

    async def test_user_not_found_denied(self):
        link = _make_link()
        svc, _ = _make_pipeline_service(user=None, link=link)
        event = _make_event()
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False
        assert result.error_stage == "rbac"


class TestHandleEventRateLimit:
    """Step 4: Rate limit check."""

    async def test_rate_limited_user_denied(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link, rate_allowed=False)
        event = _make_event()
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False
        assert result.error_stage == "rate_limit"


class TestHandleEventSanitize:
    """Step 5: Input sanitization."""

    async def test_empty_text_returns_hint(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        event = _make_event(text="")
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True
        assert "send a message" in result.response_text.lower() or "command" in result.response_text.lower()

    async def test_whitespace_only_returns_hint(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        event = _make_event(text="   \n  \t  ")
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True


class TestHandleEventDispatch:
    """Steps 6-9: Dispatch, format, send."""

    async def test_full_pipeline_success(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(
            user=user, link=link, mcp_result="Here are your results",
        )
        event = _make_event(text="search for something")
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is True
        assert result.response_text == "Here are your results"
        adapter.format_response.assert_awaited_once()
        adapter.send_message.assert_awaited_once()

    async def test_pipeline_audits_command(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        event = _make_event(text="test command")
        integration = _make_integration()
        adapter = _MockAdapter()

        await svc.handle_event(event, integration, adapter)
        # Should audit identity_resolved and integration_command
        assert svc._audit_repo.log_integration_event.await_count >= 2


class TestHandleEventExceptionHandling:
    """Pipeline exception handling."""

    async def test_internal_error_sends_ephemeral(self):
        user = _make_user()
        link = _make_link(user_id=user["id"], organization_id=str(user["organization_id"]))
        svc, _ = _make_pipeline_service(user=user, link=link)
        # Make MCP dispatch blow up
        svc._dispatch_to_mcp = AsyncMock(side_effect=RuntimeError("MCP down"))
        event = _make_event(text="trigger error")
        integration = _make_integration()
        adapter = _MockAdapter()

        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False
        assert result.error_stage == "internal"

    async def test_ephemeral_send_failure_swallowed(self):
        svc, _ = _make_pipeline_service(link=None)
        event = _make_event()
        integration = _make_integration()
        adapter = _MockAdapter()
        adapter.send_message = AsyncMock(side_effect=Exception("send failed"))

        # Should not raise even though send fails
        result = await svc.handle_event(event, integration, adapter)
        assert result.success is False


class TestSanitizeInput:
    """Tests for IntegrationService._sanitize_input static method."""

    def test_empty_string(self):
        from lucent.integrations.service import IntegrationService

        assert IntegrationService._sanitize_input("") == ""

    def test_strips_control_chars(self):
        from lucent.integrations.service import IntegrationService

        result = IntegrationService._sanitize_input("hello\x00\x07world")
        assert result == "helloworld"

    def test_preserves_newlines_tabs(self):
        from lucent.integrations.service import IntegrationService

        result = IntegrationService._sanitize_input("line1\nline2\ttab")
        assert result == "line1\nline2\ttab"

    def test_truncates_to_max_length(self):
        from lucent.integrations.service import IntegrationService, MAX_INPUT_LENGTH

        long_text = "a" * (MAX_INPUT_LENGTH + 100)
        result = IntegrationService._sanitize_input(long_text)
        assert len(result) == MAX_INPUT_LENGTH

    def test_strips_whitespace(self):
        from lucent.integrations.service import IntegrationService

        result = IntegrationService._sanitize_input("  hello  ")
        assert result == "hello"


class TestServiceResult:
    """Tests for ServiceResult dataclass."""

    def test_success_result(self):
        from lucent.integrations.service import ServiceResult

        r = ServiceResult(success=True, response_text="ok")
        assert r.success is True
        assert r.response_text == "ok"
        assert r.user_id is None
        assert r.error_stage is None

    def test_failure_result(self):
        from lucent.integrations.service import ServiceResult

        r = ServiceResult(
            success=False,
            response_text="error",
            error_stage="identity_resolution",
            user_id=uuid4(),
        )
        assert r.success is False
        assert r.error_stage == "identity_resolution"

    def test_frozen(self):
        from lucent.integrations.service import ServiceResult

        r = ServiceResult(success=True, response_text="ok")
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]
