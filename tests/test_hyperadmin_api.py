"""API tests for the hyperadmin instance endpoints.

Covers the surface an external signup site (and the local ops console) uses:
provision, member management, per-tenant settings, suspension, and purge. The
tenant-scoped behaviour these routes share with ordinary users is covered by
``tests/test_organizations_api.py``; here the focus is the cross-tenant paths and
the authorization boundary around them.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, get_current_user


@pytest_asyncio.fixture
async def ha_prefix(db_pool, delete_test_organizations):
    test_id = str(uuid4())[:8]
    prefix = f"ha_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM admin_audit_log WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memories WHERE username LIKE $1", f"{prefix}%"
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await delete_test_organizations(conn, [f"{prefix}%"])


@pytest_asyncio.fixture
async def ha_operator(db_pool, ha_prefix):
    """An instance operator with its own organization."""
    from lucent.db import OrganizationRepository, UserRepository

    org = await OrganizationRepository(db_pool).create(name=f"{ha_prefix}oporg")
    user = await UserRepository(db_pool).create(
        external_id=f"{ha_prefix}operator",
        provider="local",
        organization_id=org["id"],
        email=f"{ha_prefix}operator@test.com",
        display_name=f"{ha_prefix}Operator",
        role="hyperadmin",
    )
    return user


@pytest_asyncio.fixture
async def ha_tenant(db_pool, ha_prefix):
    """A tenant created directly in the DB (no first user)."""
    from lucent.db import OrganizationRepository

    return await OrganizationRepository(db_pool).create(name=f"{ha_prefix}tenant")


@pytest_asyncio.fixture
async def ha_tenant_member(db_pool, ha_tenant, ha_prefix):
    from lucent.db import UserRepository

    return await UserRepository(db_pool).create(
        external_id=f"{ha_prefix}tenantmember",
        provider="local",
        organization_id=ha_tenant["id"],
        email=f"{ha_prefix}member@tenant.test",
        display_name=f"{ha_prefix}Tenant Member",
        role="owner",
    )


def _client_for(user_dict, role="hyperadmin"):
    from lucent.api.app import create_app

    app = create_app()

    fake = CurrentUser(
        id=user_dict["id"],
        organization_id=user_dict.get("organization_id"),
        role=role,
        email=user_dict.get("email"),
        display_name=user_dict.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    return app


@pytest_asyncio.fixture
async def operator_client(db_pool, ha_operator):
    app = _client_for(ha_operator, role="hyperadmin")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def tenant_owner_client(db_pool, ha_tenant_member):
    app = _client_for(ha_tenant_member, role="owner")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestProvisionEndpoint:
    async def test_provision_creates_usable_tenant(
        self, operator_client, db_pool, ha_prefix
    ):
        resp = await operator_client.post(
            "/api/organizations/provision",
            json={
                "name": f"{ha_prefix}provisioned",
                "first_admin": {"email": f"{ha_prefix}first@tenant.test"},
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()

        assert data["organization"]["name"] == f"{ha_prefix}provisioned"
        assert data["user"]["role"] == "owner"
        assert data["user"]["must_change_password"] is True
        assert data["temporary_password"]
        assert data["api_key"].startswith("hs_")
        assert "skills" in data["seeded"]

    async def test_provision_without_admin_is_rejected(
        self, operator_client, ha_prefix
    ):
        """A headless tenant is never what a signup flow wants."""
        resp = await operator_client.post(
            "/api/organizations/provision",
            json={"name": f"{ha_prefix}headless"},
        )
        assert resp.status_code == 400
        # Without a first admin there is no identity to create.
        assert "email" in resp.json()["detail"].lower()

    async def test_provision_duplicate_name_conflicts(
        self, operator_client, ha_tenant, ha_prefix
    ):
        resp = await operator_client.post(
            "/api/organizations/provision",
            json={
                "name": ha_tenant["name"],
                "first_admin": {"email": f"{ha_prefix}dup@tenant.test"},
            },
        )
        assert resp.status_code == 409
        # The 409 names the field that collided, so a control plane knows
        # which input to change.
        assert resp.json()["detail"]["field"] == "name"

    async def test_provision_duplicate_owner_email_conflicts(
        self, operator_client, ha_prefix
    ):
        """The identity of the first admin is instance-unique — reusing it is a
        409 on ``owner_email``, not a second account with the same login."""
        body = {
            "name": f"{ha_prefix}emaildup",
            "first_admin": {"email": f"{ha_prefix}dup@tenant.test"},
        }
        first = await operator_client.post("/api/organizations/provision", json=body)
        assert first.status_code == 201, first.text

        again = await operator_client.post(
            "/api/organizations/provision",
            json={
                "name": f"{ha_prefix}oemaildup",
                "first_admin": {"email": f"{ha_prefix}dup@tenant.test"},
            },
        )
        assert again.status_code == 409
        detail = again.json()["detail"]
        assert detail["field"] == "owner_email"
        assert detail["message"]

    async def test_provision_rejects_admin_role_hyperadmin(
        self, operator_client, ha_prefix
    ):
        resp = await operator_client.post(
            "/api/organizations/provision",
            json={
                "name": f"{ha_prefix}badrole",
                "first_admin": {
                    "email": f"{ha_prefix}badrole@tenant.test",
                    "role": "hyperadmin",
                },
            },
        )
        assert resp.status_code == 400

    async def test_tenant_owner_cannot_provision(
        self, tenant_owner_client, ha_prefix
    ):
        resp = await tenant_owner_client.post(
            "/api/organizations/provision",
            json={
                "name": f"{ha_prefix}nope",
                "first_admin": {"email": f"{ha_prefix}nope@tenant.test"},
            },
        )
        assert resp.status_code == 403


class TestAccountLookup:
    """The signup pre-flight: does this email already hold a Lucent account?"""

    async def test_lookup_returns_accounts_with_tenant_context(
        self, operator_client, ha_tenant_member, ha_tenant, ha_prefix
    ):
        resp = await operator_client.get(
            "/api/organizations/instance/users/by-email",
            params={"email": f"{ha_prefix}member@tenant.test"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["email"] == f"{ha_prefix}member@tenant.test"
        assert len(data["accounts"]) == 1
        account = data["accounts"][0]
        assert account["user_id"] == str(ha_tenant_member["id"])
        assert account["organization_id"] == str(ha_tenant["id"])
        assert account["organization_name"] == ha_tenant["name"]
        assert account["role"] == "owner"

    async def test_lookup_matches_case_insensitively(
        self, operator_client, ha_tenant_member, ha_prefix
    ):
        resp = await operator_client.get(
            "/api/organizations/instance/users/by-email",
            params={"email": f"{ha_prefix}MEMBER@TENANT.TEST"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["accounts"]) == 1

    async def test_lookup_of_unknown_email_is_an_empty_list(
        self, operator_client, ha_prefix
    ):
        resp = await operator_client.get(
            "/api/organizations/instance/users/by-email",
            params={"email": f"{ha_prefix}nobody@nowhere.test"},
        )
        assert resp.status_code == 200
        assert resp.json()["accounts"] == []

    async def test_lookup_requires_instance_operator(
        self, tenant_owner_client, ha_tenant_member
    ):
        resp = await tenant_owner_client.get(
            "/api/organizations/instance/users/by-email",
            params={"email": ha_tenant_member["email"]},
        )
        assert resp.status_code == 403


class TestListFiltering:
    async def test_system_org_is_hidden(self, operator_client):
        resp = await operator_client.get("/api/organizations", params={"limit": 100})
        assert resp.status_code == 200
        names = [org["name"] for org in resp.json()["organizations"]]
        assert "__lucent_system__" not in names

    async def test_stats_are_included(self, operator_client, ha_tenant, ha_tenant_member):
        resp = await operator_client.get(
            "/api/organizations", params={"limit": 100, "search": ha_tenant["name"]}
        )
        assert resp.status_code == 200
        rows = [
            org for org in resp.json()["organizations"] if org["id"] == str(ha_tenant["id"])
        ]
        assert rows, "tenant must appear in the listing"
        row = rows[0]
        assert row["user_count"] == 1
        assert row["admin_count"] == 1
        assert row["owner_names"]

    async def test_search_filters(self, operator_client, ha_tenant, ha_prefix):
        resp = await operator_client.get(
            "/api/organizations", params={"search": f"{ha_prefix}tenant"}
        )
        assert resp.status_code == 200
        assert all(
            ha_prefix in org["name"] for org in resp.json()["organizations"]
        )

    async def test_status_filter(self, operator_client, ha_tenant, ha_prefix):
        await operator_client.post(f"/api/organizations/{ha_tenant['id']}/suspend")
        resp = await operator_client.get(
            "/api/organizations", params={"status": "suspended", "limit": 100}
        )
        assert resp.status_code == 200
        ids = [org["id"] for org in resp.json()["organizations"]]
        assert str(ha_tenant["id"]) in ids


class TestOrganizationMembers:
    async def test_list_members_excludes_daemon(
        self, operator_client, ha_tenant, ha_tenant_member
    ):
        resp = await operator_client.get(
            f"/api/organizations/{ha_tenant['id']}/users"
        )
        assert resp.status_code == 200
        members = resp.json()
        assert all(m["role"] != "daemon" for m in members)
        assert any(m["id"] == str(ha_tenant_member["id"]) for m in members)

    async def test_create_member_returns_temp_password(
        self, operator_client, ha_tenant, ha_prefix
    ):
        resp = await operator_client.post(
            f"/api/organizations/{ha_tenant['id']}/users",
            json={"email": f"{ha_prefix}newadmin@tenant.test", "role": "admin"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["user"]["role"] == "admin"
        assert data["temporary_password"]

    async def test_create_member_can_mint_key(
        self, operator_client, ha_tenant, ha_prefix
    ):
        resp = await operator_client.post(
            f"/api/organizations/{ha_tenant['id']}/users",
            params={"mint_api_key": "true"},
            json={"email": f"{ha_prefix}keyed@tenant.test", "role": "member"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["api_key"].startswith("hs_")

    async def test_create_member_rejects_hyperadmin_role(
        self, operator_client, ha_tenant, ha_prefix
    ):
        resp = await operator_client.post(
            f"/api/organizations/{ha_tenant['id']}/users",
            json={"email": f"{ha_prefix}esc@tenant.test", "role": "hyperadmin"},
        )
        assert resp.status_code == 400

    async def test_create_member_rejects_unknown_org(
        self, operator_client, ha_prefix
    ):
        resp = await operator_client.post(
            f"/api/organizations/{uuid4()}/users",
            json={"email": f"{ha_prefix}ghost@tenant.test"},
        )
        assert resp.status_code == 404

    async def test_role_change(self, operator_client, ha_tenant, ha_tenant_member):
        resp = await operator_client.patch(
            f"/api/organizations/{ha_tenant['id']}/users/{ha_tenant_member['id']}/role",
            json={"role": "admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    async def test_role_change_rejects_cross_org_user(
        self, operator_client, ha_tenant, ha_operator, ha_prefix
    ):
        """The user must belong to the organization in the path."""
        resp = await operator_client.patch(
            f"/api/organizations/{ha_tenant['id']}/users/{ha_operator['id']}/role",
            json={"role": "member"},
        )
        assert resp.status_code == 404

    async def test_reset_password_returns_usable_temp(
        self, operator_client, ha_tenant, ha_tenant_member
    ):
        resp = await operator_client.post(
            f"/api/organizations/{ha_tenant['id']}/users/"
            f"{ha_tenant_member['id']}/reset-password"
        )
        assert resp.status_code == 200
        message = resp.json()["message"]
        assert "Temporary password" in message

    async def test_mint_key_for_member(
        self, operator_client, ha_tenant, ha_tenant_member
    ):
        resp = await operator_client.post(
            f"/api/organizations/{ha_tenant['id']}/users/"
            f"{ha_tenant_member['id']}/api-keys",
            json={"name": "Console-minted"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["api_key"].startswith("hs_")

    async def test_cannot_remove_last_owner(
        self, operator_client, ha_tenant, ha_tenant_member
    ):
        resp = await operator_client.delete(
            f"/api/organizations/{ha_tenant['id']}/users/{ha_tenant_member['id']}"
        )
        assert resp.status_code == 400
        assert "only owner" in resp.json()["detail"]

    async def test_remove_member_when_another_owner_exists(
        self, operator_client, db_pool, ha_tenant, ha_tenant_member, ha_prefix
    ):
        from lucent.db import UserRepository

        # A tenant may have only one owner (idx_one_owner_per_org), so add a
        # second admin and transfer ownership before removing the original.
        successor = await UserRepository(db_pool).create(
            external_id=f"{ha_prefix}secondowner",
            provider="local",
            organization_id=ha_tenant["id"],
            email=f"{ha_prefix}second@tenant.test",
            role="admin",
        )
        repo = UserRepository(db_pool)
        await repo.transfer_ownership(
            organization_id=ha_tenant["id"],
            from_user_id=ha_tenant_member["id"],
            to_user_id=successor["id"],
        )

        resp = await operator_client.delete(
            f"/api/organizations/{ha_tenant['id']}/users/{ha_tenant_member['id']}"
        )
        assert resp.status_code == 200, resp.text


class TestOrganizationSettingsAPI:
    async def test_read_returns_editable_settings(
        self, operator_client, ha_tenant
    ):
        resp = await operator_client.get(
            f"/api/organizations/{ha_tenant['id']}/settings"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["organization_id"] == str(ha_tenant["id"])
        assert data["settings"]
        assert data["sections"]
        keys = {s["key"] for s in data["settings"]}
        # A known editable setting, and never a read-only credential entry.
        assert "features.sandboxes_enabled" in keys
        assert not any("password" in key for key in keys)
        # ``value`` must be the raw, round-trippable value; the human-readable
        # form belongs in ``display_value``.
        sandboxes = next(
            s for s in data["settings"] if s["key"] == "features.sandboxes_enabled"
        )
        assert isinstance(sandboxes["value"], bool)
        assert sandboxes["display_value"]

    async def test_write_applies_to_target_org(
        self, operator_client, db_pool, ha_tenant
    ):
        from lucent.db.runtime_settings import RuntimeSettingsRepository

        resp = await operator_client.put(
            f"/api/organizations/{ha_tenant['id']}/settings/features.sandboxes_enabled",
            json={"value": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "database"
        assert resp.json()["value"] is False

        row = await RuntimeSettingsRepository(db_pool).get_setting(
            ha_tenant["id"], "features.sandboxes_enabled"
        )
        assert row is not None
        assert row["value"] is False

    async def test_write_rejects_invalid_value(
        self, operator_client, ha_tenant
    ):
        resp = await operator_client.put(
            f"/api/organizations/{ha_tenant['id']}/settings/"
            "memory.search_vitality_boost_log_top_n",
            json={"value": 5000},
        )
        assert resp.status_code == 400

    async def test_write_rejects_unknown_setting(
        self, operator_client, ha_tenant
    ):
        resp = await operator_client.put(
            f"/api/organizations/{ha_tenant['id']}/settings/not.a.setting",
            json={"value": True},
        )
        assert resp.status_code == 404

    async def test_reset_falls_back(self, operator_client, db_pool, ha_tenant):
        key = "features.sandboxes_enabled"
        await operator_client.put(
            f"/api/organizations/{ha_tenant['id']}/settings/{key}",
            json={"value": False},
        )
        resp = await operator_client.put(
            f"/api/organizations/{ha_tenant['id']}/settings/{key}",
            json={"reset": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] in ("environment", "default")

        from lucent.db.runtime_settings import RuntimeSettingsRepository

        assert (
            await RuntimeSettingsRepository(db_pool).get_setting(ha_tenant["id"], key)
            is None
        )

    async def test_setting_change_is_audited_for_the_tenant(
        self, operator_client, ha_tenant
    ):
        await operator_client.put(
            f"/api/organizations/{ha_tenant['id']}/settings/features.sandboxes_enabled",
            json={"value": False},
        )
        resp = await operator_client.get(
            f"/api/organizations/{ha_tenant['id']}/audit"
        )
        assert resp.status_code == 200
        actions = [entry["action"] for entry in resp.json()["entries"]]
        assert "org.setting_update" in actions

    async def test_tenant_owner_cannot_read_settings_via_instance_route(
        self, tenant_owner_client, ha_tenant
    ):
        resp = await tenant_owner_client.get(
            f"/api/organizations/{ha_tenant['id']}/settings"
        )
        assert resp.status_code == 403


class TestSuspendResumeAudit:
    async def test_suspend_is_audited_in_both_orgs(
        self, operator_client, ha_operator, ha_tenant
    ):
        await operator_client.post(f"/api/organizations/{ha_tenant['id']}/suspend")

        tenant_trail = await operator_client.get(
            f"/api/organizations/{ha_tenant['id']}/audit"
        )
        assert "org.suspend" in [e["action"] for e in tenant_trail.json()["entries"]]

        instance_trail = await operator_client.get("/api/organizations/instance/audit")
        assert instance_trail.status_code == 200
        entries = instance_trail.json()["entries"]
        assert any(
            e["action"] == "org.suspend"
            and e["target_organization_id"] == str(ha_tenant["id"])
            for e in entries
        )

    async def test_suspended_tenant_members_cannot_authenticate(
        self, operator_client, db_pool, ha_tenant, ha_tenant_member, ha_prefix
    ):
        from lucent.auth_providers import (
            ApiKeyRepository,
            BasicAuthProvider,
            set_user_password,
        )

        await set_user_password(db_pool, ha_tenant_member["id"], "Valid-password-9")
        _, raw_key = await ApiKeyRepository(db_pool).create(
            ha_tenant_member["id"], ha_tenant["id"], f"{ha_prefix}pre-suspend"
        )

        await operator_client.post(f"/api/organizations/{ha_tenant['id']}/suspend")

        assert (
            await BasicAuthProvider(db_pool).authenticate(
                {
                    "username": f"{ha_prefix}member@tenant.test",
                    "password": "Valid-password-9",
                }
            )
            is None
        )
        assert await ApiKeyRepository(db_pool).verify(raw_key) is not None
        # ...the key itself is still valid; org status is what blocks it.
        from lucent.auth_providers import ApiKeyAuthProvider

        assert await ApiKeyAuthProvider(db_pool).authenticate({"api_key": raw_key}) is None

        await operator_client.post(f"/api/organizations/{ha_tenant['id']}/resume")
        assert (
            await BasicAuthProvider(db_pool).authenticate(
                {
                    "username": f"{ha_prefix}member@tenant.test",
                    "password": "Valid-password-9",
                }
            )
            is not None
        )


class TestDeleteOrganizationEndpoint:
    async def test_delete_requires_confirmation_name(
        self, operator_client, ha_tenant
    ):
        resp = await operator_client.delete(
            f"/api/organizations/{ha_tenant['id']}",
            params={"confirm_name": "wrong"},
        )
        assert resp.status_code == 400

    async def test_delete_removes_tenant(
        self, operator_client, db_pool, ha_tenant
    ):
        from lucent.db import OrganizationRepository

        resp = await operator_client.delete(
            f"/api/organizations/{ha_tenant['id']}",
            params={"confirm_name": ha_tenant["name"]},
        )
        assert resp.status_code == 200, resp.text
        assert await OrganizationRepository(db_pool).get_by_id(ha_tenant["id"]) is None

    async def test_confirmation_name_is_required(
        self, operator_client, ha_tenant
    ):
        resp = await operator_client.delete(f"/api/organizations/{ha_tenant['id']}")
        assert resp.status_code == 422

    async def test_tenant_owner_cannot_delete_other_org(
        self, tenant_owner_client, ha_tenant
    ):
        resp = await tenant_owner_client.delete(
            f"/api/organizations/{ha_tenant['id']}",
            params={"confirm_name": ha_tenant["name"]},
        )
        assert resp.status_code == 403


class TestInstanceAuditEndpoint:
    async def test_newest_first_and_filterable(
        self, operator_client, ha_tenant, ha_prefix
    ):
        await operator_client.post(f"/api/organizations/{ha_tenant['id']}/suspend")

        resp = await operator_client.get(
            "/api/organizations/instance/audit",
            params={"organization_id": str(ha_tenant["id"])},
        )
        assert resp.status_code == 200, resp.text
        entries = resp.json()["entries"]
        assert entries
        assert all(e["target_organization_id"] == str(ha_tenant["id"]) for e in entries)

        filtered = await operator_client.get(
            "/api/organizations/instance/audit", params={"action": "org.suspend"}
        )
        assert all(e["action"] == "org.suspend" for e in filtered.json()["entries"])

    async def test_owner_cannot_read_instance_audit(self, tenant_owner_client):
        resp = await tenant_owner_client.get("/api/organizations/instance/audit")
        assert resp.status_code == 403
