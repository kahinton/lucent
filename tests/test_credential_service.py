"""Tests for credential service OAuth refresh/rotation and scoping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from lucent.api.deps import CurrentUser
from lucent.integrations.credential_models import (
    CredentialCreate,
    CredentialIntegrationType,
    CredentialScopeType,
)
from lucent.integrations.credential_service import CredentialService
from lucent.integrations.encryption import FernetEncryptor
from lucent.integrations.oauth import OAuthTokenResponse


@pytest.fixture
def encryptor() -> FernetEncryptor:
    return FernetEncryptor(key="j4Gpi8xvXWMmV1YvKdxAab4Q7Q2lmfLBN8f2Scf8CIY=")


@pytest.fixture
def member_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role="member",
        email="member@test.dev",
        display_name="Member",
    )


@pytest.fixture
def admin_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role="admin",
        email="admin@test.dev",
        display_name="Admin",
    )


@pytest.mark.asyncio
async def test_member_cannot_create_agent_scoped_credential(
    member_user: CurrentUser,
    encryptor: FernetEncryptor,
) -> None:
    repo = AsyncMock()
    svc = CredentialService(repo, oauth=SimpleNamespace(), encryptor=encryptor)

    payload = CredentialCreate(
        integration_type=CredentialIntegrationType.GITHUB,
        display_name="Agent GitHub",
        scope_type=CredentialScopeType.AGENT,
        owner_agent_id=uuid4(),
    )

    with pytest.raises(HTTPException) as exc:
        await svc.create_credential(payload, member_user)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_refresh_rotates_refresh_token_version(
    admin_user: CurrentUser,
    encryptor: FernetEncryptor,
) -> None:
    credential_id = uuid4()
    org_id = admin_user.organization_id

    old_payload = encryptor.encrypt({"access_token": "old-a", "refresh_token": "old-r"})

    repo = AsyncMock()
    repo.get_credential = AsyncMock(
        return_value={
            "id": credential_id,
            "organization_id": org_id,
            "scope_type": "user",
            "owner_user_id": admin_user.id,
            "credential_kind": "oauth2",
            "integration_type": "github",
            "encrypted_secret_payload": old_payload,
            "refresh_token_version": 1,
        }
    )
    repo.mark_refreshed = AsyncMock(
        return_value={
            "id": credential_id,
            "refresh_token_version": 2,
            "access_token_expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    )

    oauth = AsyncMock()
    oauth.refresh_access_token = AsyncMock(
        return_value=OAuthTokenResponse(
            access_token="new-a",
            refresh_token="new-r",
            token_type="bearer",
            scope="repo",
            raw={"access_token": "new-a", "refresh_token": "new-r"},
            access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            refresh_token_expires_at=None,
        )
    )

    svc = CredentialService(repo, oauth=oauth, encryptor=encryptor)
    result = await svc.refresh_credential(credential_id, admin_user)

    assert result.refreshed is True
    assert result.rotated_refresh_token is True
    assert result.refresh_token_version == 2
    repo.mark_refreshed.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_fails_without_refresh_token(
    admin_user: CurrentUser,
    encryptor: FernetEncryptor,
) -> None:
    credential_id = uuid4()

    repo = AsyncMock()
    repo.get_credential = AsyncMock(
        return_value={
            "id": credential_id,
            "organization_id": admin_user.organization_id,
            "scope_type": "user",
            "owner_user_id": admin_user.id,
            "credential_kind": "oauth2",
            "integration_type": "github",
            "encrypted_secret_payload": encryptor.encrypt({"access_token": "only"}),
        }
    )

    oauth = AsyncMock()
    svc = CredentialService(repo, oauth=oauth, encryptor=encryptor)

    with pytest.raises(HTTPException) as exc:
        await svc.refresh_credential(credential_id, admin_user)

    assert exc.value.status_code == 409
    oauth.refresh_access_token.assert_not_called()
