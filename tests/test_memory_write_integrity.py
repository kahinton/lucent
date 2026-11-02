"""Memory write/read integrity regression tests.

Covers the platform-fix signatures from the studio-notebook 0503c568
incident (Phase 1 diagnosis, dedupe key ``notebook-0503c568-diagnosis``):

1. a written/updated memory stores and returns exactly the caller's content
2. an unguarded content update cannot silently discard prior content
3. a read returns exactly the stored bytes
"""

import pytest

from lucent.db import ContentShrinkError, MemoryRepository


def _long_content(prefix: str) -> str:
    return f"{prefix} header ✓\n" + ("- durable line\n" * 45) + "tail ✓"


async def _create(repo, test_user, prefix, content):
    return await repo.create(
        username=f"{prefix}user",
        type="experience",
        content=content,
        tags=["integrity"],
        user_id=test_user["id"],
        organization_id=test_user["organization_id"],
    )


class TestMemoryWriteReadByteFaithfulness:
    """Stored bytes and returned bytes must equal the caller's input exactly."""

    async def test_update_stores_and_returns_exact_content(
        self, db_pool, test_user, clean_test_data
    ):
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        original = _long_content(prefix)
        memory = await _create(repo, test_user, prefix, original)

        new_content = original + '\nappended: unicode ✓ "quoted" <tag>'
        result = await repo.update(
            memory_id=memory["id"],
            content=new_content,
            organization_id=test_user["organization_id"],
        )

        assert result is not None
        assert result["content"] == new_content
        stored = await repo.get(
            memory["id"], organization_id=test_user["organization_id"]
        )
        assert stored["content"] == new_content
        assert stored["content"].encode("utf-8") == new_content.encode("utf-8")

    async def test_read_returns_stored_bytes(
        self, db_pool, test_user, clean_test_data
    ):
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        content = _long_content(prefix) + "\nread-path tail ✓"
        memory = await _create(repo, test_user, prefix, content)

        fetched = await repo.get(
            memory["id"], organization_id=test_user["organization_id"]
        )
        async with db_pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT content FROM memories WHERE id = $1", str(memory["id"])
            )

        assert fetched["content"] == stored == content


class TestConcurrentUpdateCannotDropContent:
    """An unguarded stale rewrite must fail instead of dropping content."""

    async def test_unguarded_shrink_is_rejected_and_content_intact(
        self, db_pool, test_user, clean_test_data
    ):
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        base = _long_content(prefix)
        memory = await _create(repo, test_user, prefix, base)

        appended = base + "\nwriter A appended"
        result = await repo.update(
            memory_id=memory["id"],
            content=appended,
            organization_id=test_user["organization_id"],
        )
        assert result["content"] == appended

        # A stale writer holding only a short excerpt tries to replace the
        # memory. Without expected_version this must fail loudly.
        with pytest.raises(ContentShrinkError):
            await repo.update(
                memory_id=memory["id"],
                content="tiny stale excerpt",
                organization_id=test_user["organization_id"],
            )

        stored = await repo.get(
            memory["id"], organization_id=test_user["organization_id"]
        )
        assert stored["content"] == appended
        assert "writer A appended" in stored["content"]

    async def test_unguarded_append_still_succeeds(
        self, db_pool, test_user, clean_test_data
    ):
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        base = _long_content(prefix)
        memory = await _create(repo, test_user, prefix, base)

        appended = base + "\nwriter B appended"
        result = await repo.update(
            memory_id=memory["id"],
            content=appended,
            organization_id=test_user["organization_id"],
        )

        assert result["content"] == appended
        assert result["content"].startswith(base)

    async def test_explicit_expected_version_allows_intentional_rewrite(
        self, db_pool, test_user, clean_test_data
    ):
        repo = MemoryRepository(db_pool)
        prefix = clean_test_data
        base = _long_content(prefix)
        memory = await _create(repo, test_user, prefix, base)

        # An intentional, version-checked overwrite is still permitted.
        result = await repo.update(
            memory_id=memory["id"],
            content="deliberate replacement",
            expected_version=memory["version"],
            organization_id=test_user["organization_id"],
        )

        assert result["content"] == "deliberate replacement"
