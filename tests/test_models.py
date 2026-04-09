"""Tests for Lucent memory models."""

from datetime import datetime
from uuid import UUID

import pytest

from lucent.models.memory import (
    CreateMemoryInput,
    ExperienceMetadata,
    GoalMetadata,
    GoalStatus,
    IndividualMetadata,
    Memory,
    MemoryType,
    ProceduralMetadata,
    ProceduralStep,
    SearchMemoriesInput,
    SearchResult,
    TechnicalMetadata,
    TruncatedMemory,
    UpdateMemoryInput,
)


class TestMemoryType:
    def test_memory_types_exist(self):
        assert MemoryType.EXPERIENCE.value == "experience"
        assert MemoryType.TECHNICAL.value == "technical"
        assert MemoryType.PROCEDURAL.value == "procedural"
        assert MemoryType.GOAL.value == "goal"
        assert MemoryType.INDIVIDUAL.value == "individual"


class TestGoalStatus:
    def test_goal_statuses_exist(self):
        assert GoalStatus.ACTIVE.value == "active"
        assert GoalStatus.PAUSED.value == "paused"
        assert GoalStatus.COMPLETED.value == "completed"
        assert GoalStatus.ABANDONED.value == "abandoned"


class TestCreateMemoryInput:
    def test_create_basic_memory(self):
        input_data = CreateMemoryInput(
            username="testuser",
            type=MemoryType.EXPERIENCE,
            content="This is a test memory",
        )
        assert input_data.username == "testuser"
        assert input_data.type == MemoryType.EXPERIENCE
        assert input_data.content == "This is a test memory"
        assert input_data.tags == []
        assert input_data.importance == 5

    def test_tags_normalized_to_lowercase(self):
        input_data = CreateMemoryInput(
            username="testuser",
            type=MemoryType.TECHNICAL,
            content="Test",
            tags=["Python", "ASYNC", "Database"],
        )
        assert set(input_data.tags) == {"python", "async", "database"}

    def test_tags_duplicates_removed(self):
        input_data = CreateMemoryInput(
            username="testuser",
            type=MemoryType.TECHNICAL,
            content="Test",
            tags=["python", "Python", "PYTHON"],
        )
        assert input_data.tags == ["python"]

    def test_importance_bounds(self):
        # Valid importance
        input_data = CreateMemoryInput(
            username="testuser",
            type=MemoryType.EXPERIENCE,
            content="Test",
            importance=10,
        )
        assert input_data.importance == 10

        # Invalid importance - too high
        with pytest.raises(ValueError):
            CreateMemoryInput(
                username="testuser",
                type=MemoryType.EXPERIENCE,
                content="Test",
                importance=11,
            )

        # Invalid importance - too low
        with pytest.raises(ValueError):
            CreateMemoryInput(
                username="testuser",
                type=MemoryType.EXPERIENCE,
                content="Test",
                importance=0,
            )

    def test_content_max_length(self):
        # Content at the limit should succeed
        input_data = CreateMemoryInput(
            username="testuser",
            type=MemoryType.EXPERIENCE,
            content="x" * 100_000,
        )
        assert len(input_data.content) == 100_000

        # Content exceeding limit should fail
        with pytest.raises(ValueError):
            CreateMemoryInput(
                username="testuser",
                type=MemoryType.EXPERIENCE,
                content="x" * 100_001,
            )


class TestUpdateMemoryInput:
    def test_partial_update(self):
        input_data = UpdateMemoryInput(content="Updated content")
        assert input_data.content == "Updated content"
        assert input_data.tags is None
        assert input_data.importance is None

    def test_tags_normalized_on_update(self):
        input_data = UpdateMemoryInput(tags=["NEW", "Tags"])
        assert set(input_data.tags) == {"new", "tags"}

    def test_content_max_length_on_update(self):
        # At limit should work
        input_data = UpdateMemoryInput(content="x" * 100_000)
        assert len(input_data.content) == 100_000

        # Exceeding limit should fail
        with pytest.raises(ValueError):
            UpdateMemoryInput(content="x" * 100_001)


class TestSearchMemoriesInput:
    def test_default_pagination(self):
        search = SearchMemoriesInput()
        assert search.offset == 0
        assert search.limit == 5

    def test_with_filters(self):
        search = SearchMemoriesInput(
            query="test query",
            type=MemoryType.TECHNICAL,
            tags=["python"],
            importance_min=5,
            importance_max=10,
        )
        assert search.query == "test query"
        assert search.type == MemoryType.TECHNICAL
        assert search.tags == ["python"]
        assert search.importance_min == 5
        assert search.importance_max == 10


class TestMetadataModels:
    def test_experience_metadata(self):
        metadata = ExperienceMetadata(
            context="Working on a project",
            outcome="Successfully completed",
            lessons_learned=["Always test first"],
        )
        assert metadata.context == "Working on a project"
        assert metadata.lessons_learned == ["Always test first"]

    def test_technical_metadata(self):
        metadata = TechnicalMetadata(
            language="python",
            repo="lucent",
            filename="server.py",
            code_snippet="def hello(): pass",
        )
        assert metadata.language == "python"
        assert metadata.repo == "lucent"
        assert metadata.filename == "server.py"

    def test_procedural_metadata(self):
        metadata = ProceduralMetadata(
            steps=[
                ProceduralStep(order=1, description="First step"),
                ProceduralStep(order=2, description="Second step"),
            ],
            prerequisites=["Python 3.12"],
            estimated_time="30 minutes",
        )
        assert len(metadata.steps) == 2
        assert metadata.steps[0].order == 1

    def test_goal_metadata(self):
        metadata = GoalMetadata(
            status=GoalStatus.ACTIVE,
            priority=5,
            blockers=["Need more time"],
        )
        assert metadata.status == GoalStatus.ACTIVE
        assert metadata.priority == 5

    def test_individual_metadata(self):
        metadata = IndividualMetadata(
            name="John Doe",
            relationship="colleague",
            organization="Acme Corp",
        )
        assert metadata.name == "John Doe"
        assert metadata.relationship == "colleague"


class TestMemory:
    def test_full_memory(self):
        now = datetime.now()
        memory = Memory(
            id=UUID("12345678-1234-5678-1234-567812345678"),
            username="testuser",
            type=MemoryType.EXPERIENCE,
            content="Test content",
            tags=["test"],
            importance=7,
            related_memory_ids=[],
            metadata={},
            created_at=now,
            updated_at=now,
        )
        assert str(memory.id) == "12345678-1234-5678-1234-567812345678"
        assert memory.username == "testuser"
        assert memory.deleted_at is None


class TestTruncatedMemory:
    def test_truncated_memory(self):
        now = datetime.now()
        memory = TruncatedMemory(
            id=UUID("12345678-1234-5678-1234-567812345678"),
            username="testuser",
            type=MemoryType.TECHNICAL,
            content="Truncated content...",
            content_truncated=True,
            tags=["test"],
            importance=5,
            related_memory_ids=[],
            created_at=now,
            updated_at=now,
        )
        assert memory.content_truncated is True


class TestSearchResult:
    def test_search_result(self):
        result = SearchResult(
            memories=[],
            total_count=100,
            offset=0,
            limit=5,
            has_more=True,
        )
        assert result.total_count == 100
        assert result.has_more is True
