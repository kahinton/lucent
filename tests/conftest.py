"""Test configuration and fixtures for Lucent."""

import os
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio

# Set test database URL before importing any db modules
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://lucent:change-me-insecure-dev-password@localhost:5433/lucent",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Disable effective rate limiting during tests
os.environ.setdefault("LUCENT_RATE_LIMIT_PER_MINUTE", "999999")
# Ensure secret provider can initialize in tests
os.environ.setdefault("LUCENT_SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("LUCENT_SECRET_PROVIDER", "builtin")

_ORG_RESTRICTIVE_TABLES = (
    "task_outputs",
    "reviews",
    "tasks",
    "requests",
    "schedules",
    "sandboxes",
    "sandbox_templates",
    "agent_definitions",
    "mcp_server_configs",
    "models",
    "skill_definitions",
)


def pytest_collection_modifyitems(items):
    """Classify tests from their resolved fixture graph."""
    for item in items:
        marker = "integration" if "db_pool" in item.fixturenames else "unit"
        item.add_marker(getattr(pytest.mark, marker))


async def _delete_test_organizations(conn, name_patterns: list[str]) -> None:
    """Delete test organizations after clearing non-cascading dependencies."""
    restrictive_tables = {
        row["table_name"]
        for row in await conn.fetch(
            "SELECT conrelid::regclass::text AS table_name "
            "FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = 'organizations'::regclass "
            "AND confdeltype IN ('a', 'r')"
        )
    }
    unknown_tables = restrictive_tables.difference(_ORG_RESTRICTIVE_TABLES)
    if unknown_tables:
        names = ", ".join(sorted(unknown_tables))
        raise RuntimeError(f"Test organization cleanup is missing tables: {names}")

    organization_ids = await conn.fetchval(
        "SELECT array_agg(id) FROM organizations WHERE name LIKE ANY($1::text[])",
        name_patterns,
    )
    if not organization_ids:
        return

    async with conn.transaction():
        for table in _ORG_RESTRICTIVE_TABLES:
            await conn.execute(
                f"DELETE FROM {table} WHERE organization_id = ANY($1::uuid[])",
                organization_ids,
            )
        await conn.execute(
            "DELETE FROM organizations WHERE id = ANY($1::uuid[])",
            organization_ids,
        )


@pytest.fixture
def delete_test_organizations():
    """Provide FK-aware organization cleanup to file-local test fixtures."""
    return _delete_test_organizations


@pytest.fixture(autouse=True)
def _bypass_ssrf_validation_in_tests(request):
    """Bypass SSRF URL validation for all tests except SSRF-specific ones.

    Tests in ``test_ssrf_protection.py`` explicitly test the validation
    logic, so they opt out of this fixture.  All other tests that create
    MCP servers with localhost/dummy URLs need the bypass.
    """
    test_module = request.module.__name__
    if "test_ssrf_protection" in test_module:
        yield
        return

    # Patch both import sites so validation is skipped everywhere.
    with (
        patch("lucent.url_validation.validate_url", side_effect=lambda url, **kw: url),
        patch("lucent.api.routers.definitions.validate_url", side_effect=lambda url, **kw: url),
        patch("lucent.services.mcp_discovery.validate_url", side_effect=lambda url, **kw: url),
    ):
        yield


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_pool():
    """Create one database pool for the test session.

    Test data fixtures remain function-scoped and clean up after themselves.
    """
    from lucent.db.pool import close_db, init_db

    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://lucent:change-me-insecure-dev-password@localhost:5433/lucent",
    )
    pool = await init_db(database_url)
    yield pool
    await close_db()


@pytest.fixture(scope="session", autouse=True)
def cleanup_orphaned_test_data(request):
    """Remove orphaned test data before and after the full test suite.

    Individual test fixtures should clean up after themselves, but if cleanup
    fails (e.g. test crash, missing cleanup code), this catches the leftovers.
    """
    import asyncio

    async def _cleanup():
        import asyncpg

        database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://lucent:change-me-insecure-dev-password@localhost:5433/lucent",
        )
        conn = await asyncpg.connect(database_url)
        try:
            async with conn.transaction():
                await _delete_test_organizations(conn, ["test_%", "mcp_other_%"])
                await conn.execute(
                    "DELETE FROM users WHERE external_id LIKE ANY($1::text[])",
                    ["test_%", "mcp_other_%"],
                )
        finally:
            await conn.close()

    has_integration_tests = any(
        item.get_closest_marker("integration") for item in request.session.items
    )
    if not has_integration_tests:
        yield
        return

    asyncio.run(_cleanup())
    yield
    asyncio.run(_cleanup())


@pytest_asyncio.fixture
async def clean_test_data(db_pool):
    """Fixture that cleans up test data after each test.

    Creates a unique test prefix and cleans up memories/users with that prefix.
    """
    test_id = str(uuid4())[:8]
    prefix = f"test_{test_id}_"

    yield prefix

    # Cleanup: Delete test data in correct order (respect foreign keys)
    async with db_pool.acquire() as conn:
        # Delete access and audit logs for test memories first
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        # Delete definitions owned/created by test users
        await conn.execute(
            "DELETE FROM agent_hooks WHERE hook_id IN "
            "(SELECT id FROM hook_definitions WHERE created_by IN "
            "(SELECT id FROM users WHERE external_id LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_managed_tools WHERE tool_id IN "
            "(SELECT id FROM managed_tool_definitions WHERE created_by IN "
            "(SELECT id FROM users WHERE external_id LIKE $1))",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM managed_tool_runs WHERE tool_id IN "
            "(SELECT id FROM managed_tool_definitions WHERE created_by IN "
            "(SELECT id FROM users WHERE external_id LIKE $1))",
            f"{prefix}%",
        )
        for tbl in (
            "agent_definitions",
            "skill_definitions",
        ):
            await conn.execute(
                f"DELETE FROM {tbl} WHERE created_by IN "
                "(SELECT id FROM users WHERE external_id LIKE $1)",
                f"{prefix}%",
            )
        await conn.execute(
            "DELETE FROM hook_definitions WHERE created_by IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM managed_tool_definitions WHERE created_by IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        # Delete user_groups for test users
        await conn.execute(
            "DELETE FROM user_groups WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        # Delete API keys
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        # Delete memories
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        # Delete test users
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await _delete_test_organizations(conn, [f"{prefix}%"])


@pytest_asyncio.fixture
async def test_organization(db_pool, clean_test_data):
    """Create a test organization."""
    from lucent.db import OrganizationRepository

    prefix = clean_test_data
    repo = OrganizationRepository(db_pool)
    org = await repo.create(name=f"{prefix}org")
    return org


@pytest_asyncio.fixture
async def test_user(db_pool, test_organization, clean_test_data):
    """Create a test user with an organization."""
    from lucent.db import UserRepository

    prefix = clean_test_data
    repo = UserRepository(db_pool)
    user = await repo.create(
        external_id=f"{prefix}user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{prefix}user@test.com",
        display_name=f"{prefix}Test User",
    )
    return user


@pytest_asyncio.fixture
async def test_memory(db_pool, test_user, clean_test_data):
    """Create a test memory."""
    from lucent.db import MemoryRepository

    prefix = clean_test_data
    repo = MemoryRepository(db_pool)
    memory = await repo.create(
        username=f"{prefix}user",
        type="experience",
        content=f"{prefix} This is a test memory for testing",
        tags=["test", "fixture"],
        importance=5,
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )
    return memory
