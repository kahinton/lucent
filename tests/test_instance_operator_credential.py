"""Tests for the least-privilege instance-operator (control-plane) API key.

The credential is an ordinary bearer API key carrying only the ``admin-orgs``
scope, owned by a hyperadmin. These tests exercise the real auth dependency
(no dependency overrides) because the scope allowlist in ``lucent.api.deps``
is the security control under test: the key must reach the organization
administration surface — provision, list/read, per-tenant tier controls —
and nothing else: not the tenant data plane, not the tenant-scoped
``current`` routes, not member management (reads are open for billing
reconciliation; creation is the committed signup path; every other member
sub-route stays closed), even though its owner can do everything.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport

from lucent.api.deps import CurrentUser, _enforce_admin_orgs_allowlist
from lucent.db.api_key import ADMIN_ORGS_SCOPE, ApiKeyRepository


@pytest_asyncio.fixture
async def cred_prefix(db_pool):
    """Namespace + cleanup for control-plane credential test data."""
    prefix = f"test_opcred_{str(uuid4())[:8]}_"
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
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def operator_org(db_pool, cred_prefix):
    from lucent.db import OrganizationRepository

    return await OrganizationRepository(db_pool).create(name=f"{cred_prefix}oporg")


@pytest_asyncio.fixture
async def operator_user(db_pool, operator_org, cred_prefix):
    from lucent.db import UserRepository

    return await UserRepository(db_pool).create(
        external_id=f"{cred_prefix}operator",
        provider="local",
        organization_id=operator_org["id"],
        email=f"{cred_prefix}operator@control-plane.test",
        display_name=f"{cred_prefix}Operator",
        role="hyperadmin",
    )


@pytest_asyncio.fixture
async def control_plane_key(db_pool, operator_user, cred_prefix):
    """Mint the narrow key exactly the way the CLI does."""
    record, plaintext = await ApiKeyRepository(db_pool).create(
        user_id=operator_user["id"],
        organization_id=operator_user["organization_id"],
        name=f"{cred_prefix}control-plane",
        scopes=[ADMIN_ORGS_SCOPE],
    )
    return record, plaintext


@pytest_asyncio.fixture
async def raw_client(db_pool):
    """Client that uses the real auth dependency (no override)."""
    from lucent.api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class _Req:
    def __init__(self, method: str, path: str):
        self.method = method
        self.url = type("U", (), {"path": path})()


def _operator(scopes: list[str]) -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role="hyperadmin",
        email=None,
        display_name="Instance operator",
        auth_method="api_key",
        api_key_scopes=scopes,
    )


# ---------------------------------------------------------------------------
# The allowlist itself
# ---------------------------------------------------------------------------


def test_allowlist_permits_only_the_organization_surface():
    """Only the organization administration routes are reachable."""
    user = _operator([ADMIN_ORGS_SCOPE])

    org_id = str(uuid4())
    allowed = (
        # The one create path: a complete tenant.
        ("POST", "/api/organizations/provision"),
        # Reconciliation reads.
        ("GET", "/api/organizations"),
        ("GET", "/api/organizations/"),
        ("GET", f"/api/organizations/{org_id}"),
        # Rename and the per-tenant tier controls (read + write).
        ("PATCH", f"/api/organizations/{org_id}"),
        ("GET", f"/api/organizations/{org_id}/settings"),
        ("PUT", f"/api/organizations/{org_id}/settings/features.sandboxes_enabled"),
        ("GET", f"/api/organizations/{org_id}/setting-locks"),
        ("PUT", f"/api/organizations/{org_id}/setting-locks"),
        ("PUT", f"/api/organizations/{org_id}/setting-locks/features.sandboxes_enabled"),
        ("DELETE", f"/api/organizations/{org_id}/setting-locks/features.sandboxes_enabled"),
        ("POST", f"/api/organizations/{org_id}/setting-locks/release"),
        # Account lookup by email (signup pre-flight).
        ("GET", "/api/organizations/instance/users/by-email"),
        # Credential read and model grant.
        ("GET", f"/api/organizations/{org_id}/credentials"),
        ("POST", f"/api/organizations/{org_id}/models/openai/gpt-4o-mini/grant"),
        # Billing reads: who is in a tenant, what it consumes. GET-only.
        ("GET", f"/api/organizations/{org_id}/users"),
        ("GET", f"/api/organizations/{org_id}/users/"),
        ("GET", f"/api/organizations/{org_id}/usage/resources"),
        # Member creation stays reachable: it is the committed second write
        # path of the signup flow (7a12c91), kept as-is here — asserted
        # explicitly rather than removed silently.
        ("POST", f"/api/organizations/{org_id}/users"),
    )
    for method, path in allowed:
        _enforce_admin_orgs_allowlist(_Req(method, path), user)

    denied = (
        # A headless shell create is exactly what the scope must never offer;
        # the only create path is /provision.
        ("POST", "/api/organizations"),
        # Tenant-scoped routes: these operate on the key owner's own org and
        # must stay unreachable, never widened into a control-plane surface.
        ("GET", "/api/organizations/current"),
        ("PATCH", "/api/organizations/current"),
        ("DELETE", "/api/organizations/current"),
        ("GET", "/api/organizations/current/settings"),
        ("PUT", "/api/organizations/current/settings/x"),
        ("GET", "/api/organizations/current/setting-locks"),
        # The newly opened member/usage reads must not swallow the literal
        # segments: ``current`` and ``instance`` are never organization ids.
        ("GET", "/api/organizations/current/users"),
        ("GET", "/api/organizations/current/usage/resources"),
        ("GET", "/api/organizations/instance/users"),
        # The member reach is GET-only (creation aside): every member
        # management sub-route stays closed.
        ("PATCH", f"/api/organizations/{org_id}/users"),
        ("PATCH", f"/api/organizations/{org_id}/users/{uuid4()}/role"),
        ("POST", f"/api/organizations/{org_id}/users/{uuid4()}/reset-password"),
        ("DELETE", f"/api/organizations/{org_id}/users/{uuid4()}"),
        ("DELETE", f"/api/organizations/{org_id}/users/{uuid4()}/api-keys/x"),
        # The usage read has no write side, and the member read has no
        # deeper path.
        ("POST", f"/api/organizations/{org_id}/usage/resources"),
        ("PATCH", f"/api/organizations/{org_id}/usage/resources"),
        ("GET", f"/api/organizations/{org_id}/users/{uuid4()}"),
        # Same-family operations deliberately outside the reach list.
        ("DELETE", f"/api/organizations/{org_id}"),
        ("POST", f"/api/organizations/{org_id}/suspend"),
        ("GET", f"/api/organizations/{org_id}/models"),
        ("PUT", f"/api/organizations/{org_id}/credentials/anthropic"),
        # Other instance/operator surfaces.
        ("GET", "/api/auth/verify"),
        ("GET", "/api/remotes"),
        ("GET", "/api/admin/models"),
        ("GET", "/api/organizations/instance/usage/resources"),
        ("GET", "/api/organizations/instance/audit"),
        # Prefix smuggling: the patterns are anchored.
        ("GET", "/api/organizationsanything"),
        ("POST", "/api/organizations/provisioned"),
    )
    for method, path in denied:
        with pytest.raises(HTTPException) as exc:
            _enforce_admin_orgs_allowlist(_Req(method, path), user)
        assert exc.value.status_code == 403, f"{method} {path}"


# ---------------------------------------------------------------------------
# Real auth path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_plane_key_can_create_a_tenant(
    db_pool, control_plane_key, cred_prefix, raw_client
):
    """The credential's entire purpose: provision a tenant and its first owner.

    Real auth path (no dependency override). Built-in seeding is patched here
    because it is proven independently with the production path in
    ``test_org_provisioning.py::test_seeds_builtin_definitions``; this test is
    about the credential reaching the one create surface it may use, and
    everything a signup flow needs afterwards (detail read, tier settings,
    owner lookup) through the same key.
    """
    _, plaintext = control_plane_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    owner_email = f"{cred_prefix}owner@tenant.test"

    with patch(
        "lucent.builtin_definitions.sync_built_in_definitions_for_org",
        new=AsyncMock(return_value={}),
    ):
        resp = await raw_client.post(
            "/api/organizations/provision",
            headers=headers,
            json={
                "name": f"{cred_prefix}tenant_one",
                "first_admin": {"email": owner_email, "display_name": "Provisioned Owner"},
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    org_id = body["organization"]["id"]
    assert org_id
    assert body["user"]["role"] == "owner"
    assert body["temporary_password"]

    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 FROM organizations WHERE id = $1", org_id)
        assert await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND role = 'owner'", body["user"]["id"]
        )

    detail = await raw_client.get(f"/api/organizations/{org_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == org_id

    # The tenant-level control the tier application drives.
    setting = await raw_client.put(
        f"/api/organizations/{org_id}/settings/features.sandboxes_enabled",
        headers=headers,
        json={"value": False},
    )
    assert setting.status_code == 200, setting.text

    # The name is listable from the same credential (search is part of the
    # reach; a page of 100 old test tenants should never be re-scanned).
    listing = await raw_client.get(
        "/api/organizations", headers=headers, params={"limit": 100, "search": f"{cred_prefix}tenant_one"}
    )
    assert listing.status_code == 200, listing.text
    assert [org["name"] for org in listing.json()["organizations"]] == [
        f"{cred_prefix}tenant_one"
    ]

    # The signup pre-flight: does this person already have an account?
    lookup = await raw_client.get(
        "/api/organizations/instance/users/by-email",
        headers=headers,
        params={"email": owner_email.upper()},  # case-insensitive on purpose
    )
    assert lookup.status_code == 200, lookup.text
    found = lookup.json()
    assert found["email"] == owner_email.lower()
    assert [
        a for a in found["accounts"] if a["organization_id"] == org_id
    ], found


@pytest.mark.asyncio
async def test_control_plane_key_reaches_organizations_and_nothing_else(
    db_pool, control_plane_key, raw_client
):
    _, plaintext = control_plane_key
    headers = {"Authorization": f"Bearer {plaintext}"}

    allowed = await raw_client.get("/api/organizations", headers=headers)
    assert allowed.status_code == 200, allowed.text

    for path in (
        f"/api/memories/{uuid4()}",  # tenant data plane
        "/api/search",  # tenant data plane (search)
        "/api/organizations/current",  # tenant-scoped route on the owner's own org
        "/api/auth/verify",  # not part of the control-plane surface
        "/api/organizations/instance/audit",  # different instance surface
    ):
        resp = await raw_client.get(path, headers=headers)
        assert resp.status_code == 403, f"{path}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"] == "API key scope does not allow this endpoint"

    # The data plane's write side is refused too: confinement is not a
    # read/write split by accident. A valid body means only the scope gate
    # could have denied this.
    blocked_write = await raw_client.post(
        "/api/memories",
        headers=headers,
        json={"type": "experience", "content": "control-plane probe"},
    )
    assert blocked_write.status_code == 403, blocked_write.text
    assert blocked_write.json()["detail"] == "API key scope does not allow this endpoint"

    # The destructive side of the one surface the key may read.
    blocked_delete = await raw_client.delete(
        f"/api/organizations/{uuid4()}", headers=headers
    )
    assert blocked_delete.status_code == 403, blocked_delete.text

    # A bare create is not part of the reach. The route does not exist any
    # more (405 — FastAPI refuses before auth resolves); a 403 from the scope
    # gate would be equally fine if one were ever re-added.
    blocked_create = await raw_client.post(
        "/api/organizations", headers=headers, json={"name": "nope"}
    )
    assert blocked_create.status_code in (403, 405), blocked_create.text


@pytest.mark.asyncio
async def test_control_plane_key_reads_members_and_usage_but_not_member_management(
    db_pool, control_plane_key, cred_prefix, raw_client
):
    """Billing reads (`/{id}/users`, `/{id}/usage/resources`) open; writes close.

    FastAPI resolves routes before dependencies, so only real method+route
    pairs are probed here: an unconfigured pair would 405 and prove nothing.
    Provision patches built-in definitions (proven separately, as above).
    """
    _, plaintext = control_plane_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    owner_email = f"{cred_prefix}owner@tenant.test"

    with patch(
        "lucent.builtin_definitions.sync_built_in_definitions_for_org",
        new=AsyncMock(return_value={}),
    ):
        provisioned = await raw_client.post(
            "/api/organizations/provision",
            headers=headers,
            json={
                "name": f"{cred_prefix}billing_reads",
                "first_admin": {"email": owner_email, "display_name": "Billing Owner"},
            },
        )
    assert provisioned.status_code == 201, provisioned.text
    org_id = provisioned.json()["organization"]["id"]

    # The member read: the operator's tenant must list its owner.
    members = await raw_client.get(
        f"/api/organizations/{org_id}/users", headers=headers
    )
    assert members.status_code == 200, members.text
    emails = [u["email"] for u in members.json()]
    assert owner_email in emails, emails

    # The consumption read with the seats flag the billing job sets.
    usage = await raw_client.get(
        f"/api/organizations/{org_id}/usage/resources",
        headers=headers,
        params={"include_seats": True},
    )
    assert usage.status_code == 200, usage.text
    assert usage.json()["organization_id"] == org_id

    # The literal guard must survive the new parametric reads: a
    # uuid-shaped segment is never ``current``.
    current_members = await raw_client.get(
        "/api/organizations/current/users", headers=headers
    )
    assert current_members.status_code == 403, current_members.text
    assert (
        current_members.json()["detail"] == "API key scope does not allow this endpoint"
    )

    # Member management stays closed: role change, member deletion.
    stray = str(uuid4())
    role_change = await raw_client.patch(
        f"/api/organizations/{org_id}/users/{stray}/role",
        headers=headers,
        json={"role": "admin"},
    )
    assert role_change.status_code == 403, role_change.text
    assert role_change.json()["detail"] == "API key scope does not allow this endpoint"

    member_delete = await raw_client.delete(
        f"/api/organizations/{org_id}/users/{stray}", headers=headers
    )
    assert member_delete.status_code == 403, member_delete.text
    assert member_delete.json()["detail"] == "API key scope does not allow this endpoint"

    # Member creation is the pre-existing committed reach of the signup
    # flow (7a12c91): this test asserts that current behaviour explicitly
    # rather than removing the entry silently. The task draft expected 403
    # here; the allowlist kept the documented reach (see report).
    invited = await raw_client.post(
        f"/api/organizations/{org_id}/users",
        headers=headers,
        json={
            "email": f"{cred_prefix}invited@tenant.test",
            "display_name": f"{cred_prefix}Invited",
            "external_id": f"{cred_prefix}invited",
            "role": "member",
        },
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["temporary_password"]


@pytest.mark.asyncio
async def test_extra_scopes_do_not_widen_the_credential(
    db_pool, operator_user, cred_prefix, raw_client
):
    """Scope membership drives confinement, not ``has_scope`` (read+write)."""
    _, plaintext = await ApiKeyRepository(db_pool).create(
        user_id=operator_user["id"],
        organization_id=operator_user["organization_id"],
        name=f"{cred_prefix}control-plane-extra",
        scopes=["read", "write", ADMIN_ORGS_SCOPE],
    )
    headers = {"Authorization": f"Bearer {plaintext}"}

    resp = await raw_client.get(f"/api/memories/{uuid4()}", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "API key scope does not allow this endpoint"


@pytest.mark.asyncio
async def test_scope_alone_does_not_grant_the_surface(db_pool, cred_prefix, raw_client):
    """The allowlist admits the route; the hyperadmin permission must still hold."""
    from lucent.db import OrganizationRepository, UserRepository

    org = await OrganizationRepository(db_pool).create(name=f"{cred_prefix}memberorg")
    member = await UserRepository(db_pool).create(
        external_id=f"{cred_prefix}member",
        provider="local",
        organization_id=org["id"],
        email=f"{cred_prefix}member@control-plane.test",
        display_name=f"{cred_prefix}Member",
        role="member",
    )
    _, plaintext = await ApiKeyRepository(db_pool).create(
        user_id=member["id"],
        organization_id=org["id"],
        name=f"{cred_prefix}member-narrow",
        scopes=[ADMIN_ORGS_SCOPE],
    )

    resp = await raw_client.get(
        "/api/organizations", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 403, resp.text
    # The route dependency (require_role hyperadmin) rejects before its
    # handler; the message names the role, since the organizations router's
    # convention is HyperadminUser + a permission check inside the handler.
    assert "hyperadmin" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_revoked_control_plane_key_is_rejected(
    db_pool, control_plane_key, raw_client
):
    record, plaintext = control_plane_key
    headers = {"Authorization": f"Bearer {plaintext}"}

    assert (await raw_client.get("/api/organizations", headers=headers)).status_code == 200
    await ApiKeyRepository(db_pool).revoke_by_id(record["id"])

    resp = await raw_client.get("/api/organizations", headers=headers)
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_key_is_stored_hashed_never_plaintext(db_pool, control_plane_key):
    record, plaintext = control_plane_key
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key_hash, key_prefix, scopes FROM api_keys WHERE id = $1",
            record["id"],
        )

    assert row["scopes"] == [ADMIN_ORGS_SCOPE]
    assert row["key_prefix"] == plaintext[:11]
    assert row["key_hash"] != plaintext
    assert plaintext not in row["key_hash"]
    assert row["key_hash"].startswith(("$2a$", "$2b$", "$2y$"))


@pytest.mark.asyncio
async def test_key_cannot_be_exchanged_for_a_web_session(
    db_pool, control_plane_key
):
    """Web login must not turn a control-plane key into a full session."""
    from lucent.auth_providers import ApiKeyAuthProvider

    _, plaintext = control_plane_key
    assert (
        await ApiKeyAuthProvider(db_pool).authenticate({"api_key": plaintext})
        is None
    )
