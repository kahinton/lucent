import re
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from lucent.db.pool import (
    AuthorizedDatabaseConnection,
    AuthorizedDatabasePool,
    AuthorizedQueryError,
    AuthPrincipal,
    AuthTablePolicy,
    get_authorized_pool,
)


class FakeConnection:
    def __init__(self, *, principal_row=None, group_rows=()):
        self.principal_row = principal_row
        self.group_rows = group_rows
        self.config_calls = []
        self.queries = []
        self.parameters = []

    async def fetchrow(self, query, *parameters):
        if "FROM users" in query:
            return self.principal_row
        self.queries.append(query)
        self.parameters.append(parameters)
        return None

    async def fetchval(self, query, *parameters):
        if "FROM users" in query:
            return None
        self.queries.append(query)
        self.parameters.append(parameters)
        return None

    async def fetch(self, query, *parameters):
        if "FROM users" in query:
            return [self.principal_row]
        if "FROM user_groups" in query:
            return self.group_rows
        self.queries.append(query)
        self.parameters.append(parameters)
        return []

    async def execute(self, query, *parameters):
        self.config_calls.append((query, parameters))

    def _method(self, method_name):
        async def call(query, *parameters):
            self.queries.append(query)
            self.parameters.append(parameters)
            return []

        return call


class FakePool:
    def __init__(self, connection):
        self._connection = connection

    def acquire(self):
        class Context:
            async def __aenter__(self):
                return self.outer._connection

            async def __aexit__(self, *args):
                return False

        Context.outer = self
        return Context()

    async def fetch(self, query, *parameters):
        return await self._connection.fetch(query, *parameters)

    async def fetchrow(self, query, *parameters):
        return await self._connection.fetchrow(query, *parameters)


def make_principal_pool():
    user_id = uuid4()
    organization_id = uuid4()
    group_id = uuid4()
    row = {
        "user_id": str(user_id),
        "username": "alice.example",
        "organization_id": str(organization_id),
    }
    connection = FakeConnection(principal_row=row, group_rows=[{"group_id": str(group_id)}])
    fake_pool = FakePool(connection)
    return fake_pool, user_id, organization_id, group_id


async def test_get_authorized_pool_resolves_principals_from_database():
    fake_pool, user_id, organization_id, group_id = make_principal_pool()
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_pool("alice.example", "tasks")

    assert pool.principal.user_id == user_id
    assert pool.principal.organization_id == organization_id
    assert pool.principal.group_ids == (group_id,)
    assert pool.table_names == ("tasks",)


async def test_authorized_pool_scopes_multiple_table_references():
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(
        FakePool(connection),
        principal,
        (
            AuthTablePolicy("projects"),
            AuthTablePolicy("tasks"),
        ),
    )

    async with pool.acquire() as authorized:
        await authorized.fetch(
            """
            SELECT project.id
            FROM projects AS project
            JOIN tasks t ON t.project_id = project.id
            WHERE project.id = $1
            """,
            principal.user_id,
        )

    query = connection.queries[0]
    assert query.count("JOIN auth_clearances AS clearance") == 2
    assert query.count(") AS principal") == 2
    assert query.count(") AS clearance ON TRUE") == 2
    assert "NULLIF(current_setting('app.auth_group_ids', true), '')" in query
    assert "FROM (SELECT resource.*" in query
    assert "FROM projects AS resource" in query
    assert "JOIN (SELECT resource.*" in query
    assert "FROM tasks AS resource" in query
    assert "WHEN 'user' THEN" in query
    assert "WHEN 'group' THEN" in query
    assert "WHEN 'org' THEN" in query
    assert "current_setting('app.org_id', true)::uuid" in query
    assert "app.organization_id" not in query
    assert "resource.user_id = current_setting" in query
    assert "resource.organization_id = current_setting" in query
    assert "$1" in query
    assert connection.parameters[0] == (principal.user_id,)


async def test_authorized_pool_scopes_special_fetch_shapes():
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(
        FakePool(connection),
        principal,
        (
            AuthTablePolicy("projects"),
            AuthTablePolicy("tasks"),
        ),
    )

    async with pool.acquire() as authorized:
        await authorized.fetchrow(
            "SELECT id, title FROM tasks WHERE id = $1",
            principal.user_id,
        )
        await authorized.fetchval(
            """
            SELECT p.id, (
                SELECT COUNT(*) FROM tasks WHERE tasks.project_id = p.id
            ) AS task_count
            FROM projects
            WHERE projects.id = $1
            """,
            principal.user_id,
        )

    single_row_query = connection.queries[0]
    assert "FROM (SELECT resource.*" in single_row_query
    assert ") AS tasks WHERE id = $1" in single_row_query
    assert "AS WHERE" not in single_row_query
    assert connection.parameters[0] == (principal.user_id,)

    aggregate_query = connection.queries[1]
    assert "SELECT COUNT(*)" in aggregate_query
    assert "SELECT COUNT(*) FROM (SELECT resource.*" in aggregate_query
    assert "FROM tasks AS resource" in aggregate_query
    assert "FROM projects AS resource" in aggregate_query
    assert "task_count" in aggregate_query
    assert connection.parameters[1] == (principal.user_id,)


async def test_authorized_connection_sets_direct_and_clearance_context():
    principal = AuthPrincipal(
        uuid4(),
        "alice.example",
        uuid4(),
        group_ids=(uuid4(),),
    )
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(
        FakePool(connection), principal, (AuthTablePolicy("tasks"),), "write"
    )

    async with pool.acquire():
        pass

    _, parameters = connection.config_calls[0]
    assert parameters == (
        str(principal.user_id),
        str(principal.organization_id),
        str(principal.group_ids[0]),
        "write",
    )
    assert "false" in connection.config_calls[0][0]
    # One round trip per acquire: the set, and nothing else. The release-side
    # scrub is the pool reset hook's job (_reset_connection), not this block's.
    assert len(connection.config_calls) == 1


async def test_authorized_pool_supports_grant_only_table():
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(
        FakePool(connection),
        principal,
        (AuthTablePolicy("secrets", direct_columns=()),),
    )

    async with pool.acquire() as authorized:
        await authorized.fetch("SELECT id FROM secrets")

    query = connection.queries[0]
    assert "JOIN auth_clearances AS clearance" in query
    assert "resource.user_id" not in query


async def test_authorized_pool_uses_role_hierarchy():
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("secrets", direct_columns=()),),
        "write",
    )

    sql = pool._authorized_table_sql(pool.table_policies[0])

    assert "WHEN 'read' THEN 1" in sql
    assert "WHEN 'write' THEN 2" in sql
    assert "WHEN 'owner' THEN 3" in sql
    assert "clearance.role" in sql
    assert "current_setting('app.auth_context', true)" in sql


async def test_authorized_pool_rejects_quoted_table_references():
    """Quoted identifiers cannot be re-scoped; they must fail the query.

    A query mixing an unquotable reference with a rewritten one would
    otherwise run the quoted reference completely unscoped.
    """
    principal = AuthPrincipal(uuid4(), "alice", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(FakePool(connection), principal, (AuthTablePolicy("tasks"),))
    authorized = AuthorizedDatabaseConnection(connection, pool)

    with pytest.raises(AuthorizedQueryError, match="unquoted identifier"):
        await authorized.fetch('SELECT t.id FROM "tasks" AS t')

    # Quoted references leak even when another configured table rewrites.
    principal2 = AuthPrincipal(uuid4(), "alice", uuid4())
    pool2 = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal2,
        (AuthTablePolicy("tasks"), AuthTablePolicy("projects")),
    )
    authorized2 = AuthorizedDatabaseConnection(FakeConnection(), pool2)
    with pytest.raises(AuthorizedQueryError, match="unquoted identifier"):
        await authorized2.fetch(
            'SELECT * FROM "projects" JOIN tasks t ON t.project_id = "projects".id'
        )


async def test_authorized_pool_rewrites_schema_qualified_references():
    """FROM public.<table> rewrites and drops the un-usable schema qualifier."""
    principal = AuthPrincipal(uuid4(), "alice", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(FakePool(connection), principal, (AuthTablePolicy("tasks"),))

    async with pool.acquire() as authorized:
        await authorized.fetch("SELECT t.id FROM public.tasks AS t")

    rewritten = connection.queries[0]
    assert "public." not in rewritten
    assert "FROM (SELECT resource.*" in rewritten
    assert "AS t" in rewritten


async def test_authorized_pool_rewrites_all_comma_separated_references():
    """``FROM tasks a, tasks b`` rewraps both references; none stays unscoped."""
    principal = AuthPrincipal(uuid4(), "alice", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(FakePool(connection), principal, (AuthTablePolicy("tasks"),))

    async with pool.acquire() as authorized:
        await authorized.fetch(
            "SELECT a.id FROM tasks a, tasks b WHERE a.id = b.id"
        )

    query = connection.queries[0]
    assert len(re.findall(r"\) AS a,", query)) == 1
    assert query.endswith(") AS b WHERE a.id = b.id")
    assert query.count("FROM tasks AS resource") == 2


async def test_table_policy_rejects_direct_column_without_principal_mapping():
    """A direct column no GUC ever sets would compile to an always-false predicate."""
    with pytest.raises(ValueError, match="no principal session context mapping"):
        AuthTablePolicy("secrets", direct_columns=("owner_user_id",))
    with pytest.raises(ValueError, match="owner_user_id"):
        AuthTablePolicy(
            "secrets",
            auth_id_column="auth_id",
            direct_columns=("user_id", "owner_user_id"),
        )


async def test_authorized_pool_rejects_unconfigured_and_write_queries():
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    connection = FakeConnection()
    pool = AuthorizedDatabasePool(FakePool(connection), principal, (AuthTablePolicy("tasks"),))
    authorized = AuthorizedDatabaseConnection(connection, pool)

    with pytest.raises(AuthorizedQueryError, match="only supports configured tables"):
        await authorized.fetch("SELECT id FROM projects")
    with pytest.raises(AuthorizedQueryError, match="CTE shadows"):
        await authorized.fetch("WITH tasks AS (SELECT 1 AS id) SELECT id FROM tasks")
    with pytest.raises(AuthorizedQueryError, match="Manual auth-table"):
        await authorized.fetch(
            "SELECT id FROM tasks t JOIN auth_clearances ac ON ac.auth_id = t.auth_id"
        )
    with pytest.raises(AuthorizedQueryError, match="principal context"):
        await authorized.fetch("SELECT set_config('app.user_id', 'x', true) FROM tasks")
    with pytest.raises(AuthorizedQueryError, match="read-only"):
        await authorized.fetch("UPDATE tasks SET title = 'x'")
