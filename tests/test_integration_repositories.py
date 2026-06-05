"""Tests for lucent.integrations.repositories — IntegrationRepo, UserLinkRepo, PairingChallengeRepo.

Uses mocked asyncpg pool. Tests CRUD, lifecycle transitions, and state machine logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from lucent.integrations.repositories import (
    _INTEGRATION_TRANSITIONS,
    _USER_LINK_TRANSITIONS,
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid() -> str:
    return str(uuid4())


class _FakeRecord(dict):
    """Minimal asyncpg.Record stand-in."""

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


def _make_pool(
    *,
    fetchrow_return: dict | None = None,
    fetch_return: list[dict] | None = None,
    execute_return: str = "UPDATE 0",
) -> MagicMock:
    """Build a mock asyncpg Pool."""
    pool = MagicMock()
    conn = AsyncMock()

    if fetchrow_return is not None:
        conn.fetchrow = AsyncMock(return_value=_FakeRecord(fetchrow_return))
    else:
        conn.fetchrow = AsyncMock(return_value=None)

    if fetch_return is not None:
        conn.fetch = AsyncMock(return_value=[_FakeRecord(r) for r in fetch_return])
    else:
        conn.fetch = AsyncMock(return_value=[])

    conn.execute = AsyncMock(return_value=execute_return)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


def _integration_row(
    *,
    integration_id: str | None = None,
    organization_id: str | None = None,
    type: str = "slack",
    status: str = "active",
    allowed_channels: str | list | None = None,
) -> dict[str, Any]:
    return {
        "id": UUID(integration_id) if integration_id else uuid4(),
        "organization_id": UUID(organization_id) if organization_id else uuid4(),
        "type": type,
        "status": status,
        "encrypted_config": b"encrypted",
        "config_version": 1,
        "external_workspace_id": None,
        "allowed_channels": json.dumps(allowed_channels or []),
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
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "id": UUID(link_id) if link_id else uuid4(),
        "organization_id": UUID(organization_id) if organization_id else uuid4(),
        "integration_id": uuid4(),
        "user_id": uuid4(),
        "provider": "slack",
        "external_user_id": "U_EXT_123",
        "external_workspace_id": None,
        "status": status,
        "verification_method": "pairing_code",
        "superseded_by": None,
        "linked_at": None,
        "revoked_at": None,
        "revoked_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def _challenge_row(
    *,
    challenge_id: str | None = None,
    status: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = 5,
) -> dict[str, Any]:
    return {
        "id": UUID(challenge_id) if challenge_id else uuid4(),
        "integration_id": uuid4(),
        "user_id": uuid4(),
        "code_hash": "$2b$12$fakehash",
        "status": status,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "claimed_by_external_id": None,
        "created_at": datetime.now(timezone.utc),
    }


# ===========================================================================
# IntegrationRepo
# ===========================================================================


class TestIntegrationRepoCreate:
    """IntegrationRepo.create tests."""

    @pytest.mark.asyncio
    async def test_create_returns_dict(self) -> None:
        row = _integration_row()
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.create(
            organization_id=_uuid(),
            type="slack",
            encrypted_config=b"encrypted",
            created_by=_uuid(),
        )
        assert isinstance(result, dict)
        assert result["type"] == "slack"

    @pytest.mark.asyncio
    async def test_create_passes_channels(self) -> None:
        row = _integration_row(allowed_channels=["C1", "C2"])
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.create(
            organization_id=_uuid(),
            type="slack",
            encrypted_config=b"enc",
            created_by=_uuid(),
            allowed_channels=["C1", "C2"],
        )
        assert result["allowed_channels"] == ["C1", "C2"]

    @pytest.mark.asyncio
    async def test_create_with_workspace_id(self) -> None:
        row = _integration_row()
        row["external_workspace_id"] = "T_WORKSPACE"
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.create(
            organization_id=_uuid(),
            type="slack",
            encrypted_config=b"enc",
            created_by=_uuid(),
            external_workspace_id="T_WORKSPACE",
        )
        assert result["external_workspace_id"] == "T_WORKSPACE"


class TestIntegrationRepoRead:
    """IntegrationRepo read methods."""

    @pytest.mark.asyncio
    async def test_get_by_id_found(self) -> None:
        org_id = _uuid()
        int_id = _uuid()
        row = _integration_row(integration_id=int_id, organization_id=org_id)
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.get_by_id(int_id, org_id)
        assert result is not None
        assert str(result["id"]) == int_id

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self) -> None:
        pool = _make_pool()
        repo = IntegrationRepo(pool)
        result = await repo.get_by_id(_uuid(), _uuid())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_by_type_found(self) -> None:
        row = _integration_row(status="active")
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.get_active_by_type(_uuid(), "slack")
        assert result is not None
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_get_active_by_type_with_workspace(self) -> None:
        row = _integration_row()
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.get_active_by_type(_uuid(), "slack", "T_WS")
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_active_by_type_not_found(self) -> None:
        pool = _make_pool()
        repo = IntegrationRepo(pool)
        result = await repo.get_active_by_type(_uuid(), "slack")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_by_org(self) -> None:
        rows = [_integration_row(), _integration_row()]
        pool = _make_pool(fetch_return=rows)
        repo = IntegrationRepo(pool)

        result = await repo.list_by_org(_uuid())
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_by_org_with_status(self) -> None:
        rows = [_integration_row(status="disabled")]
        pool = _make_pool(fetch_return=rows)
        repo = IntegrationRepo(pool)

        result = await repo.list_by_org(_uuid(), status="disabled")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_by_org_empty(self) -> None:
        pool = _make_pool(fetch_return=[])
        repo = IntegrationRepo(pool)
        result = await repo.list_by_org(_uuid())
        assert result == []


class TestIntegrationRepoUpdate:
    """IntegrationRepo.update tests."""

    @pytest.mark.asyncio
    async def test_update_channels(self) -> None:
        row = _integration_row(allowed_channels=["C_NEW"])
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.update(
            _uuid(), _uuid(), updated_by=_uuid(), allowed_channels=["C_NEW"],
        )
        assert result is not None
        assert result["allowed_channels"] == ["C_NEW"]

    @pytest.mark.asyncio
    async def test_update_config(self) -> None:
        row = _integration_row()
        row["config_version"] = 2
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.update(
            _uuid(), _uuid(), updated_by=_uuid(), encrypted_config=b"new_enc",
        )
        assert result is not None
        assert result["config_version"] == 2

    @pytest.mark.asyncio
    async def test_update_not_found(self) -> None:
        pool = _make_pool()
        repo = IntegrationRepo(pool)
        result = await repo.update(_uuid(), _uuid(), updated_by=_uuid())
        assert result is None


class TestIntegrationRepoLifecycle:
    """Lifecycle transition tests."""

    @pytest.mark.asyncio
    async def test_activate_from_disabled(self) -> None:
        row = _integration_row(status="active")
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.activate(_uuid(), _uuid(), updated_by=_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_disable_returns_row(self) -> None:
        row = _integration_row(status="disabled")
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.disable(_uuid(), _uuid(), updated_by=_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_revoke_returns_row(self) -> None:
        row = _integration_row(status="revoked")
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.revoke(_uuid(), _uuid(), updated_by=_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_revoke_with_reason(self) -> None:
        row = _integration_row(status="revoked")
        row["revoke_reason"] = "compromised"
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.revoke(
            _uuid(), _uuid(), updated_by=_uuid(), reason="compromised",
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_soft_delete_returns_row(self) -> None:
        row = _integration_row(status="deleted")
        pool = _make_pool(fetchrow_return=row)
        repo = IntegrationRepo(pool)

        result = await repo.soft_delete(_uuid(), _uuid(), updated_by=_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_transition_invalid_returns_none(self) -> None:
        pool = _make_pool()  # fetchrow returns None = no matching row
        repo = IntegrationRepo(pool)

        result = await repo.activate(_uuid(), _uuid(), updated_by=_uuid())
        assert result is None


class TestIntegrationTransitionMap:
    """Verify the transition map is consistent."""

    def test_active_can_transition(self) -> None:
        assert _INTEGRATION_TRANSITIONS["active"] == {"disabled", "revoked", "deleted"}

    def test_disabled_can_transition(self) -> None:
        assert _INTEGRATION_TRANSITIONS["disabled"] == {"active", "revoked", "deleted"}

    def test_revoked_can_only_delete(self) -> None:
        assert _INTEGRATION_TRANSITIONS["revoked"] == {"deleted"}

    def test_deleted_is_terminal(self) -> None:
        assert "deleted" not in _INTEGRATION_TRANSITIONS


class TestIntegrationRepoRowToDict:
    """_row_to_dict JSON parsing."""

    def test_parses_json_string_channels(self) -> None:
        row = _FakeRecord({
            "allowed_channels": '["C1","C2"]',
            "id": uuid4(),
            "type": "slack",
        })
        result = IntegrationRepo._row_to_dict(row)
        assert result["allowed_channels"] == ["C1", "C2"]

    def test_leaves_list_channels(self) -> None:
        row = _FakeRecord({
            "allowed_channels": ["C1"],
            "id": uuid4(),
        })
        result = IntegrationRepo._row_to_dict(row)
        assert result["allowed_channels"] == ["C1"]


# ===========================================================================
# UserLinkRepo
# ===========================================================================


class TestUserLinkRepoCreate:
    """UserLinkRepo.create tests."""

    @pytest.mark.asyncio
    async def test_create_returns_dict(self) -> None:
        row = _link_row()
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.create(
            organization_id=_uuid(),
            integration_id=_uuid(),
            user_id=_uuid(),
            provider="slack",
            external_user_id="U_EXT_1",
            verification_method="pairing_code",
        )
        assert isinstance(result, dict)
        assert result["provider"] == "slack"


class TestUserLinkRepoRead:
    """UserLinkRepo read methods."""

    @pytest.mark.asyncio
    async def test_get_by_id_found(self) -> None:
        row = _link_row()
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.get_by_id(_uuid(), _uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self) -> None:
        pool = _make_pool()
        repo = UserLinkRepo(pool)
        result = await repo.get_by_id(_uuid(), _uuid())
        assert result is None

    @pytest.mark.asyncio
    async def test_list_by_org(self) -> None:
        rows = [_link_row(), _link_row()]
        pool = _make_pool(fetch_return=rows)
        repo = UserLinkRepo(pool)

        result = await repo.list_by_org(_uuid())
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_by_user(self) -> None:
        rows = [_link_row()]
        pool = _make_pool(fetch_return=rows)
        repo = UserLinkRepo(pool)

        result = await repo.list_by_user(_uuid(), _uuid())
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_by_integration(self) -> None:
        rows = [_link_row()]
        pool = _make_pool(fetch_return=rows)
        repo = UserLinkRepo(pool)

        result = await repo.list_by_integration(_uuid(), _uuid())
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_by_org_with_status_filter(self) -> None:
        pool = _make_pool(fetch_return=[])
        repo = UserLinkRepo(pool)

        result = await repo.list_by_org(_uuid(), status="active")
        assert result == []

    @pytest.mark.asyncio
    async def test_resolve_identity_found(self) -> None:
        row = _link_row(status="active")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.resolve_identity("slack", "U_EXT_123")
        assert result is not None
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_resolve_identity_with_workspace(self) -> None:
        row = _link_row(status="active")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.resolve_identity("slack", "U_EXT_123", "T_WS")
        assert result is not None

    @pytest.mark.asyncio
    async def test_resolve_identity_not_found(self) -> None:
        pool = _make_pool()
        repo = UserLinkRepo(pool)
        result = await repo.resolve_identity("slack", "UNKNOWN")
        assert result is None


class TestUserLinkRepoLifecycle:
    """Lifecycle transition tests for UserLinkRepo."""

    @pytest.mark.asyncio
    async def test_activate(self) -> None:
        row = _link_row(status="active")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.activate(_uuid(), _uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_revoke(self) -> None:
        row = _link_row(status="revoked")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.revoke(_uuid(), _uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_revoke_with_revoked_by(self) -> None:
        row = _link_row(status="revoked")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.revoke(_uuid(), _uuid(), revoked_by=_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_supersede(self) -> None:
        row = _link_row(status="superseded")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.supersede(_uuid(), _uuid(), superseded_by=_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_orphan(self) -> None:
        row = _link_row(status="orphaned")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.orphan(_uuid(), _uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_disable(self) -> None:
        row = _link_row(status="disabled")
        pool = _make_pool(fetchrow_return=row)
        repo = UserLinkRepo(pool)

        result = await repo.disable(_uuid(), _uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_transition_returns_none_when_no_match(self) -> None:
        pool = _make_pool()
        repo = UserLinkRepo(pool)
        result = await repo.activate(_uuid(), _uuid())
        assert result is None

    @pytest.mark.asyncio
    async def test_bulk_orphan_by_integration(self) -> None:
        pool = _make_pool(execute_return="UPDATE 3")
        repo = UserLinkRepo(pool)

        count = await repo.bulk_orphan_by_integration(_uuid(), _uuid())
        assert count == 3

    @pytest.mark.asyncio
    async def test_bulk_orphan_zero(self) -> None:
        pool = _make_pool(execute_return="UPDATE 0")
        repo = UserLinkRepo(pool)

        count = await repo.bulk_orphan_by_integration(_uuid(), _uuid())
        assert count == 0


class TestUserLinkTransitionMap:
    """Verify user link transition map."""

    def test_pending_transitions(self) -> None:
        assert _USER_LINK_TRANSITIONS["pending"] == {"active", "revoked"}

    def test_active_transitions(self) -> None:
        assert _USER_LINK_TRANSITIONS["active"] == {"revoked", "superseded", "orphaned", "disabled"}

    def test_orphaned_can_reactivate(self) -> None:
        assert _USER_LINK_TRANSITIONS["orphaned"] == {"active"}

    def test_disabled_can_reactivate(self) -> None:
        assert _USER_LINK_TRANSITIONS["disabled"] == {"active"}

    def test_revoked_is_terminal(self) -> None:
        assert "revoked" not in _USER_LINK_TRANSITIONS

    def test_superseded_is_terminal(self) -> None:
        assert "superseded" not in _USER_LINK_TRANSITIONS


# ===========================================================================
# PairingChallengeRepo
# ===========================================================================


class TestPairingChallengeRepoCreate:
    """PairingChallengeRepo.create tests."""

    @pytest.mark.asyncio
    async def test_create_returns_dict(self) -> None:
        row = _challenge_row()
        pool = _make_pool(fetchrow_return=row)
        repo = PairingChallengeRepo(pool)

        result = await repo.create(
            integration_id=_uuid(),
            user_id=_uuid(),
            code_hash="$2b$12$hash",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        assert isinstance(result, dict)
        assert result["status"] == "pending"


class TestPairingChallengeRepoRead:
    """PairingChallengeRepo read methods."""

    @pytest.mark.asyncio
    async def test_get_by_id_found(self) -> None:
        row = _challenge_row()
        pool = _make_pool(fetchrow_return=row)
        repo = PairingChallengeRepo(pool)

        result = await repo.get_by_id(_uuid())
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self) -> None:
        pool = _make_pool()
        repo = PairingChallengeRepo(pool)
        result = await repo.get_by_id(_uuid())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_pending_for_user(self) -> None:
        rows = [_challenge_row(), _challenge_row()]
        pool = _make_pool(fetch_return=rows)
        repo = PairingChallengeRepo(pool)

        result = await repo.get_pending_for_user(_uuid(), _uuid())
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_count_recent_by_user(self) -> None:
        pool = _make_pool(fetchrow_return={"cnt": 5})
        repo = PairingChallengeRepo(pool)

        count = await repo.count_recent_by_user(
            _uuid(), datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert count == 5

    @pytest.mark.asyncio
    async def test_count_recent_returns_zero(self) -> None:
        pool = _make_pool(fetchrow_return={"cnt": 0})
        repo = PairingChallengeRepo(pool)

        count = await repo.count_recent_by_user(
            _uuid(), datetime.now(timezone.utc),
        )
        assert count == 0


class TestPairingChallengeRepoLifecycle:
    """PairingChallengeRepo lifecycle operations."""

    @pytest.mark.asyncio
    async def test_increment_attempts(self) -> None:
        row = _challenge_row(attempt_count=1)
        pool = _make_pool(fetchrow_return=row)
        repo = PairingChallengeRepo(pool)

        result = await repo.increment_attempts(_uuid())
        assert result is not None
        assert result["attempt_count"] == 1

    @pytest.mark.asyncio
    async def test_increment_attempts_not_pending(self) -> None:
        pool = _make_pool()
        repo = PairingChallengeRepo(pool)
        result = await repo.increment_attempts(_uuid())
        assert result is None

    @pytest.mark.asyncio
    async def test_redeem(self) -> None:
        row = _challenge_row(status="used")
        pool = _make_pool(fetchrow_return=row)
        repo = PairingChallengeRepo(pool)

        result = await repo.redeem(_uuid(), claimed_by_external_id="U_SLACK_1")
        assert result is not None
        assert result["status"] == "used"

    @pytest.mark.asyncio
    async def test_redeem_expired(self) -> None:
        pool = _make_pool()
        repo = PairingChallengeRepo(pool)
        result = await repo.redeem(_uuid(), claimed_by_external_id="U_SLACK_1")
        assert result is None

    @pytest.mark.asyncio
    async def test_expire_stale(self) -> None:
        pool = _make_pool(execute_return="UPDATE 5")
        repo = PairingChallengeRepo(pool)

        count = await repo.expire_stale()
        assert count == 5

    @pytest.mark.asyncio
    async def test_expire_stale_none(self) -> None:
        pool = _make_pool(execute_return="UPDATE 0")
        repo = PairingChallengeRepo(pool)

        count = await repo.expire_stale()
        assert count == 0
