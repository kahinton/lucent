"""Test configuration and fixtures for Lucent."""

import os
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio

# Set test database URL before importing any db modules
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Disable effective rate limiting during tests
os.environ.setdefault("LUCENT_RATE_LIMIT_PER_MINUTE", "999999")
# Ensure secret provider can initialize in tests
os.environ.setdefault("LUCENT_SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("LUCENT_SECRET_PROVIDER", "builtin")


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


@pytest_asyncio.fixture(scope="function")
async def db_pool():
    """Create a database pool for each test function.

    Uses the lucent database for tests since we clean up after ourselves.
    """
    import lucent.db.pool as pool_module
    from lucent.db.pool import close_db, init_db

    # Reset the global pool to None to ensure fresh connection
    pool_module._pool = None

    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
    )
    pool = await init_db(database_url)
    yield pool
    await close_db()


@pytest.fixture(scope="session", autouse=True)
def cleanup_orphaned_test_data():
    """Session-scoped safety net: remove orphaned test data after the full test suite.

    Individual test fixtures should clean up after themselves, but if cleanup
    fails (e.g. test crash, missing cleanup code), this catches the leftovers.
    Runs once after ALL tests complete.
    """
    yield

    import asyncio

    async def _cleanup():
        import asyncpg

        database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://lucent:lucent_dev_password@localhost:5433/lucent",
        )
        conn = await asyncpg.connect(database_url)
        try:
            # Delete test data in FK-safe order.
            # Test users have external_id starting with 'test_' or 'mcp_other_'.
            test_user_filter = (
                "external_id LIKE 'test_%' OR external_id LIKE 'mcp_other_%'"
            )
            test_org_filter = (
                "name LIKE 'test_%' OR name LIKE 'mcp_other_%'"
            )

            # Reviews, tasks, requests for test orgs
            await conn.execute(
                f"DELETE FROM reviews WHERE organization_id IN "
                f"(SELECT id FROM organizations WHERE {test_org_filter})"
            )
            await conn.execute(
                f"DELETE FROM tasks WHERE request_id IN "
                f"(SELECT id FROM requests WHERE organization_id IN "
                f"(SELECT id FROM organizations WHERE {test_org_filter}))"
            )
            await conn.execute(
                f"DELETE FROM requests WHERE organization_id IN "
                f"(SELECT id FROM organizations WHERE {test_org_filter})"
            )
            # Memories owned by test users (CASCADE should handle this, but be explicit)
            await conn.execute(
                f"DELETE FROM memory_audit_log WHERE memory_id IN "
                f"(SELECT id FROM memories WHERE user_id IN "
                f"(SELECT id FROM users WHERE {test_user_filter}))"
            )
            await conn.execute(
                f"DELETE FROM memory_access_log WHERE memory_id IN "
                f"(SELECT id FROM memories WHERE user_id IN "
                f"(SELECT id FROM users WHERE {test_user_filter}))"
            )
            await conn.execute(
                f"DELETE FROM memories WHERE user_id IN "
                f"(SELECT id FROM users WHERE {test_user_filter})"
            )
            # Definitions, API keys, groups for test users
            for tbl in ("agent_definitions", "skill_definitions"):
                await conn.execute(
                    f"DELETE FROM {tbl} WHERE created_by IN "
                    f"(SELECT id FROM users WHERE {test_user_filter})"
                )
            await conn.execute(
                f"DELETE FROM user_groups WHERE user_id IN "
                f"(SELECT id FROM users WHERE {test_user_filter})"
            )
            await conn.execute(
                f"DELETE FROM api_keys WHERE user_id IN "
                f"(SELECT id FROM users WHERE {test_user_filter})"
            )
            # Users and orgs
            await conn.execute(f"DELETE FROM users WHERE {test_user_filter}")
            await conn.execute(f"DELETE FROM organizations WHERE {test_org_filter}")
        finally:
            await conn.close()

    try:
        asyncio.get_event_loop().run_until_complete(_cleanup())
    except Exception:
        # Best-effort cleanup — don't fail the test suite
        pass


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
        for tbl in (
            "agent_definitions",
            "skill_definitions",
        ):
            await conn.execute(
                f"DELETE FROM {tbl} WHERE created_by IN "
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
        # Delete test organizations
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


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
