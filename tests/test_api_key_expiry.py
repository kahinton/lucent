"""Tests for API key expiry hygiene.

Two regressions are covered, both found on a live instance that had 22,714 key
rows of which only 13 could actually authenticate:

1. **Counts lied.** Anything filtered on ``is_active AND revoked_at IS NULL``
   reported expired keys as active. They had never been reaped, because expiry
   was only enforced when a key was *used*.
2. **Nothing reaped them.** Scoped task keys are minted with a 60-minute TTL, so
   that backlog was almost entirely dead one-hour credentials.

The behaviour that matters is that an expired key is (a) not counted and (b) not
mistaken for a live one, while a key with no expiry — the long-lived personal
ones — is never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio

from lucent.db import ApiKeyRepository, OrganizationRepository, UserRepository
from lucent.db.api_key import _affected


def test_affected_parses_asyncpg_tags():
    assert _affected("DELETE 42") == 42
    assert _affected("UPDATE 1") == 1
    assert _affected("DELETE 0") == 0
    assert _affected(None) == 0
    assert _affected("") == 0
    assert _affected("garbage") == 0


@pytest_asyncio.fixture
async def key_org(db_pool, clean_test_data):
    prefix = clean_test_data
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}keyorg")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}keyowner",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}keyowner@test.com",
        role="owner",
    )
    return org, user, prefix


async def _mint(repo, user, org, *, name, expires_in_days=None):
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        if expires_in_days is not None
        else None
    )
    record, raw = await repo.create(
        user_id=user["id"],
        organization_id=org["id"],
        name=name,
        expires_at=expires_at,
    )
    return record, raw


class TestPurgeExpired:
    async def test_deletes_only_expired_keys(self, db_pool, key_org, clean_test_data):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)

        expired, _ = await _mint(repo, user, org, name=f"{prefix}expired", expires_in_days=-40)
        live, live_raw = await _mint(repo, user, org, name=f"{prefix}live", expires_in_days=7)
        forever, forever_raw = await _mint(repo, user, org, name=f"{prefix}forever")

        result = await repo.purge_expired(older_than_days=30)
        assert result["deleted"] >= 1

        assert await repo.get_by_id(expired["id"], user["id"], org["id"]) is None
        # Long-lived and still-valid keys must survive untouched.
        assert await repo.get_by_id(live["id"], user["id"], org["id"]) is not None
        assert await repo.get_by_id(forever["id"], user["id"], org["id"]) is not None
        assert await repo.verify(live_raw) is not None
        assert await repo.verify(forever_raw) is not None

    async def test_grace_window_protects_recently_expired(
        self, db_pool, key_org, clean_test_data
    ):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)

        recent, _ = await _mint(repo, user, org, name=f"{prefix}recent", expires_in_days=-1)
        old, _ = await _mint(repo, user, org, name=f"{prefix}old", expires_in_days=-45)

        await repo.purge_expired(older_than_days=30)

        # A lapse within the grace window stays visible for debugging.
        assert await repo.get_by_id(recent["id"], user["id"], org["id"]) is not None
        assert await repo.get_by_id(old["id"], user["id"], org["id"]) is None

    async def test_dry_run_deletes_nothing(self, db_pool, key_org, clean_test_data):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)
        expired, _ = await _mint(repo, user, org, name=f"{prefix}dry", expires_in_days=-40)

        result = await repo.purge_expired(older_than_days=0, dry_run=True)
        assert result["deleted"] == 0
        assert result["candidates"] >= 1
        assert await repo.get_by_id(expired["id"], user["id"], org["id"]) is not None

    async def test_org_scoped_purge_leaves_other_orgs_alone(
        self, db_pool, key_org, clean_test_data
    ):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)

        other_org = await OrganizationRepository(db_pool).create(name=f"{prefix}other")
        other_user = await UserRepository(db_pool).create(
            external_id=f"{prefix}otherowner",
            provider="local",
            organization_id=other_org["id"],
            email=f"{prefix}other@test.com",
            role="owner",
        )

        mine, _ = await _mint(repo, user, org, name=f"{prefix}mine", expires_in_days=-40)
        theirs, _ = await _mint(
            repo, other_user, other_org, name=f"{prefix}theirs", expires_in_days=-40
        )

        await repo.purge_expired(older_than_days=0, organization_id=org["id"])

        assert await repo.get_by_id(mine["id"], user["id"], org["id"]) is None
        assert (
            await repo.get_by_id(theirs["id"], other_user["id"], other_org["id"])
            is not None
        )

    async def test_batches_large_deletes(self, db_pool, key_org, clean_test_data):
        """Batching exists because a single statement hit the command timeout.

        ``api_keys`` has an ``AFTER DELETE`` trigger per row, so deleting tens of
        thousands in one statement runs long enough to be killed. Exercising the
        loop with a tiny batch size keeps that path covered without a slow test.
        """
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)

        for i in range(5):
            await _mint(repo, user, org, name=f"{prefix}batch{i}", expires_in_days=-40)

        result = await repo.purge_expired(older_than_days=0, batch_size=2)
        assert result["deleted"] >= 5


class TestCountsReflectUsableKeys:
    async def test_stats_exclude_expired_keys(self, db_pool, key_org, clean_test_data):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)

        await _mint(repo, user, org, name=f"{prefix}dead", expires_in_days=-1)
        await _mint(repo, user, org, name=f"{prefix}alive", expires_in_days=7)
        await _mint(repo, user, org, name=f"{prefix}forever")

        stats = await OrganizationRepository(db_pool).get_stats(org["id"])
        # Two usable keys; the expired one must not inflate the number the
        # operator console shows.
        assert stats["api_key_count"] == 2

    async def test_list_stats_exclude_expired_keys(
        self, db_pool, key_org, clean_test_data
    ):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)

        await _mint(repo, user, org, name=f"{prefix}dead", expires_in_days=-1)
        await _mint(repo, user, org, name=f"{prefix}alive", expires_in_days=7)

        result = await OrganizationRepository(db_pool).list(
            search=f"{prefix}keyorg", include_stats=True
        )
        row = next(o for o in result["organizations"] if o["id"] == org["id"])
        assert row["api_key_count"] == 1


class TestExpiredKeyCannotAuthenticate:
    async def test_verify_rejects_expired_key(self, db_pool, key_org, clean_test_data):
        """The enforcement that existed all along, pinned so it stays."""
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)
        _, raw = await _mint(repo, user, org, name=f"{prefix}stale", expires_in_days=-1)
        assert await repo.verify(raw) is None

    async def test_verify_accepts_unexpired_key(self, db_pool, key_org, clean_test_data):
        org, user, prefix = key_org
        repo = ApiKeyRepository(db_pool)
        _, raw = await _mint(repo, user, org, name=f"{prefix}fresh", expires_in_days=1)
        assert await repo.verify(raw) is not None
