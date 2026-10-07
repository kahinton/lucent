from uuid import uuid4

import pytest

from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthPrincipal,
    AuthTablePolicy,
)
from lucent.db.projects import (
    PROJECT_AUTH_POLICY,
    ProjectRepository,
    get_authorized_projects_pool,
)


class FakeConnection:
    def __init__(self, *, group_rows=(), query_results=()):
        self.group_rows = group_rows
        self.query_results = list(query_results)
        self.queries = []
        self.parameters = []
        self.config_calls = []

    async def fetch(self, query, *parameters):
        if "FROM user_groups" in query:
            return self.group_rows
        self.queries.append(query)
        self.parameters.append(parameters)
        return self.query_results.pop(0)

    async def fetchrow(self, query, *parameters):
        rows = await self.fetch(query, *parameters)
        return rows[0] if rows else None

    async def fetchval(self, query, *parameters):
        rows = await self.fetch(query, *parameters)
        return rows[0]["total"] if rows else None

    async def execute(self, query, *parameters):
        self.config_calls.append((query, parameters))

    def acquire(self):
        outer = self

        class Context:
            async def __aenter__(self):
                return outer

            async def __aexit__(self, *args):
                return False

        return Context()


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    async def fetch(self, query, *parameters):
        return await self.connection.fetch(query, *parameters)

    def acquire(self):
        return self.connection.acquire()


async def test_get_authorized_projects_pool_scopes_project_ownership():
    user_id = uuid4()
    organization_id = uuid4()
    group_id = uuid4()
    fake_pool = FakePool(FakeConnection(group_rows=[{"group_id": str(group_id)}]))

    pool = await get_authorized_projects_pool(
        fake_pool,
        {
            "id": str(user_id),
            "organization_id": str(organization_id),
            "display_name": "alice",
        },
    )

    assert pool.table_names == ("projects",)
    assert pool.principal.user_id == user_id
    assert pool.principal.organization_id == organization_id
    assert pool.principal.group_ids == (group_id,)
    sql = pool._authorized_table_sql(PROJECT_AUTH_POLICY)
    # Projects are clearance-only on usage reads: ownership rides the
    # 'owner' clearance that the migration-123 INSERT trigger files.
    assert "JOIN auth_clearances AS clearance" in sql
    assert "resource.user_id = current_setting" not in sql
    assert "resource.organization_id" not in sql


async def test_authorized_project_repository_uses_authorized_project_sql():
    principal_user_id = uuid4()
    principal = AuthPrincipal(principal_user_id, "alice", uuid4())
    project = {
        "id": principal_user_id,
        "session_count": 2,
        "file_count": 3,
    }
    connection = FakeConnection(
        query_results=[
            [{"total": 1}],
            [project],
            [project],
            [project],
        ]
    )
    pool = AuthorizedDatabasePool(
        FakePool(connection),
        principal,
        (PROJECT_AUTH_POLICY,),
    )
    repository = ProjectRepository(pool)

    result = await repository.list_owned()
    assert result["items"] == [project]
    assert result["total_count"] == 1
    assert "FROM (SELECT resource.*" in connection.queries[0]
    assert "FROM projects AS resource" in connection.queries[0]

    owned = await repository.get_owned_with_counts(principal_user_id)
    assert owned is not None
    assert "FROM projects AS resource" in connection.queries[1]

    single = await repository.get_owned(principal_user_id)
    assert single is not None
    assert "FROM projects AS resource" in connection.queries[2]


async def test_authorized_project_repository_rejects_wrong_policy():
    principal = AuthPrincipal(uuid4(), "alice", uuid4())
    repository = ProjectRepository(
        AuthorizedDatabasePool(
            FakePool(FakeConnection()),
            principal,
            (AuthTablePolicy("tasks"),),
        )
    )
    with pytest.raises(ValueError, match="projects policy"):
        await repository.get_owned(uuid4())
