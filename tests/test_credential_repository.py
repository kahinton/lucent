"""Unit tests for credential repository SQL wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from lucent.integrations.credential_repository import CredentialRepository


class _FakeRecord(dict):
    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


def _make_pool(
    fetchrow_return: dict | None = None, fetch_return: list[dict] | None = None
) -> MagicMock:
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value=_FakeRecord(fetchrow_return) if fetchrow_return else None
    )
    conn.fetch = AsyncMock(return_value=[_FakeRecord(r) for r in (fetch_return or [])])
    conn.execute = AsyncMock(return_value="DELETE 1")

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _credential_row() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "organization_id": uuid4(),
        "integration_type": "github",
        "credential_kind": "oauth2",
        "scope_type": "user",
        "owner_user_id": uuid4(),
        "owner_agent_id": None,
        "display_name": "GitHub",
        "scopes": ["repo"],
        "encrypted_secret_payload": b"secret",
        "encrypted_metadata": None,
        "access_token_expires_at": now,
        "refresh_token_expires_at": None,
        "last_refreshed_at": None,
        "last_used_at": None,
        "refresh_token_version": 1,
        "token_rotated_at": None,
        "status": "active",
        "created_by": uuid4(),
        "updated_by": None,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_create_and_get_credential() -> None:
    row = _credential_row()
    pool = _make_pool(fetchrow_return=row)
    repo = CredentialRepository(pool)

    created = await repo.create_credential(
        organization_id=str(row["organization_id"]),
        integration_type="github",
        credential_kind="oauth2",
        scope_type="user",
        owner_user_id=str(row["owner_user_id"]),
        owner_agent_id=None,
        display_name="GitHub",
        scopes=["repo"],
        encrypted_secret_payload=b"secret",
        encrypted_metadata=None,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        created_by=str(row["created_by"]),
    )
    fetched = await repo.get_credential(str(created["id"]), str(created["organization_id"]))

    assert created["display_name"] == "GitHub"
    assert fetched is not None


@pytest.mark.asyncio
async def test_consume_oauth_state_returns_row() -> None:
    row = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "provider": "github",
        "state_hash": "abc",
        "requested_scopes": ["repo"],
        "scope_type": "user",
        "owner_user_id": uuid4(),
        "owner_agent_id": None,
        "pkce_verifier": "v",
        "redirect_uri": "https://example.com/callback",
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    pool = _make_pool(fetchrow_return=row)
    repo = CredentialRepository(pool)

    consumed = await repo.consume_oauth_state(
        organization_id=str(row["organization_id"]),
        provider="github",
        state_hash="abc",
    )

    assert consumed is not None
    assert consumed["provider"] == "github"


@pytest.mark.asyncio
async def test_delete_credential_true_when_deleted() -> None:
    row = _credential_row()
    pool = _make_pool(fetchrow_return=row)
    repo = CredentialRepository(pool)

    deleted = await repo.delete_credential(str(uuid4()), str(row["organization_id"]))
    assert deleted is True
