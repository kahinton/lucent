"""Tests for credential API router handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from lucent.api.deps import CurrentUser
from lucent.integrations.credential_models import (
    CredentialCreate,
    CredentialIntegrationType,
    CredentialKind,
    CredentialScopeType,
    OAuthCallbackRequest,
    OAuthProvider,
    OAuthStartRequest,
    OAuthStartResponse,
)
from lucent.integrations.credential_router import (
    create_credential,
    get_credential,
    list_credentials,
    oauth_callback,
    start_oauth,
)


@pytest.fixture
def user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role="member",
        email="user@test.dev",
        display_name="User",
    )


def _row(user: CurrentUser) -> dict:
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "organization_id": user.organization_id,
        "integration_type": "github",
        "credential_kind": "oauth2",
        "scope_type": "user",
        "owner_user_id": user.id,
        "owner_agent_id": None,
        "display_name": "GitHub",
        "scopes": ["repo"],
        "status": "active",
        "access_token_expires_at": None,
        "refresh_token_expires_at": None,
        "last_refreshed_at": None,
        "refresh_token_version": 1,
        "token_rotated_at": None,
        "created_by": user.id,
        "updated_by": None,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_create_and_get_credential(
    monkeypatch: pytest.MonkeyPatch, user: CurrentUser
) -> None:
    row = _row(user)
    svc = AsyncMock()
    svc.create_credential = AsyncMock(return_value=row)
    svc.get_credential = AsyncMock(return_value=row)

    async def _fake_service():
        return svc

    monkeypatch.setattr("lucent.integrations.credential_router._service", _fake_service)

    created = await create_credential(
        CredentialCreate(
            integration_type=CredentialIntegrationType.GITHUB,
            credential_kind=CredentialKind.OAUTH2,
            scope_type=CredentialScopeType.USER,
            display_name="GitHub",
        ),
        user,
    )
    fetched = await get_credential(created.id, user)

    assert created.id == row["id"]
    assert fetched.display_name == "GitHub"


@pytest.mark.asyncio
async def test_list_credentials(monkeypatch: pytest.MonkeyPatch, user: CurrentUser) -> None:
    rows = [_row(user), _row(user)]
    svc = AsyncMock()
    svc.list_credentials = AsyncMock(return_value=rows)

    async def _fake_service():
        return svc

    monkeypatch.setattr("lucent.integrations.credential_router._service", _fake_service)

    result = await list_credentials(user=user)
    assert result.total_count == 2


@pytest.mark.asyncio
async def test_oauth_start_and_callback(monkeypatch: pytest.MonkeyPatch, user: CurrentUser) -> None:
    now = datetime.now(UTC)
    row = _row(user)
    svc = AsyncMock()
    svc.start_oauth = AsyncMock(
        return_value=OAuthStartResponse(
            provider=OAuthProvider.GITHUB,
            authorization_url="https://github.com/login/oauth/authorize?x=1",
            state="state",
            expires_at=now,
        )
    )
    svc.complete_oauth = AsyncMock(return_value=row)

    async def _fake_service():
        return svc

    monkeypatch.setattr("lucent.integrations.credential_router._service", _fake_service)

    started = await start_oauth(
        OAuthStartRequest(
            provider=OAuthProvider.GITHUB,
            display_name="GitHub OAuth",
            redirect_uri="https://example.com/callback",
        ),
        user,
    )

    completed = await oauth_callback(
        OAuthCallbackRequest(
            provider=OAuthProvider.GITHUB,
            code="abc",
            state="state",
            redirect_uri="https://example.com/callback",
        ),
        user,
        display_name="GitHub OAuth",
    )

    assert "github.com" in started.authorization_url
    assert completed.display_name == "GitHub"
