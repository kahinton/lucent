"""Integration tests for per-organization provider credentials and models.

The service exists so an instance operator can configure *someone else's*
workspace. Two mistakes are easy to make and both are covered here:

* writing a credential somewhere the engines cannot read it, and
* granting a model with the operator as grantor, which the clearance guard
  rejects because the operator is not a member of the tenant.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from lucent.db import OrganizationRepository, UserRepository
from lucent.secrets.builtin import BuiltinSecretProvider
from lucent.secrets.registry import SecretRegistry
from lucent.services import org_providers


@pytest_asyncio.fixture(autouse=True)
async def secret_provider(db_pool):
    """Register the real builtin provider so credential writes hit the database.

    Stubbing this out would leave the one thing worth checking untested: that a
    credential lands at the canonical key the engines actually read.
    """
    provider = BuiltinSecretProvider(db_pool)
    SecretRegistry.register("builtin", provider)
    SecretRegistry.set_default("builtin")
    yield provider
    SecretRegistry.reset()


@pytest_asyncio.fixture
async def tenant(db_pool, clean_test_data):
    """A tenant with its own owner — the grantor the clearance guard expects."""
    prefix = clean_test_data
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}tenant")
    owner = await UserRepository(db_pool).create(
        external_id=f"{prefix}tenantowner",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}tenantowner@test.com",
        display_name=f"{prefix}Tenant Owner",
        role="owner",
    )
    return org, owner


class TestCredentials:
    async def test_set_and_read_credential(self, db_pool, tenant):
        org, _ = tenant

        await org_providers.set_credential(
            db_pool, org["id"], provider="openai", value="sk-test-abc"
        )

        credentials = await org_providers.list_credentials(db_pool, org["id"])
        row = next(c for c in credentials if c["provider"] == "openai")
        assert row["configured"] is True

        # The value is never returned, only the fact that it exists.
        assert "sk-test-abc" not in str(credentials)

    async def test_credential_is_org_scoped(self, db_pool, tenant, clean_test_data):
        """One tenant's key must not make another tenant look configured."""
        org, _ = tenant
        other = await OrganizationRepository(db_pool).create(
            name=f"{clean_test_data}other"
        )

        await org_providers.set_credential(
            db_pool, org["id"], provider="openai", value="sk-test-abc"
        )

        other_credentials = await org_providers.list_credentials(db_pool, other["id"])
        other_row = next(c for c in other_credentials if c["provider"] == "openai")
        assert other_row["configured"] is False

    async def test_clear_credential(self, db_pool, tenant):
        org, _ = tenant
        await org_providers.set_credential(
            db_pool, org["id"], provider="openai", value="sk-test-abc"
        )

        await org_providers.clear_credential(db_pool, org["id"], provider="openai")

        credentials = await org_providers.list_credentials(db_pool, org["id"])
        row = next(c for c in credentials if c["provider"] == "openai")
        assert row["configured"] is False

    async def test_available_providers_is_non_empty(self):
        providers = org_providers.available_providers()
        assert providers, "the console needs at least one configurable provider"
        assert all({"provider", "name", "engines"} <= set(p) for p in providers)


class TestModels:
    async def test_create_model_grants_to_tenant(self, db_pool, tenant):
        """Creating a model must leave it usable, not merely present."""
        org, owner = tenant

        await org_providers.create_model(
            db_pool,
            org["id"],
            model_id="test-model-granted",
            provider="openai",
            name="Test Model",
            granted_by=owner["id"],
        )

        models = await org_providers.list_models(db_pool, org["id"])
        created = next(m for m in models if m["id"] == "test-model-granted")
        assert created["owned"] is True

        # The clearance the usage path joins on must exist for the org.
        clearance = await db_pool.fetchrow(
            "SELECT acl.role FROM auth_clearances acl "
            "  JOIN models m ON m.auth_id = acl.auth_id "
            " WHERE m.id = $1 AND acl.principal_type = 'org' "
            "   AND acl.principal_id = $2::uuid",
            "test-model-granted",
            org["id"],
        )
        assert clearance is not None, "an enabled model with no clearance is unusable"

    async def test_list_models_marks_shared_rows(self, db_pool, tenant):
        """Shared registry rows are not owned, which decides what a caller may do."""
        org, _ = tenant
        await db_pool.execute(
            "INSERT INTO models (id, provider, name, organization_id, is_enabled) "
            "VALUES ('test-shared-model', 'openai', 'Shared', NULL, true) "
            "ON CONFLICT (id) DO NOTHING"
        )

        models = await org_providers.list_models(db_pool, org["id"])
        shared = next(m for m in models if m["id"] == "test-shared-model")
        assert shared["owned"] is False

    async def test_toggle_refuses_shared_model(self, db_pool, tenant):
        """Disabling an instance-wide model must not silently do nothing."""
        org, _ = tenant
        await db_pool.execute(
            "INSERT INTO models (id, provider, name, organization_id, is_enabled) "
            "VALUES ('test-shared-toggle', 'openai', 'Shared', NULL, true) "
            "ON CONFLICT (id) DO NOTHING"
        )

        updated = await org_providers.set_model_enabled(
            db_pool, org["id"], "test-shared-toggle", enabled=False
        )
        assert updated is False

        still_enabled = await db_pool.fetchval(
            "SELECT is_enabled FROM models WHERE id = 'test-shared-toggle'"
        )
        assert still_enabled is True, "a shared row must not be flipped per tenant"

    async def test_grant_clones_shared_model_for_the_tenant(self, db_pool, tenant):
        """Granting a shared model gives this tenant access *without stealing it*.

        ``models.id`` is the primary key, so promoting a shared row into one org
        would remove it from every other tenant. The grant must therefore copy.
        """
        org, owner = tenant
        await db_pool.execute(
            "INSERT INTO models (id, provider, name, organization_id, is_enabled) "
            "VALUES ('test-clone-me', 'openai', 'Clone Me', NULL, true) "
            "ON CONFLICT (id) DO NOTHING"
        )

        result = await org_providers.grant_model_to_org(
            db_pool, org["id"], "test-clone-me", granted_by=owner["id"]
        )

        assert result["cloned"] is True
        assert result["model_id"] != "test-clone-me"

        # The shared row is untouched and still available to other tenants.
        source_org = await db_pool.fetchval(
            "SELECT organization_id FROM models WHERE id = 'test-clone-me'"
        )
        assert source_org is None, "the shared row must not be promoted"

        # The tenant has its own copy, and the clearance points at the copy.
        clone_org = await db_pool.fetchval(
            "SELECT organization_id FROM models WHERE id = $1", result["model_id"]
        )
        assert str(clone_org) == str(org["id"])

        clearance = await db_pool.fetchrow(
            "SELECT acl.role FROM auth_clearances acl "
            "  JOIN models m ON m.auth_id = acl.auth_id "
            " WHERE m.id = $1 AND acl.principal_type = 'org' "
            "   AND acl.principal_id = $2::uuid",
            result["model_id"],
            org["id"],
        )
        assert clearance is not None

    async def test_two_tenants_can_both_grant_the_same_shared_model(
        self, db_pool, tenant, clean_test_data
    ):
        """The second tenant must not fail because the first one granted it."""
        org_a, owner_a = tenant
        org_b = await OrganizationRepository(db_pool).create(
            name=f"{clean_test_data}tenantb"
        )
        owner_b = await UserRepository(db_pool).create(
            external_id=f"{clean_test_data}ownerb",
            provider="local",
            organization_id=org_b["id"],
            email=f"{clean_test_data}ownerb@test.com",
            display_name=f"{clean_test_data} Owner B",
            role="owner",
        )
        await db_pool.execute(
            "INSERT INTO models (id, provider, name, organization_id, is_enabled) "
            "VALUES ('test-shared-twice', 'openai', 'Twice', NULL, true) "
            "ON CONFLICT (id) DO NOTHING"
        )

        first = await org_providers.grant_model_to_org(
            db_pool, org_a["id"], "test-shared-twice", granted_by=owner_a["id"]
        )
        second = await org_providers.grant_model_to_org(
            db_pool, org_b["id"], "test-shared-twice", granted_by=owner_b["id"]
        )

        assert first["model_id"] != second["model_id"]
        assert first["granted"] and second["granted"]

    async def test_grant_uses_tenant_owner_as_grantor(
        self, db_pool, tenant, clean_test_data
    ):
        """An operator is not a tenant member, so the grant must not name them."""
        org, _ = tenant
        operator_org = await OrganizationRepository(db_pool).create(
            name=f"{clean_test_data}operatororg"
        )
        operator = await UserRepository(db_pool).create(
            external_id=f"{clean_test_data}op",
            provider="local",
            organization_id=operator_org["id"],
            email=f"{clean_test_data}op@test.com",
            display_name=f"{clean_test_data}Op",
            role="hyperadmin",
        )
        await db_pool.execute(
            "INSERT INTO models (id, provider, name, organization_id, is_enabled) "
            "VALUES ('test-grantor', 'openai', 'Grantor', NULL, true) "
            "ON CONFLICT (id) DO NOTHING"
        )

        # Passing the operator must still succeed: the service substitutes the
        # tenant's own owner before writing the clearance.
        result = await org_providers.grant_model_to_org(
            db_pool, org["id"], "test-grantor", granted_by=operator["id"]
        )
        assert result["granted"] is True

        granted_by = await db_pool.fetchval(
            "SELECT acl.granted_by FROM auth_clearances acl "
            "  JOIN models m ON m.auth_id = acl.auth_id "
            " WHERE m.id = $1 AND acl.principal_type = 'org'",
            result["model_id"],
        )
        owner_id = await db_pool.fetchval(
            "SELECT id FROM users WHERE organization_id = $1::uuid AND role = 'owner'",
            org["id"],
        )
        assert str(granted_by) == str(owner_id)
