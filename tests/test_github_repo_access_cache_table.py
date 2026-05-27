"""Integration tests for github_repo_access_cache table behavior."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio

from lucent.db import OrganizationRepository, UserRepository


@pytest_asyncio.fixture
async def gh_cache_prefix(db_pool):
    test_id = str(uuid4())[:8]
    prefix = f"test_ghcache_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM github_repo_access_cache WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def gh_cache_user(db_pool, gh_cache_prefix):
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{gh_cache_prefix}org")
    user_repo = UserRepository(db_pool)
    return await user_repo.create(
        external_id=f"{gh_cache_prefix}user",
        provider="local",
        organization_id=org["id"],
        email=f"{gh_cache_prefix}user@test.com",
        display_name=f"{gh_cache_prefix}User",
    )


class TestGitHubRepoAccessCacheTable:
    async def test_insert_row(self, db_pool, gh_cache_user):
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=15)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_repo_access_cache
                    (user_id, repo_full_name, has_access, checked_at, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                gh_cache_user["id"],
                "owner/repo",
                True,
                now,
                expires,
            )
            row = await conn.fetchrow(
                """
                SELECT has_access, checked_at, expires_at
                FROM github_repo_access_cache
                WHERE user_id = $1 AND repo_full_name = $2
                """,
                gh_cache_user["id"],
                "owner/repo",
            )

        assert row is not None
        assert row["has_access"] is True
        assert row["expires_at"] > row["checked_at"]

    async def test_upsert_updates_existing_row(self, db_pool, gh_cache_user):
        now = datetime.now(UTC)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_repo_access_cache
                    (user_id, repo_full_name, has_access, checked_at, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, repo_full_name)
                DO UPDATE SET
                    has_access = EXCLUDED.has_access,
                    checked_at = EXCLUDED.checked_at,
                    expires_at = EXCLUDED.expires_at
                """,
                gh_cache_user["id"],
                "owner/repo",
                True,
                now - timedelta(minutes=5),
                now + timedelta(minutes=10),
            )
            await conn.execute(
                """
                INSERT INTO github_repo_access_cache
                    (user_id, repo_full_name, has_access, checked_at, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, repo_full_name)
                DO UPDATE SET
                    has_access = EXCLUDED.has_access,
                    checked_at = EXCLUDED.checked_at,
                    expires_at = EXCLUDED.expires_at
                """,
                gh_cache_user["id"],
                "owner/repo",
                False,
                now,
                now + timedelta(minutes=5),
            )
            row = await conn.fetchrow(
                """
                SELECT has_access, checked_at
                FROM github_repo_access_cache
                WHERE user_id = $1 AND repo_full_name = $2
                """,
                gh_cache_user["id"],
                "owner/repo",
            )

        assert row is not None
        assert row["has_access"] is False
        assert row["checked_at"] >= now - timedelta(seconds=1)

    async def test_expiry_selection_logic(self, db_pool, gh_cache_user):
        now = datetime.now(UTC)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_repo_access_cache
                    (user_id, repo_full_name, has_access, checked_at, expires_at)
                VALUES
                    ($1, 'owner/fresh', TRUE, $2, $3),
                    ($1, 'owner/stale', FALSE, $2, $4)
                """,
                gh_cache_user["id"],
                now,
                now + timedelta(minutes=5),
                now - timedelta(minutes=1),
            )
            fresh = await conn.fetchval(
                """
                SELECT COUNT(*) FROM github_repo_access_cache
                WHERE user_id = $1 AND expires_at > $2
                """,
                gh_cache_user["id"],
                now,
            )
            stale = await conn.fetchval(
                """
                SELECT COUNT(*) FROM github_repo_access_cache
                WHERE user_id = $1 AND expires_at <= $2
                """,
                gh_cache_user["id"],
                now,
            )

        assert fresh == 1
        assert stale == 1
