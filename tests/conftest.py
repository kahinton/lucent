"""Test configuration and fixtures for Lucent."""

import os
from uuid import uuid4

import pytest_asyncio

# Set test database URL before importing any db modules
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


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
            "DELETE FROM memory_access_log WHERE memory_id IN (SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN (SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        # Delete API keys
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN (SELECT id FROM users WHERE external_id LIKE $1)",
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
