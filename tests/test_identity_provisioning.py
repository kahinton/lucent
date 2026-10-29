"""Integration tests for invitations, signup, and SSO provisioning.

These exercise the real database and the real ASGI stack. The properties under
test are the ones where a mistake is a security incident rather than a bug:

* an invitation link is single-use, expiring, and bound to one mailbox;
* a self-serve signup creates an account with the user's own password and never
  forces a reset;
* an SSO identity cannot adopt an unverified account, cross a tenant boundary,
  or become an instance operator;
* a SCIM token is tenant-scoped and cannot touch daemons or owners.
"""

import secrets
from datetime import datetime, timedelta, timezone

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
from lucent.db import OrganizationRepository, ScimTokenRepository, UserRepository
from lucent.db.identity import (
    InvitationRepository,
    SsoConnectionRepository,
    generate_invitation_token,
    hash_invitation_token,
)
from lucent.identity.provisioning import (
    AssertedIdentity,
    FederatedIdentityError,
    resolve_federated_user,
)
from lucent.services import identity as identity_service

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def org_and_owner(db_pool, cleanup_test_organizations):
    """A tenant with one owner, plus FK-safe cleanup by name prefix.

    Cleanup goes through the shared ``cleanup_test_organizations`` helper rather
    than ad-hoc DELETEs: an organization deleted directly can be blocked by (or
    silently cascade) unrelated rows, and a suite that leaves tenants behind
    perturbs later tests that count organizations.
    """
    prefix = f"test_ident_{secrets.token_hex(4)}_"
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{prefix}org")

    user_repo = UserRepository(db_pool)
    owner = await user_repo.create(
        external_id=f"{prefix}owner",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}owner@test.com",
        display_name=f"{prefix}Owner",
        role="owner",
        email_verified=True,
    )
    await set_user_password(db_pool, owner["id"], TEST_PASSWORD)

    yield org, owner, prefix

    await cleanup_test_organizations([f"{prefix}%"])
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def public_client(db_pool):
    """An unauthenticated client — the only thing an invitee has.

    Depends on ``db_pool`` on purpose: every route these tests reach consults
    the database, and the conftest marker classifier uses the fixture graph to
    decide whether a test belongs to the integration suite. Without this
    dependency the tests would be collected as unit tests and fail on an
    uninitialized pool.
    """
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        yield client


@pytest_asyncio.fixture
async def owner_client(db_pool, org_and_owner):
    _org, owner, _prefix = org_and_owner
    token = await create_session(db_pool, owner["id"])
    csrf = secrets.token_urlsafe(24)
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        client.cookies.set(SESSION_COOKIE_NAME, token, domain="test.local", path="/")
        client.cookies.set(CSRF_COOKIE_NAME, csrf, domain="test.local", path="/")
        client._csrf_token = csrf
        yield client


# ============================================================================
# Invitations
# ============================================================================


@pytest.mark.asyncio
async def test_invitation_round_trip_creates_account_without_forced_reset(
    db_pool, org_and_owner
):
    org, owner, prefix = org_and_owner
    email = f"{prefix}invitee@test.com"

    record, token = await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=email,
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
        ttl_hours=24,
    )
    assert record["email"] == email.lower()

    user = await identity_service.redeem_invitation(
        db_pool,
        token=token,
        display_name="Invitee",
        password="InviteePass1",
    )
    assert user["email"] == email.lower()
    assert user["role"] == "member"
    # The invitee chose their own password, so no forced reset may be set.
    assert await UserRepository(db_pool).is_force_password_change(user["id"]) is False


@pytest.mark.asyncio
async def test_invitation_is_single_use(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    _record, token = await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=f"{prefix}once@test.com",
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
    )

    await identity_service.redeem_invitation(
        db_pool, token=token, display_name="First", password="FirstPass1"
    )

    with pytest.raises(identity_service.InvitationError):
        await identity_service.redeem_invitation(
            db_pool, token=token, display_name="Second", password="SecondPass1"
        )


@pytest.mark.asyncio
async def test_expired_invitation_is_rejected(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    token = generate_invitation_token()
    # Insert directly with a past expiry: the service clamps ttl to >= 1 hour.
    await InvitationRepository(db_pool).create(
        organization_id=org["id"],
        email=f"{prefix}expired@test.com",
        role="member",
        token_hash=hash_invitation_token(token),
        invited_by=owner["id"],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    assert await identity_service.resolve_invitation(db_pool, token) is None
    with pytest.raises(identity_service.InvitationError):
        await identity_service.redeem_invitation(
            db_pool, token=token, display_name="Late", password="LatePass1"
        )


@pytest.mark.asyncio
async def test_revoked_invitation_is_rejected(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    record, token = await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=f"{prefix}revoked@test.com",
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
    )
    await InvitationRepository(db_pool).revoke(record["id"], organization_id=org["id"])

    assert await identity_service.resolve_invitation(db_pool, token) is None


@pytest.mark.asyncio
async def test_invitation_token_is_not_stored_in_plaintext(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    _record, token = await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=f"{prefix}hash@test.com",
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
    )
    async with db_pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT token_hash FROM identity_invitations WHERE organization_id = $1",
            str(org["id"]),
        )
    # The raw token must be unrecoverable from the database.
    assert stored != token
    assert token not in (stored or "")
    assert stored == hash_invitation_token(token)


@pytest.mark.asyncio
async def test_admin_cannot_invite_above_own_role(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    user_repo = UserRepository(db_pool)
    admin = await user_repo.create(
        external_id=f"{prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}admin@test.com",
        display_name=f"{prefix}Admin",
        role="admin",
    )

    with pytest.raises(identity_service.InvitationError, match="above your own"):
        await identity_service.create_invitation(
            db_pool,
            organization_id=org["id"],
            email=f"{prefix}escalate@test.com",
            role="owner",
            invited_by=admin["id"],
            inviter_role="admin",
        )


@pytest.mark.asyncio
async def test_cannot_invite_existing_member(db_pool, org_and_owner):
    org, owner, _prefix = org_and_owner
    with pytest.raises(identity_service.InvitationError, match="already belongs"):
        await identity_service.create_invitation(
            db_pool,
            organization_id=org["id"],
            email=owner["email"],
            role="member",
            invited_by=owner["id"],
            inviter_role="owner",
        )


@pytest.mark.asyncio
async def test_duplicate_pending_invitation_rejected(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    email = f"{prefix}dup@test.com"
    await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=email,
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
    )
    with pytest.raises(identity_service.InvitationError, match="already a pending"):
        await identity_service.create_invitation(
            db_pool,
            organization_id=org["id"],
            email=email,
            role="member",
            invited_by=owner["id"],
            inviter_role="owner",
        )


@pytest.mark.asyncio
async def test_invitation_email_is_normalized(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    record, _token = await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=f"  {prefix}MiXeD@Test.COM  ",
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
    )
    assert record["email"] == f"{prefix}mixed@test.com"


# ============================================================================
# Invitation web flow
# ============================================================================


@pytest.mark.asyncio
async def test_invite_page_renders_for_valid_token(
    db_pool, org_and_owner, public_client
):
    org, owner, prefix = org_and_owner
    _record, token = await identity_service.create_invitation(
        db_pool,
        organization_id=org["id"],
        email=f"{prefix}page@test.com",
        role="member",
        invited_by=owner["id"],
        inviter_role="owner",
    )

    response = await public_client.get(f"/invite/{token}")
    assert response.status_code == 200
    assert f"{prefix}page@test.com" in response.text


@pytest.mark.asyncio
async def test_invite_page_returns_410_for_bad_token(public_client):
    response = await public_client.get("/invite/li_not-a-real-token")
    assert response.status_code == 410


@pytest.mark.asyncio
async def test_login_page_lists_enabled_sso_connection(
    db_pool, org_and_owner, public_client
):
    org, _owner, prefix = org_and_owner
    repo = SsoConnectionRepository(db_pool)
    connection = await repo.create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"{prefix}okta".replace("_", "-").rstrip("-"),
        display_name=f"{prefix}Okta",
        issuer="https://example.okta.com",
        client_id="client-abc",
    )
    assert connection["is_enabled"] is True

    response = await public_client.get("/login")
    assert response.status_code == 200
    assert f"{prefix}Okta" in response.text
    assert f"/sso/{connection['slug']}/start" in response.text

    # Disabling it removes the button.
    await repo.update(connection["id"], organization_id=org["id"], is_enabled=False)
    response = await public_client.get("/login")
    assert f"{prefix}Okta" not in response.text


@pytest.mark.asyncio
async def test_sso_start_rejects_unknown_slug(public_client):
    response = await public_client.get("/sso/does-not-exist/start", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


@pytest.mark.asyncio
async def test_signup_disabled_redirects_to_login(public_client, monkeypatch):
    monkeypatch.delenv("LUCENT_SELF_SIGNUP_ENABLED", raising=False)
    response = await public_client.get("/signup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_signup_creates_tenant_with_user_chosen_password(
    db_pool, public_client, monkeypatch, cleanup_test_organizations
):
    """The whole point of self-serve signup: no temporary credential, no reset.

    The user types their own password, the account is created with it, and they
    are signed straight in. ``force_password_change`` must therefore be false —
    if it were set, the first page load would bounce them to the reset screen,
    which is the flow this feature exists to replace.
    """
    monkeypatch.setenv("LUCENT_SELF_SIGNUP_ENABLED", "true")
    prefix = f"test_signup_{secrets.token_hex(4)}_"
    email = f"{prefix}founder@test.com"

    csrf = secrets.token_urlsafe(24)
    public_client.cookies.set(CSRF_COOKIE_NAME, csrf, domain="test.local", path="/")

    response = await public_client.post(
        "/signup",
        data={
            CSRF_FIELD_NAME: csrf,
            "organization_name": f"{prefix}org",
            "display_name": "Founder",
            "email": email,
            "password": "SignupPass1",
            "confirm_password": "SignupPass1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # A session cookie is issued, so the person is logged in immediately.
    assert SESSION_COOKIE_NAME in response.cookies

    user = await UserRepository(db_pool).get_by_email_in_organization(
        email, (await UserRepository(db_pool).get_by_external_id(email, "local"))["organization_id"]
    )
    assert user is not None
    assert user["role"] == "owner"
    assert user["provisioned_by"] == "signup"
    assert await UserRepository(db_pool).is_force_password_change(user["id"]) is False
    # The password they chose actually works for a later login.
    from lucent.auth_providers import BasicAuthProvider

    authenticated = await BasicAuthProvider(db_pool).authenticate(
        {"username": email, "password": "SignupPass1"}
    )
    assert authenticated is not None

    await cleanup_test_organizations([f"{prefix}%"])


@pytest.mark.asyncio
async def test_signup_rejects_weak_password(db_pool, public_client, monkeypatch):
    monkeypatch.setenv("LUCENT_SELF_SIGNUP_ENABLED", "true")
    prefix = f"test_signup_{secrets.token_hex(4)}_"
    csrf = secrets.token_urlsafe(24)
    public_client.cookies.set(CSRF_COOKIE_NAME, csrf, domain="test.local", path="/")

    response = await public_client.post(
        "/signup",
        data={
            CSRF_FIELD_NAME: csrf,
            "organization_name": f"{prefix}weak",
            "email": f"{prefix}weak@test.com",
            "password": "short",
            "confirm_password": "short",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/signup?error=" in response.headers["location"]
    # No tenant may be created for a rejected signup.
    async with db_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM organizations WHERE name LIKE $1", f"{prefix}%"
            )
            == 0
        )


# ============================================================================
# Federated identity resolution
# ============================================================================


@pytest_asyncio.fixture
async def oidc_connection(db_pool, org_and_owner):
    org, _owner, _prefix = org_and_owner
    return await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"conn-{secrets.token_hex(4)}",
        display_name="Test IdP",
        issuer="https://idp.test",
        client_id="cid",
        auto_create_users=True,
        default_role="member",
        allow_email_linking=True,
    )


def _identity(subject="sub-1", email="new@test.com", verified=True, groups=()):
    return AssertedIdentity(
        subject=subject,
        email=email,
        email_verified=verified,
        display_name="New Person",
        groups=tuple(groups),
    )


@pytest.mark.asyncio
async def test_jit_provisioning_creates_member(
    db_pool, org_and_owner, oidc_connection
):
    org, _owner, prefix = org_and_owner
    user = await resolve_federated_user(
        db_pool,
        connection=oidc_connection,
        identity=_identity(subject=f"{prefix}sub", email=f"{prefix}new@test.com"),
    )
    assert user["organization_id"] == org["id"]
    assert user["role"] == "member"
    assert user["provider"] == "oidc"
    assert user["provisioned_by"] == "sso"
    assert user["email_verified"] is True


@pytest.mark.asyncio
async def test_repeat_login_reuses_account(db_pool, org_and_owner, oidc_connection):
    _org, _owner, prefix = org_and_owner
    subject = f"{prefix}repeat"
    first = await resolve_federated_user(
        db_pool,
        connection=oidc_connection,
        identity=_identity(subject=subject, email=f"{prefix}repeat@test.com"),
    )
    second = await resolve_federated_user(
        db_pool,
        connection=oidc_connection,
        identity=_identity(subject=subject, email=f"{prefix}repeat@test.com"),
    )
    assert first["id"] == second["id"]


@pytest.mark.asyncio
async def test_jit_role_comes_from_group_mapping(db_pool, org_and_owner):
    org, _owner, prefix = org_and_owner
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"map-{secrets.token_hex(4)}",
        display_name="Mapped IdP",
        issuer="https://idp.test",
        client_id="cid",
        role_claim="groups",
        role_mappings=[{"idp_value": "lucent-admins", "role": "admin"}],
    )
    user = await resolve_federated_user(
        db_pool,
        connection=connection,
        identity=_identity(
            subject=f"{prefix}mapped",
            email=f"{prefix}mapped@test.com",
            groups=["lucent-admins"],
        ),
    )
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_jit_cannot_produce_hyperadmin(db_pool, org_and_owner):
    org, _owner, prefix = org_and_owner
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"hax-{secrets.token_hex(4)}",
        display_name="Evil IdP",
        issuer="https://idp.test",
        client_id="cid",
        role_mappings=[{"idp_value": "ops", "role": "hyperadmin"}],
    )
    user = await resolve_federated_user(
        db_pool,
        connection=connection,
        identity=_identity(
            subject=f"{prefix}hax", email=f"{prefix}hax@test.com", groups=["ops"]
        ),
    )
    assert user["role"] == "member"


@pytest.mark.asyncio
async def test_auto_create_disabled_rejects_unknown_user(db_pool, org_and_owner):
    org, _owner, prefix = org_and_owner
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"nojit-{secrets.token_hex(4)}",
        display_name="No JIT",
        issuer="https://idp.test",
        client_id="cid",
        auto_create_users=False,
    )
    with pytest.raises(FederatedIdentityError, match="automatic account creation"):
        await resolve_federated_user(
            db_pool,
            connection=connection,
            identity=_identity(email=f"{prefix}nojit@test.com"),
        )


@pytest.mark.asyncio
async def test_email_linking_requires_verified_identity(db_pool, org_and_owner):
    """An unverified address must not adopt an existing account."""
    org, owner, prefix = org_and_owner
    await UserRepository(db_pool).set_email_verified(owner["id"], True)
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"link-{secrets.token_hex(4)}",
        display_name="Link IdP",
        issuer="https://idp.test",
        client_id="cid",
        allow_email_linking=True,
    )
    with pytest.raises(FederatedIdentityError, match="did not verify"):
        await resolve_federated_user(
            db_pool,
            connection=connection,
            identity=_identity(subject=f"{prefix}unverified", email=owner["email"], verified=False),
        )


@pytest.mark.asyncio
async def test_email_linking_requires_verified_target(db_pool, org_and_owner):
    """An unverified *existing* account must not be adopted either."""
    org, owner, prefix = org_and_owner
    # Explicitly unverify the target account.
    await UserRepository(db_pool).set_email_verified(owner["id"], False)
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"unv-{secrets.token_hex(4)}",
        display_name="Unverified IdP",
        issuer="https://idp.test",
        client_id="cid",
        allow_email_linking=True,
    )
    with pytest.raises(FederatedIdentityError, match="not been"):
        await resolve_federated_user(
            db_pool,
            connection=connection,
            identity=_identity(subject=f"{prefix}unv", email=owner["email"], verified=True),
        )


@pytest.mark.asyncio
async def test_email_linking_can_be_disabled(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    await UserRepository(db_pool).set_email_verified(owner["id"], True)
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"nolink-{secrets.token_hex(4)}",
        display_name="No Link IdP",
        issuer="https://idp.test",
        client_id="cid",
        allow_email_linking=False,
    )
    with pytest.raises(FederatedIdentityError, match="does not allow linking"):
        await resolve_federated_user(
            db_pool,
            connection=connection,
            identity=_identity(subject=f"{prefix}nolink", email=owner["email"], verified=True),
        )


@pytest.mark.asyncio
async def test_verified_email_links_to_existing_account(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    await UserRepository(db_pool).set_email_verified(owner["id"], True)
    connection = await SsoConnectionRepository(db_pool).create(
        organization_id=org["id"],
        provider_type="oidc",
        slug=f"yes-{secrets.token_hex(4)}",
        display_name="Link OK IdP",
        issuer="https://idp.test",
        client_id="cid",
        allow_email_linking=True,
    )
    linked = await resolve_federated_user(
        db_pool,
        connection=connection,
        identity=_identity(subject=f"{prefix}linked", email=owner["email"], verified=True),
    )
    assert linked["id"] == owner["id"]
    assert linked["provider"] == "oidc"
    assert linked["external_id"] == f"{prefix}linked"


@pytest.mark.asyncio
async def test_identity_cannot_cross_tenant_boundary(
    db_pool, org_and_owner, cleanup_test_organizations
):
    """A subject already bound in tenant A must not sign in to tenant B."""
    org_a, _owner, prefix = org_and_owner
    org_repo = OrganizationRepository(db_pool)
    org_b = await org_repo.create(name=f"{prefix}other-org")
    subject = f"{prefix}shared-sub"
    connection_a = await SsoConnectionRepository(db_pool).create(
        organization_id=org_a["id"],
        provider_type="oidc",
        slug=f"a-{secrets.token_hex(4)}",
        display_name="Tenant A",
        issuer="https://idp.test",
        client_id="cid",
    )
    user_a = await resolve_federated_user(
        db_pool,
        connection=connection_a,
        identity=_identity(subject=subject, email=f"{prefix}a@test.com"),
    )

    connection_b = await SsoConnectionRepository(db_pool).create(
        organization_id=org_b["id"],
        provider_type="oidc",
        slug=f"b-{secrets.token_hex(4)}",
        display_name="Tenant B",
        issuer="https://idp.test",
        client_id="cid",
    )
    with pytest.raises(FederatedIdentityError, match="different organization"):
        await resolve_federated_user(
            db_pool,
            connection=connection_b,
            identity=_identity(subject=subject, email=f"{prefix}b@test.com"),
        )
    _ = user_a


@pytest.mark.asyncio
async def test_deactivated_user_cannot_be_admitted(db_pool, org_and_owner, oidc_connection):
    _org, _owner, prefix = org_and_owner
    subject = f"{prefix}inactive"
    user = await resolve_federated_user(
        db_pool,
        connection=oidc_connection,
        identity=_identity(subject=subject, email=f"{prefix}inactive@test.com"),
    )
    await UserRepository(db_pool).update(user["id"], is_active=False)

    with pytest.raises(FederatedIdentityError, match="deactivated"):
        await resolve_federated_user(
            db_pool,
            connection=oidc_connection,
            identity=_identity(subject=subject, email=f"{prefix}inactive@test.com"),
        )


# ============================================================================
# SCIM
# ============================================================================


@pytest_asyncio.fixture
async def scim_token(db_pool, org_and_owner):
    org, owner, _prefix = org_and_owner
    record, token = await ScimTokenRepository(db_pool).create(
        organization_id=org["id"],
        name="test-scim",
        created_by=owner["id"],
    )
    return record, token


@pytest_asyncio.fixture
async def scim_client(scim_token):
    _record, token = scim_token
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_scim_requires_a_token(public_client):
    response = await public_client.get("/scim/v2/Users")
    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()


@pytest.mark.asyncio
async def test_scim_rejects_bad_token(public_client):
    response = await public_client.get(
        "/scim/v2/Users", headers={"Authorization": "Bearer sc_nope"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_scim_service_provider_config(scim_client):
    response = await scim_client.get("/scim/v2/ServiceProviderConfig")
    assert response.status_code == 200
    body = response.json()
    assert body["patch"]["supported"] is True
    assert body["bulk"]["supported"] is False


@pytest.mark.asyncio
async def test_scim_creates_user_without_password(scim_client, db_pool, org_and_owner):
    org, _owner, prefix = org_and_owner
    response = await scim_client.post(
        "/scim/v2/Users",
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": f"{prefix}scim@test.com",
            "displayName": "SCIM Person",
            "externalId": f"{prefix}scim-ext",
            "emails": [{"value": f"{prefix}scim@test.com", "primary": True}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["active"] is True
    assert body["externalId"] == f"{prefix}scim-ext"

    user = await UserRepository(db_pool).get_by_id(body["id"])
    assert user["provisioned_by"] == "scim"
    assert user["scim_external_id"] == f"{prefix}scim-ext"
    # A directory sync must not invent a password.
    assert await UserRepository(db_pool).get_password_hash(body["id"]) is None


@pytest.mark.asyncio
async def test_scim_create_is_idempotent_for_existing_email(
    scim_client, db_pool, org_and_owner
):
    org, owner, _prefix = org_and_owner
    response = await scim_client.post(
        "/scim/v2/Users",
        json={
            "userName": owner["email"],
            "externalId": "new-ext-for-existing",
            "emails": [{"value": owner["email"], "primary": True}],
        },
    )
    # Okta retries creates; a 409 would mark the whole sync as failed.
    assert response.status_code == 200
    assert response.json()["id"] == str(owner["id"])


@pytest.mark.asyncio
async def test_scim_filter_by_username(scim_client, org_and_owner):
    _org, owner, _prefix = org_and_owner
    response = await scim_client.get(
        "/scim/v2/Users", params={"filter": f'userName eq "{owner["email"]}"'}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["id"] == str(owner["id"])


@pytest.mark.asyncio
async def test_scim_rejects_unsupported_filter(scim_client):
    response = await scim_client.get(
        "/scim/v2/Users", params={"filter": 'displayName co "x"'}
    )
    assert response.status_code == 400
    assert response.json()["scimType"] == "invalidFilter"


@pytest.mark.asyncio
async def test_scim_deactivate_ends_sessions(scim_client, db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    user_repo = UserRepository(db_pool)
    employee = await user_repo.create(
        external_id=f"{prefix}leaver",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}leaver@test.com",
        display_name="Leaver",
        role="member",
    )
    await user_repo.update_last_login(employee["id"])
    session_token = await create_session(db_pool, employee["id"])

    response = await scim_client.patch(
        f"/scim/v2/Users/{employee['id']}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
    )
    assert response.status_code == 200
    assert response.json()["active"] is False

    from lucent.auth_providers import validate_session

    # Deactivation must invalidate the live session, not just the row.
    assert await validate_session(db_pool, session_token) is None


@pytest.mark.asyncio
async def test_scim_delete_hard_deletes_never_used_user(
    scim_client, db_pool, org_and_owner
):
    org, _owner, prefix = org_and_owner
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}never",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}never@test.com",
        display_name="Never Used",
    )
    response = await scim_client.delete(f"/scim/v2/Users/{user['id']}")
    assert response.status_code == 204
    assert await UserRepository(db_pool).get_by_id(user["id"]) is None


@pytest.mark.asyncio
async def test_scim_delete_deactivates_used_user(scim_client, db_pool, org_and_owner):
    org, _owner, prefix = org_and_owner
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{prefix}used",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}used@test.com",
        display_name="Used",
    )
    await user_repo.update_last_login(user["id"])

    response = await scim_client.delete(f"/scim/v2/Users/{user['id']}")
    assert response.status_code == 204

    remaining = await user_repo.get_by_id(user["id"])
    # The account stays so its history remains attributable, but is disabled.
    assert remaining is not None
    assert remaining["is_active"] is False


@pytest.mark.asyncio
async def test_scim_cannot_delete_owner(scim_client, org_and_owner):
    _org, owner, _prefix = org_and_owner
    response = await scim_client.delete(f"/scim/v2/Users/{owner['id']}")
    assert response.status_code == 400
    assert "owner" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_scim_cannot_see_another_tenant(
    scim_client, db_pool, org_and_owner, cleanup_test_organizations
):
    """A token from tenant A must not reach tenant B's users."""
    org, _owner, prefix = org_and_owner
    other_org = await OrganizationRepository(db_pool).create(name=f"{prefix}scim-other")
    outsider = await UserRepository(db_pool).create(
        external_id=f"{prefix}outsider",
        provider="local",
        organization_id=other_org["id"],
        email=f"{prefix}outsider@test.com",
        display_name="Outsider",
    )
    _ = org, outsider
    response = await scim_client.get(f"/scim/v2/Users/{outsider['id']}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_scim_list_excludes_daemon(db_pool, org_and_owner, scim_client):
    """A directory sync must never surface the per-org daemon identity."""
    response = await scim_client.get("/scim/v2/Users")
    assert response.status_code == 200
    for resource in response.json()["Resources"]:
        assert "daemon@lucent.local" not in (resource.get("userName") or "")


@pytest.mark.asyncio
async def test_revoked_scim_token_is_rejected(scim_client, scim_token, db_pool, org_and_owner):
    record, _token = scim_token
    org, _owner, _prefix = org_and_owner
    await ScimTokenRepository(db_pool).revoke(record["id"], organization_id=org["id"])

    response = await scim_client.get("/scim/v2/Users")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_scim_token_is_rejected(db_pool, org_and_owner, public_client):
    org, owner, _prefix = org_and_owner
    _record, token = await ScimTokenRepository(db_pool).create(
        organization_id=org["id"],
        name="expired",
        created_by=owner["id"],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    response = await public_client.get(
        "/scim/v2/Users", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


# ============================================================================
# Email uniqueness
# ============================================================================


@pytest.mark.asyncio
async def test_duplicate_email_in_org_is_rejected(db_pool, org_and_owner):
    import asyncpg

    org, owner, prefix = org_and_owner
    with pytest.raises(asyncpg.UniqueViolationError):
        await UserRepository(db_pool).create(
            external_id=f"{prefix}dupe",
            provider="local",
            organization_id=org["id"],
            email=owner["email"].upper(),
            display_name="Dupe",
        )


@pytest.mark.asyncio
async def test_same_email_allowed_in_different_orgs(db_pool, org_and_owner):
    org, owner, prefix = org_and_owner
    other_org = await OrganizationRepository(db_pool).create(name=f"{prefix}x-org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}cross",
        provider="local",
        organization_id=other_org["id"],
        email=owner["email"],
        display_name="Cross Tenant",
    )
    assert user["organization_id"] == other_org["id"]
