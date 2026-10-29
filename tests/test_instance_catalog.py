"""Instance catalog: operator-provisioned built-in definitions.

The whole point of these tests is the *boundary*, not the happy path. Three
properties have to hold or the feature is unsafe:

1. An operator row is visible to the tenant it was provisioned into, and to no
   other tenant.
2. The disk sync never adopts, overwrites, or prunes an operator row — even when
   the names collide. This is the failure that would be silent in production.
3. Provisioning grants nothing. A tenant admin still decides which agents may use
   the definition, and each user still consents.
"""

from __future__ import annotations

import pytest

from lucent.db.definitions import BuiltInProtectionError, DefinitionRepository

CONNECTOR = {
    "name": "acme-hosted-tool",
    "description": "A hosted connector the operator provides.",
    "server_type": "http",
    "url": "https://mcp.acme.example/mcp",
    "headers": {"Authorization": "secret://acme.mcp.token"},
    "credential_slots": [
        {
            "name": "ACME_TOKEN",
            "header": "Authorization",
            "format": "Bearer {value}",
            "label": "Acme token",
            "secret_key": "acme.mcp.token",
            "required": True,
        }
    ],
}


@pytest.fixture
async def two_orgs(db_pool):
    """Two independent tenants plus an operator actor.

    Deliberately self-contained: the shared ``actor`` fixture is torn down by
    the session cleanup, which would collide with the ``created_by`` foreign key on
    rows these tests deliberately leave in place. A local actor keeps teardown in
    one order.
    """
    from uuid import uuid4

    ids = []
    async with db_pool.acquire() as conn:
        for _ in range(2):
            tag = str(uuid4())[:8]
            org_id = await conn.fetchval(
                "INSERT INTO organizations (name) VALUES ($1) RETURNING id",
                f"test_catalog_{tag}",
            )
            ids.append(str(org_id))
    yield ids
    async with db_pool.acquire() as conn:
        # Definition rows first: they reference the org and (for connectors) the
        # actor. Deleting the org first trips the FK from any row left behind.
        for table in (
            "mcp_server_configs",
            "managed_tool_definitions",
            "skill_definitions",
            "agent_definitions",
            "hook_definitions",
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE organization_id = ANY($1::uuid[])", ids
            )
        await conn.execute(
            "DELETE FROM organizations WHERE id = ANY($1::uuid[])", ids
        )


@pytest.fixture
async def actor(db_pool, two_orgs):
    """A user in the first tenant, used as the provisioning actor."""
    from uuid import uuid4

    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval(
            """
            INSERT INTO users (external_id, provider, display_name, role,
                               organization_id)
            VALUES ($1, 'local', 'Catalog Actor', 'owner', $2)
            RETURNING id
            """,
            f"catalog_actor_{str(uuid4())[:8]}",
            two_orgs[0],
        )
    yield str(user_id)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM mcp_server_configs WHERE created_by = $1", user_id
        )
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest.fixture
def repo(db_pool):
    return DefinitionRepository(db_pool)


class TestProvisioning:
    async def test_creates_a_builtin_scoped_operator_row(self, repo, two_orgs, actor):
        org_id = two_orgs[0]
        row = await repo.provision_catalog_mcp_server(
            org_id=org_id, created_by=actor, **CONNECTOR
        )
        assert row["scope"] == "built-in"
        assert row["origin"] == "operator"
        assert str(row["organization_id"]) == org_id
        # No per-user owner: it belongs to the org, like a shipped built-in.
        assert row["owner_user_id"] is None
        assert row["owner_group_id"] is None

    async def test_credential_values_are_references_not_secrets(
        self, repo, two_orgs, actor
    ):
        row = await repo.provision_catalog_mcp_server(
            org_id=two_orgs[0], created_by=actor, **CONNECTOR
        )
        for value in (row.get("headers") or {}).values():
            assert str(value).startswith("secret://")
        assert row["credential_slots"][0]["secret_key"] == "acme.mcp.token"

    async def test_provisioning_is_idempotent(self, repo, two_orgs, actor):
        first = await repo.provision_catalog_mcp_server(
            org_id=two_orgs[0], created_by=actor, **CONNECTOR
        )
        second = await repo.provision_catalog_mcp_server(
            org_id=two_orgs[0],
            created_by=actor,
            **{**CONNECTOR, "description": "updated description"},
        )
        assert first["id"] == second["id"]
        assert second["description"] == "updated description"
        listed = await repo.list_catalog_definitions(two_orgs[0], "mcp_server")
        assert len(listed) == 1

    async def test_refuses_to_overwrite_a_tenant_definition(
        self, repo, two_orgs, actor
    ):
        """A tenant's own definition of that name is not the operator's to replace."""
        await repo.create_mcp_server(
            name=CONNECTOR["name"],
            description="tenant's own",
            server_type="http",
            url="https://tenant.example/mcp",
            org_id=two_orgs[0],
            created_by=actor,
            owner_user_id=actor,
            status="active",
        )
        with pytest.raises(BuiltInProtectionError, match="already exists"):
            await repo.provision_catalog_mcp_server(
                org_id=two_orgs[0], created_by=actor, **CONNECTOR
            )

    async def test_refuses_to_overwrite_a_disk_shipped_builtin(
        self, repo, two_orgs, actor
    ):
        """Naming a disk-shipped connector is a configuration error, not an update."""
        from lucent.services.builtin_connectors import BUILTIN_CONNECTORS

        disk = BUILTIN_CONNECTORS[0]
        await repo.sync_built_in_mcp_connectors(two_orgs[0])
        with pytest.raises(BuiltInProtectionError):
            await repo.provision_catalog_mcp_server(
                org_id=two_orgs[0],
                created_by=actor,
                name=disk.name,
                description="collision",
                server_type="http",
                url="https://elsewhere.example/mcp",
            )


class TestCrossTenantIsolation:
    async def test_a_row_exists_only_in_the_target_org(self, repo, two_orgs, actor):
        await repo.provision_catalog_mcp_server(
            org_id=two_orgs[0], created_by=actor, **CONNECTOR
        )
        assert len(await repo.list_catalog_definitions(two_orgs[0], "mcp_server")) == 1
        assert await repo.list_catalog_definitions(two_orgs[1], "mcp_server") == []

    async def test_revoke_affects_one_org_only(self, repo, two_orgs, actor):
        for org_id in two_orgs:
            await repo.provision_catalog_mcp_server(
                org_id=org_id, created_by=actor, **CONNECTOR
            )
        assert await repo.revoke_catalog_mcp_server(
            org_id=two_orgs[0], name=CONNECTOR["name"], actor_id=actor
        )
        assert await repo.list_catalog_definitions(two_orgs[0], "mcp_server") == []
        assert len(await repo.list_catalog_definitions(two_orgs[1], "mcp_server")) == 1

    async def test_revoke_refuses_a_row_it_did_not_create(
        self, repo, two_orgs, actor
    ):
        await repo.create_mcp_server(
            name="tenant-owned",
            description="tenant's own",
            server_type="http",
            url="https://tenant.example/mcp",
            org_id=two_orgs[0],
            created_by=actor,
            owner_user_id=actor,
            status="active",
        )
        assert not await repo.revoke_catalog_mcp_server(
            org_id=two_orgs[0], name="tenant-owned", actor_id=actor
        )

    async def test_fleet_view_groups_by_org(self, repo, two_orgs, actor):
        await repo.provision_catalog_mcp_server(
            org_id=two_orgs[0], created_by=actor, **CONNECTOR
        )
        await repo.provision_catalog_mcp_server(
            org_id=two_orgs[1], created_by=actor, **CONNECTOR
        )
        fleet = await repo.list_catalog_organizations("mcp_server")
        seen = {entry["organization_id"] for entry in fleet}
        assert set(two_orgs) <= seen


class TestDiskSyncCannotClobberOperatorRows:
    """The silent failure this feature would otherwise ship with.

    Both built-in syncs reconcile by ``(name, organization_id) AND scope='built-in'``.
    An operator row carries that scope, so without the ``origin`` guard the next
    server restart would overwrite it from disk — or archive it as "retired" if the
    disk catalogue does not know the name.
    """

    async def test_sync_does_not_overwrite_an_operator_connector(
        self, repo, two_orgs, actor
    ):
        from lucent.services.builtin_connectors import BUILTIN_CONNECTORS

        disk = BUILTIN_CONNECTORS[0]
        # Provision an operator row using the *same name as a shipped connector*
        # would be refused, so simulate the dangerous shape directly: an operator
        # row whose name the disk sync also manages.
        async with repo.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mcp_server_configs
                    (name, description, server_type, url, headers, args, env_vars,
                     credential_slots, status, scope, origin, organization_id)
                VALUES ($1, 'operator copy', 'http', 'https://operator.example/mcp',
                        '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '[]'::jsonb,
                        'active', 'built-in', 'operator', $2)
                """,
                disk.name,
                two_orgs[0],
            )
        await repo.sync_built_in_mcp_connectors(two_orgs[0])
        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, origin, description FROM mcp_server_configs "
                "WHERE name = $1 AND organization_id = $2",
                disk.name,
                two_orgs[0],
            )
        assert row["origin"] == "operator"
        assert row["url"] == "https://operator.example/mcp"
        assert row["description"] == "operator copy"

    async def test_prune_does_not_archive_an_operator_tool(
        self, repo, two_orgs, actor
    ):
        """A tool the disk catalogue does not carry must not be treated as retired."""
        async with repo.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO managed_tool_definitions
                    (name, description, input_schema, runtime_type, source_code,
                     entrypoint, requirements, runtime_config, env_vars,
                     auth_policy, network_policy, resource_limits,
                     timeout_seconds, status, scope, origin, organization_id)
                VALUES ($1, 'operator tool', '{"type":"object"}'::jsonb, 'builtin',
                        '', 'web_search', '[]'::jsonb,
                        '{"handler":"web_search"}'::jsonb, '{}'::jsonb,
                        '{"mode":"agent_grant","require_user_access":true}'::jsonb,
                        '{"network_mode":"none","allowed_hosts":[]}'::jsonb,
                        '{"memory_limit":"512m"}'::jsonb, 60, 'active',
                        'built-in', 'operator', $2)
                """,
                "operator_only_tool",
                two_orgs[0],
            )
        await repo.sync_built_in_managed_tools(two_orgs[0])
        async with repo.pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM managed_tool_definitions "
                "WHERE name = 'operator_only_tool' AND organization_id = $1",
                two_orgs[0],
            )
        assert status == "active"

    async def test_skill_sync_does_not_overwrite_an_operator_skill(
        self, repo, two_orgs, actor, tmp_path
    ):
        # Use a real shipped skill name so the disk sync would definitely touch it.
        import pathlib

        from lucent.services.builtin_tool_catalog import BUILTIN_TOOL_CATALOG

        skills_root = pathlib.Path(".github/skills")
        if not skills_root.is_dir():
            pytest.skip("built-in skills are not present in this checkout")
        name = sorted(p.name for p in skills_root.iterdir() if p.is_dir())[0]

        async with repo.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_definitions
                    (name, description, content, status, scope, origin,
                     organization_id)
                VALUES ($1, 'operator copy', 'operator body', 'active',
                        'built-in', 'operator', $2)
                """,
                name,
                two_orgs[0],
            )
        await repo.sync_built_in_skills(two_orgs[0], str(skills_root))
        async with repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content, origin FROM skill_definitions "
                "WHERE name = $1 AND organization_id = $2",
                name,
                two_orgs[0],
            )
        assert row["origin"] == "operator"
        assert row["content"] == "operator body"
        assert BUILTIN_TOOL_CATALOG  # keep the import meaningful across refactors


class TestProvisioningGrantsNothing:
    async def test_provisioned_connector_is_not_granted_to_any_agent(
        self, repo, two_orgs, actor
    ):
        row = await repo.provision_catalog_mcp_server(
            org_id=two_orgs[0], created_by=actor, **CONNECTOR
        )
        async with repo.pool.acquire() as conn:
            grants = await conn.fetchval(
                "SELECT count(*) FROM agent_mcp_servers WHERE mcp_server_id = $1",
                row["id"],
            )
        assert grants == 0, (
            "provisioning makes a connector available; granting it to an agent is "
            "the tenant's decision"
        )


class TestApiAuthorizationBoundary:
    """The routes must be reachable by an instance operator and by nobody else.

    This is the boundary the whole design turns on: provisioning writes rows into
    another tenant. A tenant owner is emphatically not enough.
    """

    @staticmethod
    def _client(app, role: str, org_id: str, user_id: str):
        import httpx
        from httpx import ASGITransport

        from lucent.api.deps import CurrentUser, get_current_user

        fake = CurrentUser(
            id=user_id,
            organization_id=org_id,
            role=role,
            email=f"{role}@test.example",
            display_name=role,
            auth_method="api_key",
            api_key_scopes=["read", "write"],
        )

        async def _override():
            return fake

        app.dependency_overrides[get_current_user] = _override
        return httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )

    async def test_tenant_owner_cannot_provision_into_another_org(
        self, two_orgs, actor
    ):
        from lucent.api.app import create_app

        app = create_app()
        # The actor is an owner of the *first* org, trying to write into the second.
        async with self._client(app, "owner", two_orgs[0], actor) as client:
            resp = await client.put(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp-servers",
                json=CONNECTOR,
            )
        assert resp.status_code == 403, resp.text

    async def test_tenant_admin_cannot_list_another_org_catalog(
        self, two_orgs, actor
    ):
        from lucent.api.app import create_app

        app = create_app()
        async with self._client(app, "admin", two_orgs[0], actor) as client:
            resp = await client.get(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp_server"
            )
        assert resp.status_code == 403, resp.text

    async def test_member_is_rejected(self, two_orgs, actor):
        from lucent.api.app import create_app

        app = create_app()
        async with self._client(app, "member", two_orgs[0], actor) as client:
            resp = await client.get(
                f"/api/organizations/{two_orgs[0]}/catalog/mcp_server"
            )
        assert resp.status_code == 403, resp.text

    async def test_hyperadmin_can_provision_and_read_back(
        self, repo, two_orgs, actor
    ):
        from lucent.api.app import create_app

        app = create_app()
        async with self._client(app, "hyperadmin", two_orgs[0], actor) as client:
            created = await client.put(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp-servers",
                json=CONNECTOR,
            )
            assert created.status_code == 200, created.text
            listed = await client.get(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp_server"
            )
        assert listed.status_code == 200, listed.text
        names = [item["name"] for item in listed.json()["items"]]
        assert CONNECTOR["name"] in names

    async def test_hyperadmin_revoke_removes_it(self, repo, two_orgs, actor):
        from lucent.api.app import create_app

        app = create_app()
        async with self._client(app, "hyperadmin", two_orgs[0], actor) as client:
            await client.put(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp-servers",
                json=CONNECTOR,
            )
            deleted = await client.delete(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp-servers"
                f"/{CONNECTOR['name']}"
            )
            assert deleted.status_code == 200, deleted.text
            again = await client.delete(
                f"/api/organizations/{two_orgs[1]}/catalog/mcp-servers"
                f"/{CONNECTOR['name']}"
            )
        assert again.status_code == 404
