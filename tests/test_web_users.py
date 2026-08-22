"""Integration tests for user management web routes in web/routes.py.

Tests:
- GET  /users                          (list users, team mode only)
- POST /users/create                   (create user, admin/owner only)
- POST /users/{user_id}/impersonate    (start impersonation, admin/owner only)
- POST /users/stop-impersonation       (stop impersonation)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

import re
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.definitions import DefinitionRepository
from lucent.license import create_license
from lucent.mode import get_mode

TEST_PASSWORD = "TestPass1"
_TEST_LICENSE_PRIVATE_KEY = "eeddca8cb93457f6e6064745285738aef75c9a281d876677c2fb4690fca5b095"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web user tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webusr_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_hooks WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest.fixture
def team_mode(monkeypatch):
    """Enable licensed team mode for impersonation behavior tests."""
    monkeypatch.setenv("LUCENT_MODE", "team")
    monkeypatch.setenv(
        "LUCENT_LICENSE_KEY",
        create_license(_TEST_LICENSE_PRIVATE_KEY, "test-org", max_users=10),
    )
    get_mode.cache_clear()
    yield
    get_mode.cache_clear()


@pytest_asyncio.fixture
async def owner_user(db_pool, web_prefix):
    """Create an owner user + org for web tests and return (user, org, session_token)."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}owner",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}owner@test.com",
        display_name=f"{web_prefix}Owner",
    )
    await user_repo.update_role(user["id"], "owner")
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def member_user(db_pool, web_prefix, owner_user):
    """Create a member user in the same org as the owner."""
    _, org, _ = owner_user
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}member",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}member@test.com",
        display_name=f"{web_prefix}Member",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, owner_user):
    """httpx client with owner session + CSRF cookies pre-set."""
    _user, _org, session_token = owner_user
    csrf_token = "test-csrf-token-usr123"

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://test",
    ) as c:
        c.cookies.set(SESSION_COOKIE_NAME, session_token, domain="test.local", path="/")
        c.cookies.set(CSRF_COOKIE_NAME, csrf_token, domain="test.local", path="/")
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


@pytest_asyncio.fixture
async def member_client(db_pool, member_user):
    """httpx client with member session + CSRF cookies pre-set."""
    _user, _org, session_token = member_user
    csrf_token = "test-csrf-token-mbr123"

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://test",
    ) as c:
        c.cookies.set(SESSION_COOKIE_NAME, session_token, domain="test.local", path="/")
        c.cookies.set(CSRF_COOKIE_NAME, csrf_token, domain="test.local", path="/")
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /settings/users — list
# ============================================================================


@pytest.mark.asyncio
async def test_users_list_renders(client):
    """With an owner user, GET /settings/users returns 200."""
    resp = await client.get("/settings/users")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_users_list_not_team_mode_returns_404(client):
    """Legacy root /users path is no longer mounted."""
    resp = await client.get("/users")
    assert resp.status_code == 404


# ============================================================================
# POST /settings/users/create
# ============================================================================


@pytest.mark.asyncio
@patch("secrets.token_urlsafe", return_value="TempPass1safe")
async def test_create_user_as_owner(_mock_pw, client):
    """Owner can create a new user; expect 303 redirect to settings users."""
    resp = await client.post(
        "/settings/users/create",
        data=_csrf_data(
            client,
            {
                "display_name": "New User",
                "email": "newuser@test.com",
                "role": "member",
            },
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/settings/users" in resp.headers["location"]
    assert "success=" in resp.headers["location"]


@pytest.mark.asyncio
async def test_create_user_without_permission_returns_403(member_client):
    """A member user cannot create users; expect 403."""
    resp = await member_client.post(
        "/settings/users/create",
        data=_csrf_data(
            member_client,
            {
                "display_name": "Sneaky User",
                "email": "sneaky@test.com",
                "role": "member",
            },
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ============================================================================
# POST /settings/users/{user_id}/impersonate
# ============================================================================


@pytest.mark.asyncio
async def test_impersonate_user(client, member_user, team_mode):
    """Owner can impersonate a member; expect 303 redirect."""
    target_user, _, _ = member_user
    resp = await client.post(
        f"/settings/users/{target_user['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "impersonating" in resp.headers["location"]


@pytest.mark.asyncio
async def test_impersonation_follows_redirect_as_target_user(client, member_user, team_mode):
    """The signed impersonation cookie must take effect after the redirect."""
    target_user, _, _ = member_user
    response = await client.post(
        f"/settings/users/{target_user['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Impersonating" in response.text
    assert target_user["display_name"] in response.text


@pytest.mark.asyncio
async def test_impersonated_badge_counts_only_target_proposals(
    client, db_pool, owner_user, member_user, team_mode
):
    """The sidebar proposal count must use the impersonated user's visibility."""
    owner, org, _ = owner_user
    member, _, _ = member_user
    repo = DefinitionRepository(db_pool)

    for index in range(5):
        await repo.create_agent(
            name=f"Owner proposal {uuid4()} {index}",
            description="Owner-only proposal",
            content="# Owner proposal",
            org_id=str(org["id"]),
            created_by=str(owner["id"]),
        )
    await repo.create_agent(
        name=f"Member proposal {uuid4()}",
        description="Member-visible proposal",
        content="# Member proposal",
        org_id=str(org["id"]),
        created_by=str(member["id"]),
    )

    await client.post(
        f"/settings/users/{member['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    response = await client.get("/activity")

    assert response.status_code == 200
    badge = re.search(
        r'id="definition-proposal-sidebar-badge"[^>]*>\s*(\d+)\s*<',
        response.text,
    )
    assert badge is not None
    assert badge.group(1) == "1"


@pytest.mark.asyncio
async def test_impersonate_self_redirects_with_error(client, owner_user, team_mode):
    """Owner cannot impersonate themselves; expect settings users error redirect."""
    owner, _, _ = owner_user
    resp = await client.post(
        f"/settings/users/{owner['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


@pytest.mark.asyncio
async def test_personal_mode_impersonation_follows_redirect_as_target_user(client, member_user):
    """Personal mode honors the same signed impersonation flow as team mode."""
    target_user, _, _ = member_user

    members = await client.get("/settings/users")
    response = await client.post(
        f"/settings/users/{target_user['id']}/impersonate",
        data=_csrf_data(client),
        follow_redirects=True,
    )

    assert members.status_code == 200
    assert "Impersonate" in members.text
    assert response.status_code == 200
    assert "Impersonating" in response.text
    assert target_user["display_name"] in response.text


# ============================================================================
# POST /settings/users/stop-impersonation
# ============================================================================


@pytest.mark.asyncio
async def test_stop_impersonation(client):
    """POST /settings/users/stop-impersonation returns 303 redirect to settings users."""
    resp = await client.post(
        "/settings/users/stop-impersonation",
        data=_csrf_data(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/settings/users" in resp.headers["location"]
