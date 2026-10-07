"""Memory repository clearance-mode usage reads and policy wiring.

Memory *use* (search, detail reads, tags, export) is clearance-driven and
default-deny on `memories`: ownership (migration 123) and org/user/group
read grants live in auth_clearances; migration 129 retired the legacy
`memory_access_grants` table. Admins/owners keep the org-wide management
view on the legacy path. A plain pool used for a usage read is fail-closed.
These tests pin that split without a database.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from lucent.db.memory import (
    MEMORY_AUTH_POLICY,
    MemoryRepository,
    get_authorized_memories_pool,
)
from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthorizedQueryError,
    AuthPrincipal,
    AuthTablePolicy,
)

MAKE_USER = {"id": str(uuid4()), "organization_id": str(uuid4())}

MEMORY_POLICIES = (MEMORY_AUTH_POLICY,)


class FakeConnection:
    def __init__(self, *, rows=(), count_total=0, identity_row=None):
        self.rows = rows
        self.count_total = count_total
        self.identity_row = identity_row
        self.queries = []
        self.parameters = []

    def transaction(self):
        outer = self

        class Context:
            async def __aenter__(self):
                return outer

            async def __aexit__(self, *args):
                return False

        return Context()

    async def execute(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        return "OK"

    async def fetchrow(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        q = query.lower()
        if "from users where id" in q:
            return self.identity_row
        if "count(*)" in q:
            return {"total": self.count_total}
        return self.rows[0] if self.rows else None

    async def fetchval(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        q = query.lower()
        if "select exists(" in q:
            return True
        return True

    async def fetch(self, query, *parameters):
        self.queries.append(query)
        self.parameters.append(parameters)
        return list(self.rows)


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
        return []


def make_authorized_pool(connection, policies):
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    return AuthorizedDatabasePool(FakePool(connection), principal, policies)


def _full_memory_row(**overrides):
    data = {
        "id": uuid4(),
        "username": "alice.example",
        "type": "experience",
        "content": "deploy checklist learned the hard way",
        "tags": ["deploy"],
        "importance": 6,
        "related_memory_ids": [],
        "metadata": {},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "deleted_at": None,
        "user_id": MAKE_USER["id"],
        "organization_id": MAKE_USER["organization_id"],
        "shared": False,
        "last_accessed_at": None,
        "version": 1,
        "lifecycle_stage": "active",
        "vitality_score": 0.5,
        "vitality_computed_at": None,
        "sim_score": None,
    }
    data.update(overrides)
    return data


class Row:
    def __init__(self, mapping):
        self._data = dict(mapping)

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)


async def test_memory_policy_grants_nothing_by_default():
    """The memories usage policy has no direct columns: clearances decide."""
    assert MEMORY_AUTH_POLICY.table == "memories"
    assert MEMORY_AUTH_POLICY.direct_columns == ()


async def test_get_authorized_memories_pool_carries_memory_policy_only():
    fake_pool = FakePool(FakeConnection())
    with patch(
        "lucent.db.pool.get_pool",
        new=AsyncMock(return_value=fake_pool),
    ):
        pool = await get_authorized_memories_pool(fake_pool, MAKE_USER)
    assert str(pool.principal.user_id) == MAKE_USER["id"]
    assert str(pool.principal.organization_id) == MAKE_USER["organization_id"]
    assert pool.table_policies == MEMORY_POLICIES


async def test_search_usage_arm_is_clearance_only():
    """Usage search rewrites the memories reference through the clearance
    probe and keeps the org predicate as defense in depth."""
    connection = FakeConnection(
        rows=[Row(_full_memory_row())],
        count_total=1,
        identity_row={"role": "member", "external_id": None},
    )
    repo = MemoryRepository(make_authorized_pool(connection, MEMORY_POLICIES))
    result = await repo.search(
        "deploy",
        memory_ids=None,
        type="experience",
        requesting_user_id=MAKE_USER["id"],
        requesting_org_id=MAKE_USER["organization_id"],
    )

    count_query, page_query = [
        q for q in connection.queries if "FROM (SELECT resource.*" in q
    ][:2]
    for query in (count_query, page_query):
        assert "FROM (SELECT resource.*" in query
        assert "JOIN auth_clearances AS clearance" in query
        assert "organization_id = $1" in query  # defense in depth
        assert "memory_access_grants" not in query
        assert "Lucent Daemon" not in query
        assert "IN ('admin', 'owner')" not in query
    assert result["total_count"] == 1
    assert result["memories"][0]["content"] == "deploy checklist learned the hard way"


async def test_get_accessible_is_clearance_only():
    connection = FakeConnection(
        rows=[Row(_full_memory_row(type="goal", shared=False))]
    )
    pool = make_authorized_pool(connection, MEMORY_POLICIES)
    repo = MemoryRepository(pool)
    memory = await repo.get_accessible(
        uuid4(),
        user_id=MAKE_USER["id"],
        organization_id=MAKE_USER["organization_id"],
    )
    assert memory["type"] == "goal"
    query = next(q for q in connection.queries if "FROM (SELECT resource.*" in q)
    assert query
    assert "JOIN auth_clearances AS clearance" in query
    assert "organization_id = $2" in query
    assert "deleted_at IS NULL" in query


async def test_get_accessible_fails_closed_without_authorized_pool():
    """A plain pool without a requesting identity never runs an org-wide read."""
    repo = MemoryRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.get_accessible(
            uuid4(),
            user_id=None,
            organization_id=MAKE_USER["organization_id"],
        )


async def test_search_fails_closed_without_authorized_pool():
    repo = MemoryRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.search(
            "anything",
            requesting_user_id=MAKE_USER["id"],
            requesting_org_id=None,
        )


async def test_plain_pool_authorized_on_the_fly_from_requesting_identity():
    """A plain pool authorized on the fly rewrites through the clearance probe."""
    connection = FakeConnection(
        rows=[],
        count_total=0,
        identity_row={"role": "member", "external_id": None},
    )
    repo = MemoryRepository(FakePool(connection))
    await repo.get_accessible(
        uuid4(),
        user_id=MAKE_USER["id"],
        organization_id=MAKE_USER["organization_id"],
    )
    real_query = next(
        q for q in connection.queries if "FROM (SELECT resource.*" in q
    )
    assert "JOIN auth_clearances AS clearance" in real_query


async def test_unscoped_daemon_identity_maps_to_org_first_owner():
    """An unscoped daemon-service key acts as the org's first owner."""
    owner_id = str(uuid4())
    connection = FakeConnection(
        rows=[],
        count_total=0,
        identity_row={"role": "local-service", "external_id": "daemon-service:abc"},
    )
    pool = FakePool(connection)

    with patch(
        "lucent.db.memory.get_first_memory_owner",
        new=AsyncMock(
            return_value={"id": owner_id, "display_name": "Owner", "organization_id": "??"}
        ),
    ):
        repo = MemoryRepository(pool)
        authed = await repo._usage_pool(
            MAKE_USER["id"], MAKE_USER["organization_id"]
        )
    assert str(authed.principal.user_id) == owner_id


async def test_org_shared_only_daemon_key_acts_as_itself():
    """An org_shared_only daemon key matches org read clearances: no mapping."""
    connection = FakeConnection(
        identity_row={"role": "local-service", "external_id": "daemon-service:abc"}
    )
    repo = MemoryRepository(FakePool(connection))
    authed = await repo._usage_pool(
        MAKE_USER["id"],
        MAKE_USER["organization_id"],
        memory_scope="org_shared_only",
    )
    assert str(authed.principal.user_id) == MAKE_USER["id"]


async def test_get_accessible_on_wrong_policy_pool_fails_closed():
    principal = AuthPrincipal(uuid4(), "alice.example", uuid4())
    pool = AuthorizedDatabasePool(
        FakePool(FakeConnection()),
        principal,
        (AuthTablePolicy("schedules"),),
    )
    with pytest.raises(AuthorizedQueryError):
        await MemoryRepository(pool).get_accessible(
            uuid4(),
            user_id=MAKE_USER["id"],
            organization_id=MAKE_USER["organization_id"],
        )


async def test_find_duplicate_technical_memory_is_clearance_only():
    connection = FakeConnection()
    pool = make_authorized_pool(connection, MEMORY_POLICIES)
    repo = MemoryRepository(pool)
    duplicate = await repo.find_duplicate_technical_file_memory(
        metadata={"repo": "org/repo", "filename": "src/app.py"},
        requesting_user_id=MAKE_USER["id"],
        requesting_org_id=MAKE_USER["organization_id"],
    )
    assert duplicate is None  # the fake answers nothing; the shape is what we pin
    query = next(q for q in connection.queries if "FROM (SELECT resource.*" in q)
    assert "FROM (SELECT resource.*" in query
    assert "JOIN auth_clearances AS clearance" in query
    assert "type = 'technical'" in query
    assert "organization_id = $3" in query


async def test_find_duplicate_fails_closed_without_identity():
    repo = MemoryRepository(FakePool(FakeConnection()))
    with pytest.raises(TypeError, match="AuthorizedDatabasePool"):
        await repo.find_duplicate_technical_file_memory(
            metadata={"repo": "org/repo", "filename": "src/app.py"},
            requesting_user_id=None,
            requesting_org_id=MAKE_USER["organization_id"],
        )


async def test_list_knowledge_tree_is_clearance_only():
    connection = FakeConnection()
    pool = make_authorized_pool(connection, MEMORY_POLICIES)
    repo = MemoryRepository(pool)
    await repo.list_knowledge_tree(
        organization_id=MAKE_USER["organization_id"],
        user_id=MAKE_USER["id"],
    )
    query = next(q for q in connection.queries if "FROM (SELECT resource.*" in q)
    assert "JOIN auth_clearances AS clearance" in query
    assert "organization_id = $1" in query
    assert "type = 'technical'" in query
    assert "metadata->>'repo'" in query


@patch("lucent.db.memory.scoped_acquire_on")
async def test_set_shared_files_and_removes_org_clearance(scoped_acquire_on_mock):
    connection = FakeConnection(rows=[Row(_full_memory_row(shared=False))])
    pool = FakePool(connection)

    @asynccontextmanager
    async def fake_scope(*args, **kwargs):
        async with pool.acquire() as conn:
            yield conn

    scoped_acquire_on_mock.side_effect = fake_scope
    repo = MemoryRepository(pool)

    await repo.set_shared(uuid4(), MAKE_USER["id"], shared=True)
    insert_sql = next(q for q in connection.queries if "INSERT INTO auth_clearances" in q)
    assert "principal_type" in insert_sql
    assert "memory_access_grants" not in insert_sql

    await repo.set_shared(uuid4(), MAKE_USER["id"], shared=False)
    delete_sql = next(q for q in connection.queries if "DELETE FROM auth_clearances" in q)
    assert "principal_type = 'org'" in delete_sql


@patch("lucent.db.memory.scoped_acquire_on")
async def test_create_files_daemon_reader_clearance_when_daemon_authors(
    scoped_acquire_on_mock,
):
    """A daemon key's write attributes to the first owner and files the
    daemon a read clearance — its org_shared_only searches still reach
    what it authored, without exposing the row to org members."""
    connection = FakeConnection(rows=[Row(_full_memory_row(shared=False))])
    pool = make_authorized_pool(connection, MEMORY_POLICIES)

    @asynccontextmanager
    async def fake_scope(*args, **kwargs):
        # create's writes run on the plain pool under the patched scope.
        async with pool.pool.acquire() as conn:
            yield conn

    scoped_acquire_on_mock.side_effect = fake_scope
    repo = MemoryRepository(pool)
    reader_id = uuid4()

    await repo.create(
        username="Kahinton",
        type="experience",
        content="assessment captured by the daemon, owned by a human",
        user_id=uuid4(),
        organization_id=uuid4(),
        daemon_reader_id=reader_id,
    )

    insert_sql = next(
        q for q in connection.queries if "INSERT INTO auth_clearances" in q
    )
    assert "'read', 'user'" in insert_sql
    assert "memory_access_grants" not in insert_sql
    # No org clearance: sharing stays the owner's explicit choice.
    assert "principal_type = 'org'" not in insert_sql


@patch("lucent.db.memory.scoped_acquire_on")
async def test_create_skips_daemon_clearance_without_daemon_reader(
    scoped_acquire_on_mock,
):
    connection = FakeConnection(rows=[Row(_full_memory_row(shared=False))])
    pool = make_authorized_pool(connection, MEMORY_POLICIES)

    @asynccontextmanager
    async def fake_scope(*args, **kwargs):
        # create's writes run on the plain pool under the patched scope.
        async with pool.pool.acquire() as conn:
            yield conn

    scoped_acquire_on_mock.side_effect = fake_scope
    repo = MemoryRepository(pool)

    await repo.create(
        username="Kahinton",
        type="experience",
        content="a human's note",
        user_id=uuid4(),
        organization_id=uuid4(),
    )

    assert not [q for q in connection.queries if "INSERT INTO auth_clearances" in q]


@patch("lucent.db.memory.scoped_acquire_on")
async def test_grant_access_writes_clearance_with_principal_mapping(
    scoped_acquire_on_mock,
):
    grantee_id = uuid4()
    memory_row = Row({"id": uuid4(), "created_at": "2026-01-01T00:00:00+00:00"})
    connection = FakeConnection(rows=[memory_row])
    pool = FakePool(connection)

    @asynccontextmanager
    async def fake_scope(*args, **kwargs):
        async with pool.acquire() as conn:
            yield conn

    scoped_acquire_on_mock.side_effect = fake_scope
    repo = MemoryRepository(pool)

    grant = await repo.grant_access(
        uuid4(),
        MAKE_USER["organization_id"],
        grantee_type="user",
        grantee_id=grantee_id,
        created_by=MAKE_USER["id"],
    )
    insert_sql = next(q for q in connection.queries if "INSERT INTO auth_clearances" in q)
    assert "'read'" in insert_sql
    assert "memory_access_grants" not in insert_sql
    assert grant["grantee_type"] == "user"
    assert str(grant["grantee_user_id"]) == str(grantee_id)
    assert grant["grantee_group_id"] is None


@patch("lucent.db.memory.scoped_acquire_on")
async def test_revoke_access_deletes_clearance(scoped_acquire_on_mock):
    connection = FakeConnection()
    pool = FakePool(connection)

    @asynccontextmanager
    async def fake_scope(*args, **kwargs):
        async with pool.acquire() as conn:
            yield conn

    scoped_acquire_on_mock.side_effect = fake_scope
    repo = MemoryRepository(pool)

    revoked = await repo.revoke_access(
        uuid4(),
        MAKE_USER["organization_id"],
        grantee_type="organization",
        grantee_id=None,
    )
    assert revoked is True
    delete_sql = next(q for q in connection.queries if "DELETE FROM auth_clearances" in q)
    assert "principal_type = $3::text" in delete_sql
    assert "role = 'read'" in delete_sql


@patch("lucent.db.memory.scoped_acquire_on")
async def test_list_access_grants_maps_clearances_to_grantee_shape(
    scoped_acquire_on_mock,
):
    user_principal = uuid4()
    group_principal = uuid4()
    connection = FakeConnection(
        rows=[
            Row(
                {
                    "id": uuid4(),
                    "memory_id": uuid4(),
                    "organization_id": MAKE_USER["organization_id"],
                    "principal_type": "org",
                    "principal_id": MAKE_USER["organization_id"],
                    "created_by": MAKE_USER["id"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "user_display_name": None,
                    "user_email": None,
                    "group_name": None,
                }
            ),
            Row(
                {
                    "id": uuid4(),
                    "memory_id": uuid4(),
                    "organization_id": MAKE_USER["organization_id"],
                    "principal_type": "user",
                    "principal_id": user_principal,
                    "created_by": MAKE_USER["id"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "user_display_name": "Bob",
                    "user_email": "bob@example.com",
                    "group_name": None,
                }
            ),
            Row(
                {
                    "id": uuid4(),
                    "memory_id": uuid4(),
                    "organization_id": MAKE_USER["organization_id"],
                    "principal_type": "group",
                    "principal_id": group_principal,
                    "created_by": MAKE_USER["id"],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "user_display_name": None,
                    "user_email": None,
                    "group_name": "Platform",
                }
            ),
        ]
    )
    pool = FakePool(connection)

    @asynccontextmanager
    async def fake_scope(*args, **kwargs):
        async with pool.acquire() as conn:
            yield conn

    scoped_acquire_on_mock.side_effect = fake_scope
    repo = MemoryRepository(pool)

    grants = await repo.list_access_grants(
        uuid4(), MAKE_USER["organization_id"]
    )
    query = connection.queries[0]
    assert "JOIN auth_clearances" in query
    assert "memory_access_grants" not in query

    by_type = {g["grantee_type"]: g for g in grants}
    assert by_type["organization"]["grantee_user_id"] is None
    assert by_type["organization"]["grantee_group_id"] is None
    assert str(by_type["user"]["grantee_user_id"]) == str(user_principal)
    assert by_type["user"]["user_display_name"] == "Bob"
    assert str(by_type["group"]["grantee_group_id"]) == str(group_principal)
    assert by_type["group"]["group_name"] == "Platform"
