"""Tests for the Connections read-model builder and admin permission gate.

* ``test_build_view_model_*`` exercises ``build_connections_view_model``,
  the explicit read-model assembly used by the GET handler.
* ``test_admin_gate_*`` verifies that workspace integration mutation
  endpoints reject non-admin callers via ``MANAGE_INTEGRATIONS``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from lucent.api.deps import CurrentUser
from lucent.integrations.models import (
    IntegrationCreate,
    IntegrationType,
    IntegrationUpdate,
)
from lucent.integrations.router import (
    create_integration,
    delete_integration,
    update_integration,
)
from lucent.web.routes.connections import build_connections_view_model

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(role: str = "member") -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role=role,
        email=f"{role}@test.dev",
        display_name=role.title(),
    )


def _make_pool() -> MagicMock:
    """Mock asyncpg pool placeholder — repositories are patched per-test."""
    return MagicMock()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LUCENT_CONNECTIONS_PAT_ENABLED",
        "LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED",
        "LUCENT_CONNECTIONS_OAUTH_ENABLED",
        "LUCENT_WORKSPACE_INTEGRATIONS_ENABLED",
        "LUCENT_GITHUB_APP_ENABLED",
        "LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    for prov in ("github", "slack", "jira"):
        monkeypatch.delenv(f"LUCENT_OAUTH_{prov.upper()}_CLIENT_ID", raising=False)


# ---------------------------------------------------------------------------
# Read-model builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_view_model_default_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults: PAT/OAuth/workspace on, GitHub App off, owner sees admin caps."""
    user = _user("admin")

    cred_repo = MagicMock()
    cred_repo.list_credentials = AsyncMock(return_value=[])
    int_repo = MagicMock()
    int_repo.list_by_org = AsyncMock(return_value=[])

    monkeypatch.setattr(
        "lucent.integrations.credential_repository.CredentialRepository",
        lambda pool: cred_repo,
    )
    monkeypatch.setattr(
        "lucent.integrations.repositories.IntegrationRepo",
        lambda pool: int_repo,
    )

    vm = await build_connections_view_model(user=user, pool=_make_pool())

    # All required top-level keys present.
    assert set(vm.keys()) == {
        "feature_flags",
        "admin_permissions",
        "provider_capabilities",
        "workspace_connections",
        "your_connected_accounts",
        "env_detected",
    }
    assert vm["feature_flags"]["pat_enabled"] is True
    assert vm["feature_flags"]["github_app_enabled"] is False
    assert vm["admin_permissions"] == {
        "manage_integrations": True,
        "is_owner": False,
    }
    assert vm["workspace_connections"] == []
    assert vm["your_connected_accounts"] == []
    assert vm["env_detected"] == {}

    caps_by_id = {p["id"]: p for p in vm["provider_capabilities"]}
    # Without LUCENT_OAUTH_*_CLIENT_ID set, OAuth capability is False even
    # though the global flag is on.
    assert caps_by_id["github"]["supports_oauth"] is False
    assert caps_by_id["github"]["supports_pat"] is True
    assert caps_by_id["github"]["supports_workspace_app"] is False  # app off

    # The personal section query is scoped to the current user.
    cred_repo.list_credentials.assert_awaited_once()
    kwargs = cred_repo.list_credentials.await_args.kwargs
    assert kwargs["owner_user_id"] == str(user.id)
    assert kwargs["scope_type"] == "user"


@pytest.mark.asyncio
async def test_build_view_model_pat_disabled_disables_provider_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_CONNECTIONS_PAT_ENABLED", "false")

    cred_repo = MagicMock()
    cred_repo.list_credentials = AsyncMock(return_value=[])
    int_repo = MagicMock()
    int_repo.list_by_org = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "lucent.integrations.credential_repository.CredentialRepository",
        lambda pool: cred_repo,
    )
    monkeypatch.setattr(
        "lucent.integrations.repositories.IntegrationRepo",
        lambda pool: int_repo,
    )

    vm = await build_connections_view_model(user=_user("admin"), pool=_make_pool())

    assert vm["feature_flags"]["pat_enabled"] is False
    for cap in vm["provider_capabilities"]:
        assert cap["supports_pat"] is False


@pytest.mark.asyncio
async def test_build_view_model_workspace_disabled_returns_empty_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_WORKSPACE_INTEGRATIONS_ENABLED", "false")

    cred_repo = MagicMock()
    cred_repo.list_credentials = AsyncMock(return_value=[])
    # If the flag works correctly the IntegrationRepo factory must NOT be
    # called — fail loudly if it is.
    int_repo_factory = MagicMock(
        side_effect=AssertionError("IntegrationRepo built when workspace flag is off")
    )
    monkeypatch.setattr(
        "lucent.integrations.credential_repository.CredentialRepository",
        lambda pool: cred_repo,
    )
    monkeypatch.setattr(
        "lucent.integrations.repositories.IntegrationRepo", int_repo_factory
    )

    vm = await build_connections_view_model(user=_user("admin"), pool=_make_pool())
    assert vm["workspace_connections"] == []
    assert vm["feature_flags"]["workspace_integrations_enabled"] is False


@pytest.mark.asyncio
async def test_build_view_model_member_cannot_manage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user("member")

    cred_repo = MagicMock()
    cred_repo.list_credentials = AsyncMock(return_value=[])
    int_repo = MagicMock()
    int_repo.list_by_org = AsyncMock(
        return_value=[
            {
                "id": uuid4(),
                "type": "github_app",
                "external_workspace_id": "acme",
                "install_id": "12345",
                "status": "active",
                "created_by": uuid4(),
                "created_at": datetime.now(UTC),
                "health_status": "healthy",
                "health_detail": None,
                "health_checked_at": None,
            }
        ]
    )
    monkeypatch.setattr(
        "lucent.integrations.credential_repository.CredentialRepository",
        lambda pool: cred_repo,
    )
    monkeypatch.setattr(
        "lucent.integrations.repositories.IntegrationRepo",
        lambda pool: int_repo,
    )

    vm = await build_connections_view_model(user=user, pool=_make_pool())

    assert vm["admin_permissions"]["manage_integrations"] is False
    assert vm["admin_permissions"]["is_owner"] is False
    wc = vm["workspace_connections"]
    assert len(wc) == 1
    assert wc[0]["actions"] == {"can_disable": False, "can_revoke": False}
    assert wc[0]["health"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_build_view_model_env_detected_respects_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abcdefghijklmno1234")

    cred_repo = MagicMock()
    cred_repo.list_credentials = AsyncMock(return_value=[])
    int_repo = MagicMock()
    int_repo.list_by_org = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "lucent.integrations.credential_repository.CredentialRepository",
        lambda pool: cred_repo,
    )
    monkeypatch.setattr(
        "lucent.integrations.repositories.IntegrationRepo",
        lambda pool: int_repo,
    )

    # Default: env-token claim ON → env detection populated.
    vm_on = await build_connections_view_model(user=_user("admin"), pool=_make_pool())
    assert "github" in vm_on["env_detected"]

    # Disable claim → env detection drops to empty.
    monkeypatch.setenv("LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED", "false")
    vm_off = await build_connections_view_model(user=_user("admin"), pool=_make_pool())
    assert vm_off["env_detected"] == {}


# ---------------------------------------------------------------------------
# Admin permission gate on workspace integration mutations
# ---------------------------------------------------------------------------


def _patch_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid hitting a real DB pool when the gate rejects before queries."""
    async def _fake_get_pool() -> MagicMock:  # pragma: no cover - guard only
        return MagicMock()

    monkeypatch.setattr("lucent.integrations.router.get_pool", _fake_get_pool)


@pytest.mark.asyncio
async def test_create_integration_rejects_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pool(monkeypatch)
    member = _user("member")
    body = IntegrationCreate(type=IntegrationType.GITHUB_APP, config={"k": "v"})

    with pytest.raises(HTTPException) as ei:
        await create_integration(body=body, user=member)
    assert ei.value.status_code == 403
    assert "integrations.manage" in ei.value.detail


@pytest.mark.asyncio
async def test_update_integration_rejects_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pool(monkeypatch)
    member = _user("member")
    body = IntegrationUpdate(allowed_channels=["C1"])

    with pytest.raises(HTTPException) as ei:
        await update_integration(integration_id=uuid4(), body=body, user=member)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_integration_rejects_non_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pool(monkeypatch)
    member = _user("member")

    with pytest.raises(HTTPException) as ei:
        await delete_integration(integration_id=uuid4(), user=member)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_passes_permission_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin clears MANAGE_INTEGRATIONS — execution reaches the DB layer.

    We assert that *no* 403 is raised. Downstream failure (any other
    exception type) is fine and proves the gate did not fire.
    """
    _patch_pool(monkeypatch)
    admin = _user("admin")
    body = IntegrationCreate(type=IntegrationType.GITHUB_APP, config={"k": "v"})

    try:
        await create_integration(body=body, user=admin)
    except HTTPException as e:
        assert e.status_code != 403, "Admin should pass MANAGE_INTEGRATIONS gate"
    except Exception:
        # Any non-HTTPException is acceptable: the gate let us through and
        # we hit the (mocked) DB layer or encryptor.
        pass
