"""Integration tests for non-interactive provisioning (lucent.bootstrap).

These exercise the parts of the bootstrap that need the real schema: adopting a
global seed model into an organisation, enabling it, and granting organisation
read clearance so the instance is actually usable without provider credentials.
"""

from __future__ import annotations

import pytest_asyncio

from lucent import bootstrap
from lucent.db import OrganizationRepository, UserRepository


@pytest_asyncio.fixture
async def bootstrap_org_owner(db_pool, clean_test_data):
    """A fresh organisation with one owner, cleaned up by ``clean_test_data``."""
    prefix = clean_test_data
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}bootorg")
    owner = await UserRepository(db_pool).create(
        external_id=f"{prefix}bootowner",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}bootowner@test.com",
        display_name=f"{prefix}Boot Owner",
        role="owner",
    )
    return org, owner, prefix


@pytest_asyncio.fixture
async def global_model(db_pool, clean_test_data):
    """A global (organization_id NULL), disabled model row, removed afterwards."""
    model_id = f"{clean_test_data}globalmodel"
    await db_pool.execute(
        "INSERT INTO models (id, provider, name, is_enabled, discovery_source) "
        "VALUES ($1, 'test', 'Bootstrap Test Model', false, 'seed')",
        model_id,
    )
    yield model_id
    await db_pool.execute("DELETE FROM models WHERE id = $1", model_id)


async def _clearance_count(db_pool, model_id: str) -> int:
    return await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM auth_clearances acl
        JOIN models m ON m.auth_id = acl.auth_id
        WHERE m.id = $1 AND acl.role = 'read' AND acl.principal_type = 'org'
        """,
        model_id,
    )


class TestEnsureUsableModel:
    async def test_adopts_enables_and_grants(
        self, db_pool, bootstrap_org_owner, global_model
    ):
        org, owner, _ = bootstrap_org_owner
        setup = await bootstrap.ensure_usable_model(
            db_pool, org_id=str(org["id"]), granted_by=str(owner["id"]), model_id=global_model
        )

        assert setup.model_id == global_model
        assert setup.adopted is True  # global row promoted into the org
        assert setup.enabled and setup.granted

        row = await db_pool.fetchrow(
            "SELECT organization_id, is_enabled FROM models WHERE id = $1", global_model
        )
        assert str(row["organization_id"]) == str(org["id"])
        assert row["is_enabled"] is True
        assert await _clearance_count(db_pool, global_model) == 1

    async def test_is_idempotent(self, db_pool, bootstrap_org_owner, global_model):
        org, owner, _ = bootstrap_org_owner
        args = dict(org_id=str(org["id"]), granted_by=str(owner["id"]), model_id=global_model)

        first = await bootstrap.ensure_usable_model(db_pool, **args)
        second = await bootstrap.ensure_usable_model(db_pool, **args)

        assert first.adopted is True
        assert second.adopted is False  # already org-scoped
        assert second.enabled and second.granted
        # Upsert must not accumulate duplicate clearance rows.
        assert await _clearance_count(db_pool, global_model) == 1

    async def test_noop_when_org_already_has_usable_model(
        self, db_pool, bootstrap_org_owner, global_model
    ):
        org, owner, _ = bootstrap_org_owner
        await bootstrap.ensure_usable_model(
            db_pool, org_id=str(org["id"]), granted_by=str(owner["id"]), model_id=global_model
        )

        # No explicit model: the existing usable model is returned untouched.
        setup = await bootstrap.ensure_usable_model(
            db_pool, org_id=str(org["id"]), granted_by=str(owner["id"])
        )
        assert setup.model_id == global_model
        assert setup.adopted is False


class TestProvisionInstanceAdoption:
    async def test_adopts_existing_owner_without_duplicating(
        self, db_pool, bootstrap_org_owner, global_model
    ):
        org, owner, prefix = bootstrap_org_owner
        # The shared test DB already has a human user, so provision_instance
        # takes the adopt path; scope it to our org with org_name.
        result = await bootstrap.provision_instance(
            db_pool,
            display_name=f"{prefix}Boot Owner",
            email=None,
            password="ValidPass1",
            model_id=global_model,
            org_name=f"{prefix}bootorg",
        )

        assert result.created_user is False
        assert str(result.user["id"]) == str(owner["id"])
        assert result.model is not None
        assert result.model.model_id == global_model
        assert result.api_key is None  # no new key on a repeat run


class TestProvisionDryRun:
    async def test_dry_run_writes_nothing(self, db_pool, bootstrap_org_owner, global_model):
        org, owner, prefix = bootstrap_org_owner
        before = await db_pool.fetchval(
            "SELECT organization_id FROM models WHERE id = $1", global_model
        )
        result = await bootstrap.provision_instance(
            db_pool,
            display_name=f"{prefix}Boot Owner",
            email=None,
            password="ValidPass1",
            model_id=global_model,
            org_name=f"{prefix}bootorg",
            dry_run=True,
        )
        after = await db_pool.fetchval(
            "SELECT organization_id FROM models WHERE id = $1", global_model
        )
        assert result.dry_run is True
        assert before == after  # model was not adopted
        assert await _clearance_count(db_pool, global_model) == 0
