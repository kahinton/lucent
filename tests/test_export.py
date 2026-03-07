"""Tests for the memory export functionality."""

import pytest

from lucent.db import MemoryRepository


class TestMemoryExport:
    """Tests for MemoryRepository.export()."""

    @pytest.mark.asyncio
    async def test_export_returns_all_user_memories(self, db_pool, test_user, clean_test_data):
        """Export should return all non-deleted memories for the user."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create multiple memories
        for i in range(3):
            await repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} Export test memory {i}",
                tags=["export-test"],
                importance=5,
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        result = await repo.export(
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        # Should have at least our 3 + the individual memory from test_user fixture
        export_memories = [m for m in result if m["username"] == f"{prefix}user"]
        assert len(export_memories) >= 3

    @pytest.mark.asyncio
    async def test_export_filter_by_type(self, db_pool, test_user, clean_test_data):
        """Export should filter by memory type."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Experience memory",
            tags=["export-test"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} Technical memory",
            tags=["export-test"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.export(
            type="technical",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        test_memories = [m for m in result if m["username"] == f"{prefix}user"]
        assert all(m["type"] == "technical" for m in test_memories)
        assert len(test_memories) >= 1

    @pytest.mark.asyncio
    async def test_export_filter_by_tags(self, db_pool, test_user, clean_test_data):
        """Export should filter by tags."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Tagged memory",
            tags=["special-tag", "export-test"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Other memory",
            tags=["other-tag"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.export(
            tags=["special-tag"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        test_memories = [m for m in result if m["username"] == f"{prefix}user"]
        assert all("special-tag" in m["tags"] for m in test_memories)

    @pytest.mark.asyncio
    async def test_export_includes_full_content(self, db_pool, test_user, clean_test_data):
        """Export should return full content, not truncated."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        long_content = f"{prefix} " + "A" * 2000
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=long_content,
            tags=["export-test"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.export(
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        long_memories = [m for m in result if len(m["content"]) > 1000]
        assert len(long_memories) >= 1
        assert long_memories[0]["content"] == long_content

    @pytest.mark.asyncio
    async def test_export_includes_metadata(self, db_pool, test_user, clean_test_data):
        """Export should include metadata in the response."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        meta = {"category": "testing", "language": "python"}
        await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} Memory with metadata",
            tags=["export-test"],
            metadata=meta,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.export(
            type="technical",
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        test_memories = [
            m for m in result
            if m["username"] == f"{prefix}user" and m["type"] == "technical"
        ]
        assert len(test_memories) >= 1
        assert test_memories[0]["metadata"]["category"] == "testing"

    @pytest.mark.asyncio
    async def test_export_excludes_deleted(self, db_pool, test_user, clean_test_data):
        """Export should not include soft-deleted memories."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memory = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Will be deleted",
            tags=["export-test", "deleted"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.delete(memory["id"])

        result = await repo.export(
            tags=["deleted"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        test_memories = [m for m in result if m["username"] == f"{prefix}user"]
        assert len(test_memories) == 0

    @pytest.mark.asyncio
    async def test_export_filter_by_importance(self, db_pool, test_user, clean_test_data):
        """Export should filter by importance range."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Low importance",
            tags=["importance-test"],
            importance=2,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} High importance",
            tags=["importance-test"],
            importance=9,
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        result = await repo.export(
            importance_min=7,
            tags=["importance-test"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        test_memories = [m for m in result if m["username"] == f"{prefix}user"]
        assert len(test_memories) == 1
        assert test_memories[0]["importance"] == 9

    @pytest.mark.asyncio
    async def test_export_ordered_by_created_at(self, db_pool, test_user, clean_test_data):
        """Export should return memories ordered by created_at ascending."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        for i in range(3):
            await repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} Ordered memory {i}",
                tags=["order-test"],
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        result = await repo.export(
            tags=["order-test"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        test_memories = [m for m in result if m["username"] == f"{prefix}user"]
        dates = [m["created_at"] for m in test_memories]
        assert dates == sorted(dates)


class TestMemoryImport:
    """Tests for MemoryRepository.import_memories()."""

    @pytest.mark.asyncio
    async def test_import_happy_path(self, db_pool, test_user, clean_test_data):
        """Import should create new memories and return correct counts."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memories = [
            {
                "type": "experience",
                "content": f"{prefix} Imported memory 1",
                "username": f"{prefix}user",
                "tags": ["import-test"],
                "importance": 7,
            },
            {
                "type": "technical",
                "content": f"{prefix} Imported memory 2",
                "username": f"{prefix}user",
                "tags": ["import-test"],
                "importance": 5,
                "metadata": {"language": "python"},
            },
        ]

        result = await repo.import_memories(
            memories=memories,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
            requesting_username=f"{prefix}user",
        )

        assert result["imported"] == 2
        assert result["skipped"] == 0
        assert result["errors"] == []
        assert result["total"] == 2

        # Verify they exist in the database
        exported = await repo.export(
            tags=["import-test"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        imported_memories = [m for m in exported if m["username"] == f"{prefix}user"]
        assert len(imported_memories) == 2

    @pytest.mark.asyncio
    async def test_import_skips_duplicates(self, db_pool, test_user, clean_test_data):
        """Import should skip memories that already exist (same content+type+username)."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create a memory first
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} Duplicate content",
            tags=["dedup-test"],
            user_id=test_user["id"],
            organization_id=test_user["organization_id"],
        )

        # Try to import the same content
        memories = [
            {
                "type": "experience",
                "content": f"{prefix} Duplicate content",
                "username": f"{prefix}user",
                "tags": ["dedup-test"],
            },
            {
                "type": "technical",
                "content": f"{prefix} New unique content",
                "username": f"{prefix}user",
                "tags": ["dedup-test"],
            },
        ]

        result = await repo.import_memories(
            memories=memories,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert result["imported"] == 1
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_import_malformed_data(self, db_pool, test_user, clean_test_data):
        """Import should report errors for invalid memory data."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memories = [
            {"type": "invalid_type", "content": f"{prefix} bad type"},
            {"type": "experience", "content": ""},  # empty content
            {"type": "experience"},  # missing content
            {
                "type": "experience",
                "content": f"{prefix} Valid memory",
                "username": f"{prefix}user",
            },
        ]

        result = await repo.import_memories(
            memories=memories,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
            requesting_username=f"{prefix}user",
        )

        assert result["imported"] == 1
        assert len(result["errors"]) == 3

    @pytest.mark.asyncio
    async def test_import_preserves_timestamps(self, db_pool, test_user, clean_test_data):
        """Import should preserve original created_at/updated_at if provided."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        from datetime import datetime

        original_created = datetime(2024, 1, 15, 10, 30, 0)
        original_updated = datetime(2024, 6, 20, 14, 0, 0)

        memories = [
            {
                "type": "experience",
                "content": f"{prefix} Timestamped memory",
                "username": f"{prefix}user",
                "tags": ["timestamp-test"],
                "created_at": original_created.isoformat(),
                "updated_at": original_updated.isoformat(),
            },
        ]

        result = await repo.import_memories(
            memories=memories,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        assert result["imported"] == 1

        exported = await repo.export(
            tags=["timestamp-test"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        test_memories = [m for m in exported if m["username"] == f"{prefix}user"]
        assert len(test_memories) == 1
        assert test_memories[0]["created_at"].year == 2024
        assert test_memories[0]["created_at"].month == 1

    @pytest.mark.asyncio
    async def test_import_round_trip(self, db_pool, test_user, clean_test_data):
        """Export then import should produce identical memories."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        # Create memories
        for i in range(3):
            await repo.create(
                username=f"{prefix}user",
                type="experience",
                content=f"{prefix} Round-trip memory {i}",
                tags=["roundtrip-test"],
                importance=5 + i,
                metadata={"index": i},
                user_id=test_user["id"],
                organization_id=test_user["organization_id"],
            )

        # Export
        exported = await repo.export(
            tags=["roundtrip-test"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        original_memories = [m for m in exported if m["username"] == f"{prefix}user"]
        assert len(original_memories) == 3

        # Delete originals
        for m in original_memories:
            await repo.delete(m["id"])

        # Import from export data — convert UUIDs/datetimes to strings as export would
        import_data = []
        for m in original_memories:
            import_data.append({
                "type": m["type"],
                "content": m["content"],
                "username": m["username"],
                "tags": m["tags"],
                "importance": m["importance"],
                "metadata": m["metadata"],
                "created_at": m["created_at"].isoformat(),
                "updated_at": m["updated_at"].isoformat(),
            })

        result = await repo.import_memories(
            memories=import_data,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        assert result["imported"] == 3
        assert result["skipped"] == 0

        # Verify the re-imported data matches
        re_exported = await repo.export(
            tags=["roundtrip-test"],
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )
        reimported = [m for m in re_exported if m["username"] == f"{prefix}user"]
        assert len(reimported) == 3
        for orig, reimp in zip(original_memories, reimported):
            assert orig["content"] == reimp["content"]
            assert orig["type"] == reimp["type"]
            assert orig["importance"] == reimp["importance"]
            assert orig["tags"] == reimp["tags"]

    @pytest.mark.asyncio
    async def test_import_duplicate_within_batch(self, db_pool, test_user, clean_test_data):
        """Import should deduplicate within the same import batch."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        memories = [
            {
                "type": "experience",
                "content": f"{prefix} Duplicate in batch",
                "username": f"{prefix}user",
            },
            {
                "type": "experience",
                "content": f"{prefix} Duplicate in batch",
                "username": f"{prefix}user",
            },
        ]

        result = await repo.import_memories(
            memories=memories,
            requesting_user_id=test_user["id"],
            requesting_org_id=test_user["organization_id"],
        )

        # First one imports, second is skipped because hash was added after first insert
        assert result["imported"] == 1
        assert result["skipped"] == 1
