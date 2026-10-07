"""Tests for the secret storage system.

Covers:
- Store and retrieve a secret
- Encryption verification (DB value != plaintext)
- Access control: user A can't read user B's secrets
- Missing LUCENT_SECRET_KEY raises clear error
- Provider registry
- API endpoints (CRUD)
- Audit logging for secret access
"""

import json
import os
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import GroupRepository, OrganizationRepository, UserRepository
from lucent.secrets import SecretRegistry, SecretScope
from lucent.secrets.builtin import (
    BuiltinSecretProvider,
    SecretKeyError,
    _derive_fernet_key,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def secret_prefix(db_pool):
    """Create and clean up test data for secrets tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_sec_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM secrets WHERE key LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM user_groups WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM groups WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def org_and_users(db_pool, secret_prefix):
    """Create an org with two users for ACL tests."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{secret_prefix}org")
    user_repo = UserRepository(db_pool)
    user_a = await user_repo.create(
        external_id=f"{secret_prefix}userA",
        provider="local",
        organization_id=org["id"],
        email=f"{secret_prefix}a@test.com",
        display_name="User A",
        role="member",
    )
    user_b = await user_repo.create(
        external_id=f"{secret_prefix}userB",
        provider="local",
        organization_id=org["id"],
        email=f"{secret_prefix}b@test.com",
        display_name="User B",
        role="member",
    )
    admin = await user_repo.create(
        external_id=f"{secret_prefix}admin",
        provider="local",
        organization_id=org["id"],
        email=f"{secret_prefix}admin@test.com",
        display_name="Admin",
        role="admin",
    )
    return {"org": org, "user_a": user_a, "user_b": user_b, "admin": admin}


@pytest_asyncio.fixture
async def provider(db_pool):
    """Create a BuiltinSecretProvider with a test key."""
    return BuiltinSecretProvider(db_pool, secret_key="test-secret-key-for-unit-tests")


@pytest_asyncio.fixture
async def registered_provider(provider):
    """Register the provider in the registry and clean up after."""
    SecretRegistry.register("builtin", provider)
    yield provider
    SecretRegistry.reset()


def _make_client(app, user_record):
    """Create an httpx AsyncClient with auth mocked to the given user."""
    fake_user = CurrentUser(
        id=user_record["id"],
        organization_id=user_record["organization_id"],
        role=user_record.get("role", "member"),
        email=user_record.get("email"),
        display_name=user_record.get("display_name"),
    )
    app.dependency_overrides[get_current_user] = lambda: fake_user
    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


# ============================================================================
# Unit Tests: Encryption
# ============================================================================


class TestEncryption:
    """Test Fernet encryption mechanics."""

    def test_derive_key_deterministic(self):
        """PBKDF2 derivation produces the same key for the same input."""
        k1 = _derive_fernet_key("my-secret")
        k2 = _derive_fernet_key("my-secret")
        assert k1 == k2

    def test_derive_key_different_inputs(self):
        """Different inputs produce different keys."""
        k1 = _derive_fernet_key("key-one")
        k2 = _derive_fernet_key("key-two")
        assert k1 != k2

    def test_missing_secret_key_raises(self):
        """Missing LUCENT_SECRET_KEY raises SecretKeyError."""
        old = os.environ.pop("LUCENT_SECRET_KEY", None)
        try:
            with pytest.raises(SecretKeyError, match="LUCENT_SECRET_KEY"):
                BuiltinSecretProvider.__new__(BuiltinSecretProvider)
                BuiltinSecretProvider.__init__(
                    BuiltinSecretProvider.__new__(BuiltinSecretProvider),
                    pool=None,
                )
        finally:
            if old is not None:
                os.environ["LUCENT_SECRET_KEY"] = old


# ============================================================================
# Unit Tests: Provider Operations
# ============================================================================


class TestBuiltinProvider:
    """Test the built-in Postgres+Fernet provider."""

    @pytest.mark.asyncio
    async def test_set_and_get(self, provider, org_and_users, secret_prefix):
        """Store and retrieve a secret."""
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await provider.set(f"{secret_prefix}api_key", "super-secret-value", scope)
        value = await provider.get(f"{secret_prefix}api_key", scope)
        assert value == "super-secret-value"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, provider, org_and_users, secret_prefix):
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        assert await provider.get(f"{secret_prefix}nope", scope) is None

    @pytest.mark.asyncio
    async def test_encryption_in_db(self, provider, org_and_users, db_pool, secret_prefix):
        """Verify the DB value is encrypted, not plaintext."""
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await provider.set(f"{secret_prefix}enc_test", "plaintext-value", scope)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT encrypted_value FROM secrets WHERE key = $1",
                f"{secret_prefix}enc_test",
            )
        raw = row["encrypted_value"]
        assert raw != b"plaintext-value"
        assert b"plaintext-value" not in raw

    @pytest.mark.asyncio
    async def test_update_existing(self, provider, org_and_users, secret_prefix):
        """Setting the same key again updates the value."""
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await provider.set(f"{secret_prefix}update", "v1", scope)
        await provider.set(f"{secret_prefix}update", "v2", scope)
        assert await provider.get(f"{secret_prefix}update", scope) == "v2"

    @pytest.mark.asyncio
    async def test_delete(self, provider, org_and_users, secret_prefix):
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await provider.set(f"{secret_prefix}del", "val", scope)
        assert await provider.delete(f"{secret_prefix}del", scope) is True
        assert await provider.get(f"{secret_prefix}del", scope) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, provider, org_and_users, secret_prefix):
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        assert await provider.delete(f"{secret_prefix}nope", scope) is False

    @pytest.mark.asyncio
    async def test_list_keys(self, provider, org_and_users, secret_prefix):
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await provider.set(f"{secret_prefix}k1", "v1", scope)
        await provider.set(f"{secret_prefix}k2", "v2", scope)
        keys = await provider.list_keys(scope)
        assert f"{secret_prefix}k1" in keys
        assert f"{secret_prefix}k2" in keys

    @pytest.mark.asyncio
    async def test_scope_isolation(self, provider, org_and_users, secret_prefix):
        """User A's secrets are invisible to user B."""
        scope_a = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        scope_b = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_b"]["id"]),
        )
        await provider.set(f"{secret_prefix}private", "a-secret", scope_a)
        assert await provider.get(f"{secret_prefix}private", scope_b) is None
        assert f"{secret_prefix}private" not in await provider.list_keys(scope_b)

    @pytest.mark.asyncio
    async def test_system_managed_scope(self, provider, org_and_users, db_pool, secret_prefix):
        """Lucent-owned app secrets can be stored without user/group ownership."""
        system_scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            system_managed=True,
        )
        user_scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        key = f"{secret_prefix}system-signing"

        await provider.set(key, "stored-in-secret-provider", system_scope)

        assert await provider.get(key, system_scope) == "stored-in-secret-provider"
        assert await provider.get(key, user_scope) is None
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT system_managed, owner_user_id, owner_group_id
                   FROM secrets WHERE key = $1""",
                key,
            )
        assert row["system_managed"] is True
        assert row["owner_user_id"] is None
        assert row["owner_group_id"] is None

    @pytest.mark.asyncio
    async def test_initialize_signing_secret_uses_secret_storage(
        self,
        registered_provider,
        db_pool,
        secret_prefix,
        monkeypatch,
    ):
        """Cookie signing secret is created and reloaded through SecretRegistry."""
        import lucent.auth_providers as auth_providers

        monkeypatch.delenv("LUCENT_SIGNING_SECRET", raising=False)
        monkeypatch.setattr(auth_providers, "SYSTEM_SECRET_ORG_NAME", f"{secret_prefix}system")
        auth_providers._set_signing_secret("", "test")

        first = await auth_providers.initialize_signing_secret(db_pool)
        auth_providers._set_signing_secret("", "test")
        second = await auth_providers.initialize_signing_secret(db_pool)

        assert second == first
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.system_managed, s.owner_user_id, s.owner_group_id
                   FROM secrets s
                   JOIN organizations o ON o.id = s.organization_id
                   WHERE o.name = $1 AND s.key = $2""",
                f"{secret_prefix}system",
                auth_providers.SIGNING_SECRET_KEY,
            )
        assert row["system_managed"] is True
        assert row["owner_user_id"] is None
        assert row["owner_group_id"] is None


# ============================================================================
# Unit Tests: Registry
# ============================================================================


class TestSecretRegistry:
    def test_register_and_get(self, provider):
        SecretRegistry.reset()
        SecretRegistry.register("test", provider)
        assert SecretRegistry.get("test") is provider
        SecretRegistry.reset()

    def test_get_unregistered_raises(self):
        SecretRegistry.reset()
        with pytest.raises(KeyError, match="not registered"):
            SecretRegistry.get("nonexistent")

    def test_is_registered(self, provider):
        SecretRegistry.reset()
        assert not SecretRegistry.is_registered("builtin")
        SecretRegistry.register("builtin", provider)
        assert SecretRegistry.is_registered("builtin")
        SecretRegistry.reset()


# ============================================================================
# API Endpoint Tests
# ============================================================================


class TestSecretsAPI:
    """Test the /api/secrets endpoints."""

    @pytest.mark.asyncio
    async def test_create_secret(self, registered_provider, org_and_users, secret_prefix):
        app = create_app()
        async with _make_client(app, org_and_users["user_a"]) as client:
            resp = await client.post(
                "/api/secrets",
                json={"key": f"{secret_prefix}api_token", "value": "secret123"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["key"] == f"{secret_prefix}api_token"
        assert "value" not in data  # Never return the value

    @pytest.mark.asyncio
    async def test_list_secrets_no_values(self, registered_provider, org_and_users, secret_prefix):
        app = create_app()
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await registered_provider.set(f"{secret_prefix}list1", "v1", scope)
        await registered_provider.set(f"{secret_prefix}list2", "v2", scope)

        async with _make_client(app, org_and_users["user_a"]) as client:
            resp = await client.get("/api/secrets")
        assert resp.status_code == 200
        data = resp.json()
        keys = [k["key"] for k in data["keys"]]
        assert f"{secret_prefix}list1" in keys
        assert f"{secret_prefix}list2" in keys
        # No values in list response
        for k in data["keys"]:
            assert "value" not in k

    @pytest.mark.asyncio
    async def test_get_secret_value(self, registered_provider, org_and_users, secret_prefix):
        app = create_app()
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await registered_provider.set(f"{secret_prefix}getme", "the-value", scope)

        async with _make_client(app, org_and_users["user_a"]) as client:
            resp = await client.get(f"/api/secrets/{secret_prefix}getme")
        assert resp.status_code == 200
        assert resp.json()["value"] == "the-value"

    @pytest.mark.asyncio
    async def test_get_secret_not_found(self, registered_provider, org_and_users, secret_prefix):
        app = create_app()
        async with _make_client(app, org_and_users["user_a"]) as client:
            resp = await client.get(f"/api/secrets/{secret_prefix}nope")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_secret(self, registered_provider, org_and_users, secret_prefix):
        app = create_app()
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await registered_provider.set(f"{secret_prefix}delme", "val", scope)

        async with _make_client(app, org_and_users["user_a"]) as client:
            resp = await client.delete(f"/api/secrets/{secret_prefix}delme")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_cross_user_access_denied(
        self, registered_provider, org_and_users, secret_prefix
    ):
        """User B cannot read User A's secrets via the API."""
        app = create_app()
        scope_a = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await registered_provider.set(f"{secret_prefix}private", "a-only", scope_a)

        # User B tries to get User A's secret — gets 404 (not found in their scope)
        async with _make_client(app, org_and_users["user_b"]) as client:
            resp = await client.get(f"/api/secrets/{secret_prefix}private")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_non_member_cannot_list_group_secret_names(
        self, registered_provider, org_and_users, db_pool, secret_prefix
    ):
        group_repo = GroupRepository(db_pool)
        group = await group_repo.create_group(
            org_id=str(org_and_users["org"]["id"]),
            name=f"{secret_prefix}group",
            description="Secret group",
            created_by=str(org_and_users["user_a"]["id"]),
        )
        await group_repo.add_member(
            str(group["id"]),
            str(org_and_users["user_a"]["id"]),
            role="admin",
            organization_id=str(org_and_users["org"]["id"]),
        )
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_group_id=str(group["id"]),
        )
        await registered_provider.set(f"{secret_prefix}group_secret", "val", scope)

        app = create_app()
        async with _make_client(app, org_and_users["user_b"]) as client:
            resp = await client.get(f"/api/secrets?owner_group_id={group['id']}")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_non_group_admin_cannot_create_group_secret(
        self, registered_provider, org_and_users, db_pool, secret_prefix
    ):
        group_repo = GroupRepository(db_pool)
        group = await group_repo.create_group(
            org_id=str(org_and_users["org"]["id"]),
            name=f"{secret_prefix}writers",
            description="Secret writers",
            created_by=str(org_and_users["user_a"]["id"]),
        )
        await group_repo.add_member(
            str(group["id"]),
            str(org_and_users["user_b"]["id"]),
            role="member",
            organization_id=str(org_and_users["org"]["id"]),
        )

        app = create_app()
        async with _make_client(app, org_and_users["user_b"]) as client:
            resp = await client.post(
                "/api/secrets",
                json={
                    "key": f"{secret_prefix}poison",
                    "value": "bad",
                    "owner_group_id": str(group["id"]),
                },
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_log_on_read(
        self, registered_provider, org_and_users, db_pool, secret_prefix
    ):
        """Reading a secret creates an audit log entry."""
        app = create_app()
        scope = SecretScope(
            organization_id=str(org_and_users["org"]["id"]),
            owner_user_id=str(org_and_users["user_a"]["id"]),
        )
        await registered_provider.set(f"{secret_prefix}audited", "val", scope)

        async with _make_client(app, org_and_users["user_a"]) as client:
            await client.get(f"/api/secrets/{secret_prefix}audited")

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM memory_audit_log WHERE action_type = 'secret_read' "
                "AND context->>'secret_key' = $1 ORDER BY created_at DESC LIMIT 1",
                f"{secret_prefix}audited",
            )
        assert row is not None
        assert str(row["user_id"]) == str(org_and_users["user_a"]["id"])

    @pytest.mark.asyncio
    async def test_migrate_plaintext_configs_migrates_tools_and_redacts_runtime_records(
        self, registered_provider, org_and_users, db_pool, secret_prefix,
    ):
        org_id = org_and_users["org"]["id"]
        admin_id = org_and_users["admin"]["id"]
        template_id = tool_id = sandbox_id = None
        try:
            async with db_pool.acquire() as conn:
                template = await conn.fetchrow(
                    """INSERT INTO sandbox_templates
                       (name, organization_id, created_by, owner_user_id, env_vars)
                       VALUES ($1, $2, $3, $3, $4::jsonb) RETURNING id""",
                    f"{secret_prefix}sandbox",
                    org_id,
                    admin_id,
                    {"API_TOKEN": "legacy-fixture-token"},
                )
                tool = await conn.fetchrow(
                    """INSERT INTO managed_tool_definitions
                       (name, organization_id, created_by, owner_user_id, env_vars)
                       VALUES ($1, $2, $3, $3, $4::jsonb) RETURNING id""",
                    f"{secret_prefix}tool",
                    org_id,
                    admin_id,
                    {"API_TOKEN": "legacy-fixture-token"},
                )
                sandbox = await conn.fetchrow(
                    """INSERT INTO sandboxes (name, organization_id, config)
                       VALUES ($1, $2, $3::jsonb) RETURNING id""",
                    f"{secret_prefix}runtime",
                    org_id,
                    {
                        "env_vars": {
                            "NODE_ENV": "test",
                            "API_TOKEN": "legacy-fixture-token",
                        },
                        "git_credentials": "legacy-git-token",
                    },
                )
                template_id, tool_id, sandbox_id = (
                    template["id"], tool["id"], sandbox["id"]
                )

            app = create_app()
            async with _make_client(app, org_and_users["admin"]) as client:
                response = await client.post("/api/secrets/migrate-plaintext-configs")

            assert response.status_code == 200
            result = response.json()
            assert result["migrated_sandbox_env_vars"] == 1
            assert result["migrated_managed_tool_env_vars"] == 1
            assert result["redacted_sandbox_runtime_configs"] == 1

            async with db_pool.acquire() as conn:
                template_env = await conn.fetchval(
                    "SELECT env_vars FROM sandbox_templates WHERE id = $1", template_id,
                )
                tool_env = await conn.fetchval(
                    "SELECT env_vars FROM managed_tool_definitions WHERE id = $1", tool_id,
                )
                runtime_config = await conn.fetchval(
                    "SELECT config FROM sandboxes WHERE id = $1", sandbox_id,
                )
            if isinstance(template_env, str):
                template_env = json.loads(template_env)
            if isinstance(tool_env, str):
                tool_env = json.loads(tool_env)
            if isinstance(runtime_config, str):
                runtime_config = json.loads(runtime_config)
            assert template_env["API_TOKEN"].startswith("secret://sandbox.")
            assert tool_env["API_TOKEN"].startswith("secret://managed-tool.")
            assert runtime_config["env_vars"] == {"NODE_ENV": "***", "API_TOKEN": "***"}
            assert "git_credentials" not in runtime_config
        finally:
            async with db_pool.acquire() as conn:
                if sandbox_id:
                    await conn.execute("DELETE FROM sandboxes WHERE id = $1", sandbox_id)
                if tool_id:
                    await conn.execute(
                        "DELETE FROM managed_tool_definitions WHERE id = $1", tool_id,
                    )
                if template_id:
                    await conn.execute(
                        "DELETE FROM sandbox_templates WHERE id = $1", template_id,
                    )
                await conn.execute(
                    "DELETE FROM secrets WHERE organization_id = $1 "
                    "AND key LIKE ANY($2::text[])",
                    org_id,
                    ["sandbox.%", "managed-tool.%"],
                )

    @pytest.mark.asyncio
    async def test_migrate_plaintext_configs_scrubs_legacy_string_runtime_config(
        self, registered_provider, org_and_users, db_pool, secret_prefix,
    ):
        org_id = org_and_users["org"]["id"]
        sandbox_id = None
        try:
            async with db_pool.acquire() as conn:
                sandbox = await conn.fetchrow(
                    """INSERT INTO sandboxes (name, organization_id, config)
                       VALUES ($1, $2, $3::jsonb) RETURNING id""",
                    f"{secret_prefix}legacy-runtime",
                    org_id,
                    json.dumps(
                        {
                            "env_vars": {"API_TOKEN": "legacy-fixture-token"},
                            "git_credentials": "legacy-git-token",
                        }
                    ),
                )
                sandbox_id = sandbox["id"]
                config_type_before = await conn.fetchval(
                    "SELECT jsonb_typeof(config) FROM sandboxes WHERE id = $1", sandbox_id,
                )
            assert config_type_before == "string"

            app = create_app()
            async with _make_client(app, org_and_users["admin"]) as client:
                response = await client.post("/api/secrets/migrate-plaintext-configs")

            assert response.status_code == 200
            assert response.json()["redacted_sandbox_runtime_configs"] == 1

            async with db_pool.acquire() as conn:
                config_type_after = await conn.fetchval(
                    "SELECT jsonb_typeof(config) FROM sandboxes WHERE id = $1", sandbox_id,
                )
                persisted_config = await conn.fetchval(
                    "SELECT config::text FROM sandboxes WHERE id = $1", sandbox_id,
                )
            assert config_type_after == "object"
            assert '"API_TOKEN": "***"' in persisted_config
            assert "git_credentials" not in persisted_config
            assert "legacy-fixture-token" not in persisted_config
        finally:
            if sandbox_id:
                async with db_pool.acquire() as conn:
                    await conn.execute("DELETE FROM sandboxes WHERE id = $1", sandbox_id)


class TestGrantedValueAccess:
    """Grants must reach the value path, not stop at the listing.

    Before the clearance value path, a user granted read on someone
    else's secret could see the key in a listing but both the API value
    fetch (scope-predicated get_secret_id lookup) and the env-reference
    resolver (candidate-scope probe) came up empty.
    """

    @pytest.mark.asyncio
    async def test_granted_user_fetches_secret_value(
        self, registered_provider, org_and_users, secret_prefix, db_pool
    ):
        from lucent.db.access_control import AuthAccessRepository

        org_id = str(org_and_users["org"]["id"])
        scope = SecretScope(
            organization_id=org_id, owner_user_id=str(org_and_users["user_a"]["id"])
        )
        key = f"{secret_prefix}granted"
        await registered_provider.set(key, "grantee-sees-this", scope)

        secret_id = await registered_provider.get_secret_id(key, scope)
        await AuthAccessRepository(db_pool).upsert_grant(
            "secret",
            UUID(secret_id),
            grantee_type="user",
            grantee_id=UUID(str(org_and_users["user_b"]["id"])),
            role="read",
            granted_by=UUID(str(org_and_users["user_a"]["id"])),
            organization_id=UUID(org_id),
        )

        app = create_app()
        async with _make_client(app, org_and_users["user_b"]) as client:
            resp = await client.get(f"/api/secrets/{key}")
        assert resp.status_code == 200
        assert resp.json()["value"] == "grantee-sees-this"

    @pytest.mark.asyncio
    async def test_granted_user_lists_granted_secret(
        self, registered_provider, org_and_users, secret_prefix, db_pool
    ):
        from lucent.db.access_control import AuthAccessRepository

        org_id = str(org_and_users["org"]["id"])
        scope = SecretScope(
            organization_id=org_id, owner_user_id=str(org_and_users["user_a"]["id"])
        )
        key = f"{secret_prefix}granted_list"
        await registered_provider.set(key, "value", scope)
        secret_id = await registered_provider.get_secret_id(key, scope)
        await AuthAccessRepository(db_pool).upsert_grant(
            "secret",
            UUID(secret_id),
            grantee_type="user",
            grantee_id=UUID(str(org_and_users["user_b"]["id"])),
            role="read",
            granted_by=UUID(str(org_and_users["user_a"]["id"])),
            organization_id=UUID(org_id),
        )

        app = create_app()
        async with _make_client(app, org_and_users["user_b"]) as client:
            resp = await client.get("/api/secrets")
        assert resp.status_code == 200
        keys = [k["key"] for k in resp.json()["keys"]]
        assert key in keys

    @pytest.mark.asyncio
    async def test_ungranted_user_cannot_fetch_or_resolve(
        self, registered_provider, org_and_users, secret_prefix, db_pool
    ):
        from lucent.api.deps import CurrentUser, get_current_user
        from lucent.auth import set_current_user
        from lucent.secrets.utils import resolve_secret_reference

        org_id = str(org_and_users["org"]["id"])
        scope = SecretScope(
            organization_id=org_id, owner_user_id=str(org_and_users["user_a"]["id"])
        )
        key = f"{secret_prefix}private_val"
        await registered_provider.set(key, "a-only", scope)

        # User B (same org, member, NO grant): API fetch is 404 —
        app = create_app()
        async with _make_client(app, org_and_users["user_b"]) as client:
            resp = await client.get(f"/api/secrets/{key}")
        assert resp.status_code == 404

        # — and the env_reference resolution is a clear KeyError.
        fake_user = CurrentUser(
            id=org_and_users["user_b"]["id"],
            organization_id=org_id,
            role="member",
            email=org_and_users["user_b"].get("email"),
            display_name=org_and_users["user_b"].get("display_name"),
        )
        app.dependency_overrides[get_current_user] = lambda: fake_user
        set_current_user(
            {"id": str(org_and_users["user_b"]["id"]), "organization_id": org_id}
        )
        try:
            with pytest.raises(KeyError):
                await resolve_secret_reference(
                    f"secret://{key}", registered_provider
                )
        finally:
            set_current_user(None)

    @pytest.mark.asyncio
    async def test_granted_user_resolves_env_reference(
        self, registered_provider, org_and_users, secret_prefix, db_pool
    ):
        from lucent.auth import set_current_user
        from lucent.db.access_control import AuthAccessRepository
        from lucent.secrets.utils import resolve_secret_reference

        org_id = str(org_and_users["org"]["id"])
        scope = SecretScope(
            organization_id=org_id, owner_user_id=str(org_and_users["user_a"]["id"])
        )
        key = f"{secret_prefix}envref"
        await registered_provider.set(key, "resolved!", scope)
        secret_id = await registered_provider.get_secret_id(key, scope)
        await AuthAccessRepository(db_pool).upsert_grant(
            "secret",
            UUID(secret_id),
            grantee_type="user",
            grantee_id=UUID(str(org_and_users["user_b"]["id"])),
            role="read",
            granted_by=UUID(str(org_and_users["user_a"]["id"])),
            organization_id=UUID(org_id),
        )

        set_current_user(
            {"id": str(org_and_users["user_b"]["id"]), "organization_id": org_id}
        )
        try:
            value = await resolve_secret_reference(
                f"secret://{key}", registered_provider
            )
            assert value == "resolved!"
        finally:
            set_current_user(None)
