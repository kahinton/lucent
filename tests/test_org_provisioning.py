"""Integration tests for hyperadmin organization provisioning.

These exercise the path an external signup site takes: create a tenant, give it a
first admin, mint a default key, and make the tenant immediately usable. They are
deliberately end-to-end (real schema, real repositories) because the failure mode
being guarded against is a *partially* provisioned tenant — an organization row
with nobody in it, or an admin who cannot sign in.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from lucent.db import ApiKeyRepository, OrganizationRepository, UserRepository
from lucent.db.admin_audit import AdminAuditRepository
from lucent.db.organization import OrganizationNameConflictError
from lucent.services import org_provisioning


@pytest_asyncio.fixture
async def operator(db_pool, clean_test_data):
    """An instance operator in its own organization (the audit-trail owner)."""
    prefix = clean_test_data
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}oporg")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}operator",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}operator@test.com",
        display_name=f"{prefix}Operator",
        role="hyperadmin",
    )
    return org, user


class TestProvisionOrganization:
    async def test_creates_org_admin_and_key(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}tenant",
            admin_email=f"{prefix}owner@tenant.test",
            admin_display_name=f"{prefix}Tenant Owner",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )

        assert result.organization["name"] == f"{prefix}tenant"
        assert result.organization["status"] == "active"

        # The tenant has a real, usable first admin.
        assert result.user["organization_id"] == result.organization["id"]
        assert result.user["role"] == "owner"
        assert result.temporary_password, "a password was generated"
        assert result.api_key and result.api_key.startswith("hs_")

        # And that admin can actually authenticate with what was returned.
        from lucent.auth_providers import BasicAuthProvider

        authenticated = await BasicAuthProvider(db_pool).authenticate(
            {
                "username": f"{prefix}owner@tenant.test",
                "password": result.temporary_password,
            }
        )
        assert authenticated is not None
        assert authenticated["role"] == "owner"

        # The minted key authenticates and is bound to the tenant.
        key_info = await ApiKeyRepository(db_pool).verify(result.api_key)
        assert key_info is not None
        assert str(key_info["organization_id"]) == str(result.organization["id"])

    async def test_admin_role_can_be_requested(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}adminonly",
            admin_email=f"{prefix}admin@tenant.test",
            admin_role="admin",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        assert result.user["role"] == "admin"

    async def test_supplied_password_is_not_returned(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}withpw",
            admin_email=f"{prefix}pw@tenant.test",
            admin_password="Chosen-password-9",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        assert result.temporary_password is None

        from lucent.auth_providers import BasicAuthProvider

        authenticated = await BasicAuthProvider(db_pool).authenticate(
            {"username": f"{prefix}pw@tenant.test", "password": "Chosen-password-9"}
        )
        assert authenticated is not None

    async def test_weak_password_is_refused(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, operator_user = operator

        with pytest.raises(org_provisioning.InvalidProvisioningRequestError):
            await org_provisioning.provision_organization(
                db_pool,
                name=f"{prefix}weak",
                admin_email=f"{prefix}weak@tenant.test",
                admin_password="short",
                operator_org_id=operator_org["id"],
                operator_user_id=operator_user["id"],
            )

        # A refused password must not leave a headless tenant behind.
        assert await OrganizationRepository(db_pool).get_by_name(f"{prefix}weak") is None

    async def test_duplicate_name_is_refused(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, operator_user = operator

        await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}dup",
            admin_email=f"{prefix}dup1@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        with pytest.raises(org_provisioning.OrganizationNameTakenError):
            await org_provisioning.provision_organization(
                db_pool,
                name=f"{prefix}dup",
                admin_email=f"{prefix}dup2@tenant.test",
                operator_org_id=operator_org["id"],
                operator_user_id=operator_user["id"],
            )

    async def test_name_conflict_is_case_insensitive(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}CaseTenant",
            admin_email=f"{prefix}case1@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        with pytest.raises(org_provisioning.OrganizationNameTakenError):
            await org_provisioning.provision_organization(
                db_pool,
                name=f"{prefix}casetenant",
                admin_email=f"{prefix}case2@tenant.test",
                operator_org_id=operator_org["id"],
                operator_user_id=operator_user["id"],
            )

    async def test_first_user_conflict_rolls_back_the_org(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}first",
            admin_email=f"{prefix}shared@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        with pytest.raises(org_provisioning.FirstUserConflictError):
            await org_provisioning.provision_organization(
                db_pool,
                name=f"{prefix}second",
                admin_email=f"{prefix}shared@tenant.test",
                operator_org_id=operator_org["id"],
                operator_user_id=operator_user["id"],
            )

        # The second tenant must not exist: an org you cannot log into is worse
        # than a clean failure.
        assert await OrganizationRepository(db_pool).get_by_name(f"{prefix}second") is None

    async def test_seeds_builtin_definitions(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}seeded",
            admin_email=f"{prefix}seed@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        # Seeding is environment-dependent (the definitions ship in .github/), so
        # assert the summary shape rather than a particular count.
        assert isinstance(result.seeded, dict)
        assert set(result.seeded) == {
            "skills", "agents", "hooks", "managed_tools", "sandbox_templates",
        }


class TestProvisionWithProvidersAndSettings:
    """A tenant created in one call should be usable without a follow-up visit.

    These guard the console's "create org" form, which posts credentials and
    runtime settings alongside the name. Each part fails independently, so each
    is asserted separately — a tenant with a key but no settings is exactly the
    half-configured state that made the old form useless.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def secret_provider(self, db_pool):
        from lucent.secrets.builtin import BuiltinSecretProvider
        from lucent.secrets.registry import SecretRegistry

        SecretRegistry.register("builtin", BuiltinSecretProvider(db_pool))
        SecretRegistry.set_default("builtin")
        yield
        SecretRegistry.reset()

    async def test_providers_are_stored(
        self, db_pool, operator, clean_test_data, monkeypatch
    ):
        from lucent.services import org_providers

        prefix = clean_test_data
        operator_org, operator_user = operator

        # Discovery is stubbed: this test is about credential *storage*, and the
        # un-stubbed call reaches api.openai.com with a fake key. It used to pass
        # only because a per-provider discovery error was dropped silently;
        # provisioning now reports those in `warnings` (an unreachable provider
        # must not look like a clean tenant), so a live 401 would fail the run on
        # a machine with network access and pass on one without.
        async def _no_discovery(pool, organization_id, *, providers=None):
            return {"providers": [], "upserted_count": 0, "errors": []}

        monkeypatch.setattr(org_providers, "discover_models", _no_discovery)

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}withprovider",
            admin_email=f"{prefix}prov@tenant.test",
            providers=[{"provider": "openai", "api_key": "sk-test-xyz"}],
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )

        assert result.providers_configured == ["openai"]
        assert result.warnings == []

        credentials = await org_providers.list_credentials(
            db_pool, result.organization["id"]
        )
        openai_row = next(c for c in credentials if c["provider"] == "openai")
        assert openai_row["configured"] is True

    async def test_settings_are_applied(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}withsettings",
            admin_email=f"{prefix}settings@tenant.test",
            settings={
                "features.sandboxes_enabled": False,
                "memory.inject_max_memories": 2,
            },
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )

        assert result.settings_applied == {
            "features.sandboxes_enabled": False,
            "memory.inject_max_memories": 2,
        }

    async def test_unknown_setting_is_a_warning_not_a_failure(
        self, db_pool, operator, clean_test_data
    ):
        """An unappliable setting must not prevent the tenant from existing."""
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}badsetting",
            admin_email=f"{prefix}bad@tenant.test",
            settings={"features.sandboxes_enabled": False, "nope.not_a_setting": 1},
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )

        assert result.organization["status"] == "active"
        assert result.settings_applied == {"features.sandboxes_enabled": False}
        assert any("nope.not_a_setting" in w for w in result.warnings)


class TestProvisioningAudit:
    async def test_writes_operator_and_tenant_rows(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}audited",
            admin_email=f"{prefix}audit@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )

        repo = AdminAuditRepository(db_pool)

        operator_trail = await repo.list_instance_events(operator_org_id=operator_org["id"])
        matching = [
            entry
            for entry in operator_trail["entries"]
            if str(entry.get("target_organization_id")) == str(result.organization["id"])
        ]
        assert matching, "operator trail must record the provisioning"
        assert matching[0]["action"] == "org.provision"

        # And the tenant's own audit log must show the change too: an operator
        # action must never be invisible to the tenant it affected.
        tenant_trail = await repo.list_for_org(result.organization["id"])
        assert any(e["action"] == "org.provision" for e in tenant_trail["entries"])


class TestCreateFirstUser:
    async def test_rejects_hyperadmin_role(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, _ = operator
        org = await OrganizationRepository(db_pool).create(name=f"{prefix}norole")

        with pytest.raises(org_provisioning.InvalidProvisioningRequestError):
            await org_provisioning.create_first_user(
                db_pool,
                organization_id=org["id"],
                role="hyperadmin",
                email=f"{prefix}nope@tenant.test",
            )

    async def test_requires_an_identity(self, db_pool, operator, clean_test_data):
        prefix = clean_test_data
        operator_org, _ = operator
        org = await OrganizationRepository(db_pool).create(name=f"{prefix}noident")

        with pytest.raises(org_provisioning.InvalidProvisioningRequestError):
            await org_provisioning.create_first_user(
                db_pool, organization_id=org["id"], role="owner", email=None
            )


class TestDeleteOrganization:
    async def test_requires_matching_confirmation(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}deleteme",
            admin_email=f"{prefix}del@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )

        with pytest.raises(org_provisioning.InvalidProvisioningRequestError):
            await org_provisioning.delete_organization(
                db_pool,
                organization_id=result.organization["id"],
                operator_org_id=operator_org["id"],
                operator_user_id=operator_user["id"],
                confirm_name="the wrong name",
            )
        assert await OrganizationRepository(db_pool).get_by_id(
            result.organization["id"]
        ) is not None

    async def test_deletes_and_records_before_removal(
        self, db_pool, operator, clean_test_data
    ):
        prefix = clean_test_data
        operator_org, operator_user = operator

        result = await org_provisioning.provision_organization(
            db_pool,
            name=f"{prefix}purge",
            admin_email=f"{prefix}purge@tenant.test",
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
        )
        org_id = result.organization["id"]

        deleted = await org_provisioning.delete_organization(
            db_pool,
            organization_id=org_id,
            operator_org_id=operator_org["id"],
            operator_user_id=operator_user["id"],
            confirm_name=f"{prefix}purge",
        )
        assert deleted["deleted"] is True
        assert await OrganizationRepository(db_pool).get_by_id(org_id) is None

        # The audit row survives in the operator's organization even though the
        # tenant's own rows cascaded away.
        repo = AdminAuditRepository(db_pool)
        trail = await repo.list_instance_events(
            operator_org_id=operator_org["id"],
            action="org.delete",
        )
        assert any(
            str(entry.get("target_organization_id")) == str(org_id)
            for entry in trail["entries"]
        )

    async def test_refuses_the_system_org(self, db_pool, operator):
        operator_org, operator_user = operator
        from lucent.builtin_definitions import SYSTEM_ORG_NAME

        org_repo = OrganizationRepository(db_pool)
        system_org = await org_repo.get_by_name(SYSTEM_ORG_NAME)
        if system_org is None:
            pytest.skip("no internal system organization on this database")

        with pytest.raises(org_provisioning.InvalidProvisioningRequestError):
            await org_provisioning.delete_organization(
                db_pool,
                organization_id=system_org["id"],
                operator_org_id=operator_org["id"],
                operator_user_id=operator_user["id"],
                confirm_name=SYSTEM_ORG_NAME,
            )


class TestOrganizationNameUniqueness:
    async def test_repository_refuses_duplicate_names(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)

        await repo.create(name=f"{prefix}uniq")
        with pytest.raises(OrganizationNameConflictError):
            await repo.create(name=f"{prefix}uniq")

    async def test_rename_refuses_duplicate_names(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)

        await repo.create(name=f"{prefix}alpha")
        beta = await repo.create(name=f"{prefix}beta")

        with pytest.raises(OrganizationNameConflictError):
            await repo.update(beta["id"], f"{prefix}alpha")

    async def test_get_or_create_adopts_on_race(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)

        first, created_first = await repo.get_or_create(f"{prefix}race")
        second, created_second = await repo.get_or_create(f"{prefix}race")

        assert created_first is True
        assert created_second is False
        assert first["id"] == second["id"]

    async def test_get_by_name_is_case_insensitive(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)
        org = await repo.create(name=f"{prefix}MixedCase")

        found = await repo.get_by_name(f"{prefix}mixedcase")
        assert found is not None
        assert found["id"] == org["id"]


class TestOrganizationListFilters:
    async def test_excludes_hidden_names(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)
        await repo.create(name=f"{prefix}visible")

        result = await repo.list(
            exclude_names=[f"{prefix}visible"],
            include_stats=True,
        )
        assert all(
            org["name"] != f"{prefix}visible" for org in result["organizations"]
        )

    async def test_stats_counts_humans_not_daemon(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)
        org = await repo.create(name=f"{prefix}stats")

        user_repo = UserRepository(db_pool)
        await user_repo.create(
            external_id=f"{prefix}statowner",
            provider="local",
            organization_id=org["id"],
            email=f"{prefix}statowner@test.com",
            role="owner",
        )

        stats = await repo.get_stats(org["id"])
        # create() also provisions a daemon-service identity; it must not count
        # as a member, and the owner must.
        assert stats["user_count"] == 1
        assert stats["owner_count"] == 1
        assert stats["api_key_count"] == 0

    async def test_stats_counts_active_api_keys(self, db_pool, clean_test_data):
        prefix = clean_test_data
        repo = OrganizationRepository(db_pool)
        org = await repo.create(name=f"{prefix}keystats")
        user = await UserRepository(db_pool).create(
            external_id=f"{prefix}keyowner",
            provider="local",
            organization_id=org["id"],
            email=f"{prefix}keyowner@test.com",
            role="owner",
        )
        await ApiKeyRepository(db_pool).create(user["id"], org["id"], f"{prefix}k")

        assert (await repo.get_stats(org["id"]))["api_key_count"] == 1

        stats = await repo.list(search=f"{prefix}keystats", include_stats=True)
        row = next(
            o for o in stats["organizations"] if o["id"] == org["id"]
        )
        # The list view and the detail view must agree; a null here silently
        # blanks a column in the operator console.
        assert row["api_key_count"] == 1


class TestDeletedOrganizationRace:
    async def test_system_schedule_seeding_skips_a_deleted_org(
        self, db_pool, clean_test_data
    ):
        """Seeding must survive a tenant vanishing mid-sweep.

        Hyperadmins can delete a tenant at any moment while the server-side
        schedule runner loops every 30 seconds. The runner selects organizations
        and then inserts a schedule per org; if one is deleted in between, the
        insert raises a foreign-key violation. That used to abort the whole sweep
        and leave every *other* organization without its system schedules.
        """
        from lucent.api import system_schedules

        prefix = clean_test_data
        doomed = await OrganizationRepository(db_pool).create(name=f"{prefix}doomed")

        # Delete the org, then drive the seeding path with a stale row list so
        # the FK violation actually happens.
        assert await OrganizationRepository(db_pool).delete(doomed["id"]) is True

        original = system_schedules.ScheduleRepository.list_daemon_service_users

        async def stale_rows(self):
            return [
                {"organization_id": str(doomed["id"]), "daemon_user_id": None}
            ]

        system_schedules.ScheduleRepository.list_daemon_service_users = stale_rows
        try:
            created = await system_schedules.ensure_server_system_schedules()
        finally:
            system_schedules.ScheduleRepository.list_daemon_service_users = original

        # No exception, and nothing was created for the departed tenant.
        assert created == 0
