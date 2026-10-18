"""Integration tests for user file storage quotas and provider selection.

Uses an in-memory :class:`FileStorageProvider` so quota behaviour is tested
without touching Azure or the filesystem. Requires the isolated test database
(``scripts/dev-test-db.sh``) — the ``db_pool`` fixture marks these integration.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
from httpx import ASGITransport

from lucent import settings as runtime_settings
from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.files import UserFileRepository
from lucent.db.pool import scoped_acquire
from lucent.storage.providers import (
    FileStorageProvider,
    FileStorageRegistry,
)
from lucent.storage.service import UserFileService


class InMemoryFileStorageProvider(FileStorageProvider):
    """Three trivial methods; the injection point the suite's fixtures use."""

    def __init__(self, name: str = "local"):
        self.name = name
        self.store: dict[str, bytes] = {}

    async def put(self, storage_key: str, content: bytes) -> None:
        self.store[storage_key] = bytes(content)

    async def get(self, storage_key: str) -> bytes:
        if storage_key not in self.store:
            raise FileNotFoundError(storage_key)
        return self.store[storage_key]

    async def delete(self, storage_key: str) -> None:
        self.store.pop(storage_key, None)


class RecordingFileStorageProvider(InMemoryFileStorageProvider):
    """In-memory provider that records every provider-side call."""

    def __init__(self, name: str = "local"):
        super().__init__(name)
        self.put_calls = 0
        self.delete_calls = 0

    async def put(self, storage_key: str, content: bytes) -> None:
        self.put_calls += 1
        await super().put(storage_key, content)

    async def delete(self, storage_key: str) -> None:
        self.delete_calls += 1
        await super().delete(storage_key)


class FailingDeleteFileStorageProvider(RecordingFileStorageProvider):
    """Provider whose compensating delete raises, to test error precedence."""

    async def delete(self, storage_key: str) -> None:
        self.delete_calls += 1
        raise RuntimeError("provider delete failed")


def _barrier_reserves(service: UserFileService, parties: int = 2) -> None:
    """Hold each quota reservation until ``parties`` uploads reach it.

    The race is decided when the first upload takes the per-org advisory lock
    for its reservation, so the barrier belongs at the reservation seam: both
    uploads are in flight before either can take the lock, forcing a genuine
    race on the measure-then-reserve pair rather than an accidental ordering of
    scheduler and pool acquisitions.
    """
    release = asyncio.Event()
    arrived = 0
    original = service.repository.reserve

    async def reserve(**kwargs):
        nonlocal arrived
        arrived += 1
        if arrived >= parties:
            release.set()
        else:
            await asyncio.wait_for(release.wait(), timeout=5)
        return await original(**kwargs)

    service.repository.reserve = reserve


@pytest.fixture(autouse=True)
def _reset_runtime_settings_cache():
    """The quota limits are cached per org in-process; keep tests isolated."""
    runtime_settings.clear_runtime_setting_cache()
    yield
    runtime_settings.clear_runtime_setting_cache()


def _set_limit(org_id, key: str, value: int) -> None:
    runtime_settings.set_runtime_setting_cache(str(org_id), key, value)


def _service(db_pool, provider: InMemoryFileStorageProvider | None = None) -> UserFileService:
    provider = provider or InMemoryFileStorageProvider("local")
    return UserFileService(db_pool, FileStorageRegistry([provider]))


async def _create(service: UserFileService, *, org, user, filename: str, content: bytes):
    return await service.create(
        org_id=str(org),
        user_id=str(user),
        created_by=str(user),
        filename=filename,
        content=content,
    )


# --------------------------------------------------------------------------- #
# Settings wiring (unit — no database)
# --------------------------------------------------------------------------- #

_LIMIT_ENV_VARS = (
    "LUCENT_MAX_USER_FILE_BYTES",
    "LUCENT_MAX_USER_FILE_BYTES_PER_USER",
    "LUCENT_MAX_USER_FILE_BYTES_PER_ORG",
)


def test_file_limit_defaults(monkeypatch):
    from lucent.settings import max_bytes_per_file, max_bytes_per_org, max_bytes_per_user

    for var in _LIMIT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    runtime_settings.clear_runtime_setting_cache()

    assert max_bytes_per_file() == 10 * 1024 * 1024
    assert max_bytes_per_user() == 1024 * 1024 * 1024
    assert max_bytes_per_org() == 10 * 1024 * 1024 * 1024


def test_file_limits_honour_env_fallback(monkeypatch):
    """The legacy LUCENT_MAX_USER_FILE_BYTES still works, plus the two new vars."""
    from lucent.settings import max_bytes_per_file, max_bytes_per_org, max_bytes_per_user

    monkeypatch.setenv("LUCENT_MAX_USER_FILE_BYTES", "12345")
    monkeypatch.setenv("LUCENT_MAX_USER_FILE_BYTES_PER_USER", "111")
    monkeypatch.setenv("LUCENT_MAX_USER_FILE_BYTES_PER_ORG", "222")
    runtime_settings.clear_runtime_setting_cache()

    assert max_bytes_per_file() == 12345
    assert max_bytes_per_user() == 111
    assert max_bytes_per_org() == 222


def test_file_limits_db_cache_overrides_env(monkeypatch):
    from lucent.settings import max_bytes_per_file

    monkeypatch.setenv("LUCENT_MAX_USER_FILE_BYTES", "12345")
    runtime_settings.clear_runtime_setting_cache()
    runtime_settings.set_runtime_setting_cache(
        "00000000-0000-0000-0000-000000000001", "files.max_bytes_per_file", 7
    )

    assert max_bytes_per_file(organization_id="00000000-0000-0000-0000-000000000001") == 7


# --------------------------------------------------------------------------- #
# Per-file, per-user, per-org rejection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_per_file_limit_rejects_oversized_upload(
    db_pool, test_user, test_organization
):
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_file", 10)
    service = _service(db_pool)

    with pytest.raises(ValueError, match="per-file limit"):
        await _create(service, org=org, user=user, filename="big.txt", content=b"x" * 11)

    # Nothing was persisted.
    listed = await service.repository.list_owned(org, user)
    assert listed["total_count"] == 0


@pytest.mark.asyncio
async def test_user_under_the_limit_succeeds(db_pool, test_user, test_organization):
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 100)
    _set_limit(org, "files.max_bytes_per_org", 10_000)

    created = await _create(
        _service(db_pool), org=org, user=user, filename="ok.txt", content=b"x" * 60
    )

    assert created["size_bytes"] == 60
    assert created["provider"] == "local"


@pytest.mark.asyncio
async def test_per_user_limit_rejects_second_upload(
    db_pool, test_user, test_organization
):
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 100)
    service = _service(db_pool)

    await _create(service, org=org, user=user, filename="a.txt", content=b"x" * 60)
    with pytest.raises(ValueError, match="per-user limit"):
        await _create(service, org=org, user=user, filename="b.txt", content=b"x" * 60)

    listed = await service.repository.list_owned(org, user)
    assert listed["total_count"] == 1


@pytest.mark.asyncio
async def test_per_org_limit_rejects_across_users(
    db_pool, test_user, test_organization, clean_test_data
):
    org = str(test_organization["id"])
    _set_limit(org, "files.max_bytes_per_org", 100)
    other = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}quota-other",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}quota-other@test.com",
        display_name="Other quota user",
    )
    service = _service(db_pool)

    await _create(
        service, org=org, user=str(test_user["id"]), filename="a.txt", content=b"x" * 60
    )
    with pytest.raises(ValueError, match="organization limit"):
        await _create(
            service, org=org, user=str(other["id"]), filename="b.txt", content=b"x" * 60
        )

    # The first user's file survives; the second user's write left nothing.
    assert (await service.repository.list_owned(org, str(test_user["id"])))["total_count"] == 1
    assert (await service.repository.list_owned(org, str(other["id"])))["total_count"] == 0


# --------------------------------------------------------------------------- #
# Revisions add bytes and are enforced
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_revision_exceeding_limit_is_rejected_and_bytes_freed(
    db_pool, test_user, test_organization
):
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 100)
    provider = InMemoryFileStorageProvider("local")
    service = _service(db_pool, provider)

    created = await _create(
        service, org=org, user=user, filename="doc.md", content=b"x" * 60
    )
    keys_before = set(provider.store)

    # Second 60-byte revision would make the user's total 120 > 100.
    with pytest.raises(ValueError, match="per-user limit"):
        await service.update(
            file_id=str(created["id"]),
            org_id=org,
            user_id=user,
            edited_by=user,
            content=b"y" * 60,
        )

    # The rejected revision's bytes were compensating-deleted, and no revision
    # row was written.
    assert set(provider.store) == keys_before
    revisions = await service.repository.list_revisions_owned(str(created["id"]), org, user)
    assert [r["revision_number"] for r in revisions] == [1]


@pytest.mark.asyncio
async def test_revision_within_limit_succeeds(db_pool, test_user, test_organization):
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 1000)
    service = _service(db_pool)

    created = await _create(service, org=org, user=user, filename="doc.md", content=b"x" * 60)
    updated = await service.update(
        file_id=str(created["id"]),
        org_id=org,
        user_id=user,
        edited_by=user,
        content=b"y" * 40,
    )

    assert updated["current_revision"] == 2


# --------------------------------------------------------------------------- #
# Concurrency: two uploads cannot both slip past the check
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrent_uploads_cannot_both_pass(db_pool, test_user, test_organization):
    org, user = str(test_organization["id"]), str(test_user["id"])
    # Room for exactly one 60-byte file.
    _set_limit(org, "files.max_bytes_per_user", 100)
    _set_limit(org, "files.max_bytes_per_org", 10_000)
    # The barrier holds both uploads in flight before either reaches the
    # measure-then-reserve step, so the advisory lock is what makes it safe.
    provider = RecordingFileStorageProvider("local")
    service = _service(db_pool, provider)
    _barrier_reserves(service)

    async def upload(filename: str):
        return await _create(
            service, org=org, user=user, filename=filename, content=b"x" * 60
        )

    results = await asyncio.gather(
        upload("one.txt"), upload("two.txt"), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if not isinstance(r, Exception)]

    assert len(successes) == 1, results
    assert len(errors) == 1, results
    assert "per-user limit" in str(errors[0])

    # Exactly one file, and exactly one provider object, survived.
    listed = await service.repository.list_owned(org, user)
    assert listed["total_count"] == 1
    assert len(provider.store) == 1


@pytest.mark.asyncio
async def test_concurrent_uploads_by_different_users_cannot_both_pass_org_cap(
    db_pool, test_user, test_organization, clean_test_data
):
    """The per-org cap holds when two *different* users in one org race.

    This is the case a user-granularity lock could not cover: the two uploads
    take different owners, so only an org-level lock in the measure-then-insert
    window stops them from both reading the pre-insert org total and jointly
    overshooting the org ceiling.
    """
    org = str(test_organization["id"])
    # Room for exactly one 60-byte file org-wide, but each user individually
    # has ample headroom — only the per-org cap can reject here.
    _set_limit(org, "files.max_bytes_per_org", 100)
    _set_limit(org, "files.max_bytes_per_user", 10_000)
    other = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}quota-org-race-other",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}quota-org-race-other@test.com",
        display_name="Other org-race user",
    )
    # The barrier holds both uploads in flight before either reaches the
    # measure-then-reserve step, forcing a genuine race on the org total.
    provider = RecordingFileStorageProvider("local")
    service = _service(db_pool, provider)
    _barrier_reserves(service)

    async def upload(user_id: str, filename: str):
        return await _create(
            service, org=org, user=user_id, filename=filename, content=b"x" * 60
        )

    results = await asyncio.gather(
        upload(str(test_user["id"]), "one.txt"),
        upload(str(other["id"]), "two.txt"),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if not isinstance(r, Exception)]

    assert len(successes) == 1, results
    assert len(errors) == 1, results
    assert "organization limit" in str(errors[0])

    # Exactly one file survived across both owners, and the org's accounted
    # bytes never exceeded the ceiling.
    org_total = 0
    for user_id in (str(test_user["id"]), str(other["id"])):
        org_total += (await service.repository.list_owned(org, user_id))["total_count"]
    assert org_total == 1
    assert len(provider.store) == 1


@pytest.mark.asyncio
async def test_quota_lock_serializes_within_an_org_and_not_across_orgs(
    db_pool, test_user, test_organization, clean_test_data
):
    """Observe the advisory lock's granularity directly at the enforcement seam.

    With org A's quota lock held, a *second* user of org A parks on it while a
    user of org B proceeds immediately — the two behaviours the org-only key is
    supposed to produce. Driving ``_enforce_quota`` on explicit scoped
    connections is what makes the lock observable: through the service, the
    critical section is too short for a same-org vs cross-org difference to
    surface reliably.
    """
    org_a = str(test_organization["id"])
    user_a = str(test_user["id"])
    users = UserRepository(db_pool)
    sibling_user = await users.create(
        external_id=f"{clean_test_data}quota-lock-sibling",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}quota-lock-sibling@test.com",
        display_name="Org A sibling user",
    )
    org_b = await OrganizationRepository(db_pool).create(name=f"{clean_test_data}orgB")
    org_b_id = str(org_b["id"])
    user_b = await users.create(
        external_id=f"{clean_test_data}quota-lock-orgB",
        provider="local",
        organization_id=org_b["id"],
        email=f"{clean_test_data}quota-lock-orgB@test.com",
        display_name="Org B user",
    )
    limit = 10_000

    async def enforce(org_id: str, user_id: str) -> None:
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            async with conn.transaction():
                await UserFileRepository._enforce_quota(
                    conn,
                    org_id=org_id,
                    user_id=user_id,
                    new_bytes=0,
                    max_bytes_per_user=None,
                    max_bytes_per_org=limit,
                )

    async with scoped_acquire(organization_id=org_a, user_id=user_a) as holder:
        async with holder.transaction():
            # Hold org A's lock for the rest of this block.
            await UserFileRepository._enforce_quota(
                holder,
                org_id=org_a,
                user_id=user_a,
                new_bytes=0,
                max_bytes_per_user=None,
                max_bytes_per_org=limit,
            )

            reached_lock = asyncio.Event()

            async def same_org_enforce() -> None:
                async with scoped_acquire(
                    organization_id=org_a, user_id=str(sibling_user["id"])
                ) as conn:
                    async with conn.transaction():
                        reached_lock.set()
                        await UserFileRepository._enforce_quota(
                            conn,
                            org_id=org_a,
                            user_id=str(sibling_user["id"]),
                            new_bytes=0,
                            max_bytes_per_user=None,
                            max_bytes_per_org=limit,
                        )

            sibling = asyncio.create_task(same_org_enforce())
            # It reached the enforcement call and is now parked on the lock.
            await asyncio.wait_for(reached_lock.wait(), timeout=5)
            await asyncio.sleep(0.1)
            assert not sibling.done(), "same-org upload was not serialized"

            # A different org takes a different key, so it never waits on A.
            await asyncio.wait_for(enforce(org_b_id, str(user_b["id"])), timeout=5)

        # Leaving the block releases org A's lock and the parked write proceeds.
    await asyncio.wait_for(sibling, timeout=5)
    assert sibling.exception() is None


# --------------------------------------------------------------------------- #
# Regression: switching the active provider does not break legacy local rows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_active_provider_switch_keeps_local_rows_readable(
    db_pool, test_user, test_organization
):
    org, user = str(test_organization["id"]), str(test_user["id"])
    local = InMemoryFileStorageProvider("local")
    azure = InMemoryFileStorageProvider("azure")

    # Write a file while the active provider is the legacy default.
    local_service = UserFileService(
        db_pool, FileStorageRegistry([local, azure], active="local")
    )
    legacy = await _create(
        local_service, org=org, user=user, filename="legacy.md", content=b"# legacy\n"
    )
    assert legacy["provider"] == "local"
    assert "storage_key" not in legacy
    assert b"# legacy\n" in local.store.values()

    # Switch the active provider for new writes to azure.
    switched_service = UserFileService(
        db_pool, FileStorageRegistry([local, azure], active="azure")
    )

    # The pre-existing local row still reads — resolution follows the row's own
    # provider, not the active setting.
    read = await switched_service.read_owned(str(legacy["id"]), org, user)
    assert read is not None
    assert read[1] == b"# legacy\n"

    # New writes land in azure, and the row records that fact.
    fresh = await _create(
        switched_service, org=org, user=user, filename="new.md", content=b"# new\n"
    )
    assert fresh["provider"] == "azure"
    assert "storage_key" not in fresh
    assert b"# new\n" in azure.store.values()
    assert b"# new\n" not in local.store.values()


# --------------------------------------------------------------------------- #
# Regression: quota is reserved before storage I/O; deleted bytes stay counted
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_over_quota_upload_never_reaches_the_provider(
    db_pool, test_user, test_organization
):
    """An over-quota write is rejected before ``provider.put`` is called."""
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 100)
    provider = RecordingFileStorageProvider("local")
    service = _service(db_pool, provider)

    created = await _create(
        service, org=org, user=user, filename="a.txt", content=b"x" * 60
    )
    assert provider.put_calls == 1

    with pytest.raises(ValueError, match="per-user limit"):
        await _create(service, org=org, user=user, filename="b.txt", content=b"x" * 60)

    # The rejected payload was never transmitted or stored.
    assert provider.put_calls == 1
    assert len(provider.store) == 1
    # Metadata responses never expose provider-internal keys.
    assert "storage_key" not in created
    listed = await service.repository.list_owned(org, user)
    assert "storage_key" not in listed["items"][0]


@pytest.mark.asyncio
async def test_deleted_bytes_count_until_the_retention_window_expires(
    db_pool, test_user, test_organization
):
    """Soft-deleted revisions hold quota until the provider expires them."""
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 100)
    service = _service(db_pool)

    created = await _create(
        service, org=org, user=user, filename="gone.txt", content=b"x" * 60
    )
    assert await service.delete_owned(str(created["id"]), org, user) is True

    # The row is gone, but the provider still retains (and bills for) the
    # blob for the soft-delete window, so the 60 bytes still count.
    with pytest.raises(ValueError, match="per-user limit"):
        await _create(service, org=org, user=user, filename="next.txt", content=b"x" * 60)

    # Once the provider's retention window has passed, the bytes stop
    # counting and the same upload fits.
    await db_pool.execute(
        "UPDATE user_files SET deleted_at = NOW() - interval '8 days' "
        "WHERE id = $1::uuid",
        created["id"],
    )
    accepted = await _create(
        service, org=org, user=user, filename="next.txt", content=b"x" * 60
    )
    assert accepted["size_bytes"] == 60


@pytest.mark.asyncio
async def test_delete_with_unavailable_provider_fails_before_the_row_is_deleted(
    db_pool, test_user, test_organization
):
    """A missing provider leaves the row live so a retry can free the blobs."""
    org, user = str(test_organization["id"]), str(test_user["id"])
    local = InMemoryFileStorageProvider("local")
    azure = InMemoryFileStorageProvider("azure")
    service = UserFileService(
        db_pool, FileStorageRegistry([local, azure], active="local")
    )
    created = await _create(
        service, org=org, user=user, filename="stranded.txt", content=b"# bytes\n"
    )
    assert len(local.store) == 1

    # Simulate the row's provider no longer being configured.
    missing_provider_service = UserFileService(
        db_pool, FileStorageRegistry([azure], active="azure")
    )
    with pytest.raises(ValueError, match="Unknown file storage provider"):
        await missing_provider_service.delete_owned(str(created["id"]), org, user)

    # The row is still live and its blob still exists: nothing was half done.
    assert (
        await missing_provider_service.repository.get_owned(
            str(created["id"]), org, user
        )
        is not None
    )
    assert len(local.store) == 1


@pytest.mark.asyncio
async def test_compensating_delete_failure_surfaces_the_original_error(
    db_pool, test_user, test_organization
):
    """Cleanup failures never replace the error that failed the write."""
    org, user = str(test_organization["id"]), str(test_user["id"])
    _set_limit(org, "files.max_bytes_per_user", 100)
    provider = FailingDeleteFileStorageProvider("local")
    service = _service(db_pool, provider)
    original_create = service.repository.create

    async def failing_create(**kwargs):
        raise ValueError("original write failure")

    service.repository.create = failing_create
    try:
        with pytest.raises(ValueError, match="original write failure"):
            await _create(
                service, org=org, user=user, filename="fail.txt", content=b"x" * 60
            )
    finally:
        service.repository.create = original_create

    # The compensating delete was attempted and failed...
    assert provider.delete_calls == 1
    # ...but the reservation was released: a 95-byte write fits under the
    # 100-byte cap only if the failed write's 60-byte reservation is gone.
    accepted = await _create(
        service, org=org, user=user, filename="after.txt", content=b"y" * 95
    )
    assert accepted["size_bytes"] == 95


@pytest.mark.asyncio
async def test_revision_after_provider_switch_lands_in_the_active_provider(
    db_pool, test_user, test_organization
):
    """Revisions are new writes and follow the active provider, not the row."""
    org, user = str(test_organization["id"]), str(test_user["id"])
    local = InMemoryFileStorageProvider("local")
    azure = InMemoryFileStorageProvider("azure")

    legacy_service = UserFileService(
        db_pool, FileStorageRegistry([local, azure], active="local")
    )
    created = await _create(
        legacy_service, org=org, user=user, filename="legacy.md", content=b"# one\n"
    )
    assert b"# one\n" in local.store.values()

    switched_service = UserFileService(
        db_pool, FileStorageRegistry([local, azure], active="azure")
    )
    updated = await switched_service.update(
        file_id=str(created["id"]),
        org_id=org,
        user_id=user,
        edited_by=user,
        content=b"# two\n",
    )

    assert updated["provider"] == "azure"
    assert b"# two\n" in azure.store.values()
    assert b"# two\n" not in local.store.values()
    revisions = await switched_service.repository.list_revisions_owned(
        str(created["id"]), org, user
    )
    revision_two = next(r for r in revisions if r["revision_number"] == 2)
    assert revision_two["provider"] == "azure"
    assert "storage_key" not in revision_two
    # The revision still reads back through its own recorded provider.
    read = await switched_service.read_revision_owned(
        str(created["id"]), 2, org, user
    )
    assert read is not None and read[1] == b"# two\n"


@pytest.mark.asyncio
async def test_storage_key_not_scoped_to_the_owner_is_rejected(
    db_pool, test_user, test_organization
):
    """A key naming another tenant fails closed before any provider call."""
    org, user = str(test_organization["id"]), str(test_user["id"])
    provider = RecordingFileStorageProvider("local")
    service = _service(db_pool, provider)

    # No application path writes caller-supplied keys today; this row models a
    # legacy/buggy key that points outside the owner's prefix.
    mismatched_key = "00000000-0000-0000-0000-000000000099/other-user/other-file"
    content = b"other tenant's bytes"
    created = await service.repository.create(
        org_id=org,
        user_id=user,
        created_by=user,
        provider="local",
        storage_key=mismatched_key,
        filename="foreign.txt",
        display_name="foreign.txt",
        mime_type="text/plain",
        size_bytes=len(content),
        checksum_sha256=hashlib.sha256(content).hexdigest(),
        metadata={},
    )
    await provider.put(mismatched_key, content)

    with pytest.raises(ValueError, match="does not belong to this file owner"):
        await service.read_owned(str(created["id"]), org, user)

    with pytest.raises(ValueError, match="does not belong to this file owner"):
        await service.delete_owned(str(created["id"]), org, user)

    # Fail closed means the row was not deleted.
    assert (
        await service.repository.get_owned(str(created["id"]), org, user) is not None
    )


# --------------------------------------------------------------------------- #
# The routers surface an over-quota rejection as a 4xx
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_api_surfaces_quota_rejection_as_422(
    db_pool, test_user, test_organization, tmp_path, monkeypatch
):
    monkeypatch.setenv("LUCENT_FILE_STORAGE_PATH", str(tmp_path))
    org = str(test_organization["id"])
    _set_limit(org, "files.max_bytes_per_file", 5)

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=test_user["id"],
        organization_id=test_user["organization_id"],
        role="member",
        email=test_user.get("email"),
        display_name=test_user.get("display_name"),
        auth_method="api_key",
    )
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/files",
                json={"filename": "big.txt", "content": "x" * 10},  # 10 bytes, limit 5
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "per-file limit" in response.json()["detail"]
