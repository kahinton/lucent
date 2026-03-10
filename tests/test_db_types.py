"""Tests for database TypedDict definitions (lucent.db.types)."""

from datetime import datetime, timezone
from typing import get_type_hints
from uuid import uuid4

from lucent.db.types import (
    AccessLogRecord,
    AccessLogResult,
    ApiKeyRecord,
    ApiKeyVerifyRecord,
    AuditLogRecord,
    AuditLogResult,
    MemoryRecord,
    MemorySearchRecord,
    MemorySearchResult,
    MostAccessedRecord,
    OrganizationListResult,
    OrganizationRecord,
    TagCount,
    TagSuggestion,
    UserRecord,
)


class TestMemoryRecord:
    """Tests for MemoryRecord TypedDict."""

    def test_can_construct(self):
        """Test that a MemoryRecord can be constructed with valid data."""
        now = datetime.now(timezone.utc)
        record: MemoryRecord = {
            "id": uuid4(),
            "username": "testuser",
            "type": "experience",
            "content": "Some content",
            "tags": ["test"],
            "importance": 5,
            "related_memory_ids": [],
            "metadata": {},
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "user_id": uuid4(),
            "organization_id": None,
            "shared": False,
            "last_accessed_at": None,
        }
        assert record["username"] == "testuser"
        assert record["type"] == "experience"
        assert record["deleted_at"] is None

    def test_has_expected_fields(self):
        """Test that MemoryRecord has all expected type hints."""
        hints = get_type_hints(MemoryRecord)
        expected = {
            "id", "username", "type", "content", "tags", "importance",
            "related_memory_ids", "metadata", "created_at", "updated_at",
            "deleted_at", "user_id", "organization_id", "shared", "last_accessed_at",
        }
        assert set(hints.keys()) == expected


class TestMemorySearchRecord:
    """Tests for MemorySearchRecord TypedDict."""

    def test_includes_similarity_and_truncation(self):
        """Test search-specific fields are present."""
        hints = get_type_hints(MemorySearchRecord)
        assert "similarity_score" in hints
        assert "content_truncated" in hints

    def test_can_construct(self):
        """Test construction with similarity score."""
        now = datetime.now(timezone.utc)
        record: MemorySearchRecord = {
            "id": uuid4(),
            "username": "testuser",
            "type": "technical",
            "content": "Content",
            "content_truncated": False,
            "tags": [],
            "importance": 3,
            "related_memory_ids": [],
            "created_at": now,
            "updated_at": now,
            "similarity_score": 0.95,
            "user_id": None,
            "organization_id": None,
            "shared": False,
            "last_accessed_at": None,
        }
        assert record["similarity_score"] == 0.95
        assert record["content_truncated"] is False


class TestMemorySearchResult:
    """Tests for MemorySearchResult TypedDict."""

    def test_has_pagination_fields(self):
        """Test that search result includes pagination metadata."""
        hints = get_type_hints(MemorySearchResult)
        assert "memories" in hints
        assert "total_count" in hints
        assert "offset" in hints
        assert "limit" in hints
        assert "has_more" in hints

    def test_can_construct(self):
        """Test construction of a search result."""
        result: MemorySearchResult = {
            "memories": [],
            "total_count": 0,
            "offset": 0,
            "limit": 50,
            "has_more": False,
        }
        assert result["total_count"] == 0
        assert result["has_more"] is False


class TestTagCount:
    """Tests for TagCount TypedDict."""

    def test_fields(self):
        """Test TagCount has tag and count."""
        hints = get_type_hints(TagCount)
        assert set(hints.keys()) == {"tag", "count"}

    def test_can_construct(self):
        """Test constructing a TagCount."""
        tc: TagCount = {"tag": "python", "count": 42}
        assert tc["tag"] == "python"
        assert tc["count"] == 42


class TestTagSuggestion:
    """Tests for TagSuggestion TypedDict."""

    def test_includes_similarity(self):
        """Test TagSuggestion has similarity field beyond TagCount."""
        hints = get_type_hints(TagSuggestion)
        assert "similarity" in hints
        assert "tag" in hints
        assert "count" in hints

    def test_can_construct(self):
        """Test constructing a TagSuggestion."""
        ts: TagSuggestion = {"tag": "py", "count": 10, "similarity": 0.8}
        assert ts["similarity"] == 0.8


class TestUserRecord:
    """Tests for UserRecord TypedDict."""

    def test_has_expected_fields(self):
        """Test that UserRecord has all expected fields."""
        hints = get_type_hints(UserRecord)
        expected = {
            "id", "external_id", "provider", "organization_id", "email",
            "display_name", "avatar_url", "provider_metadata", "is_active",
            "created_at", "updated_at", "last_login_at", "role",
        }
        assert set(hints.keys()) == expected

    def test_can_construct(self):
        """Test construction of a UserRecord."""
        now = datetime.now(timezone.utc)
        record: UserRecord = {
            "id": uuid4(),
            "external_id": "ext_123",
            "provider": "github",
            "organization_id": None,
            "email": "test@example.com",
            "display_name": "Test User",
            "avatar_url": None,
            "provider_metadata": {},
            "is_active": True,
            "created_at": now,
            "updated_at": now,
            "last_login_at": None,
            "role": "member",
        }
        assert record["provider"] == "github"
        assert record["role"] == "member"


class TestApiKeyRecord:
    """Tests for ApiKeyRecord TypedDict."""

    def test_has_expected_fields(self):
        """Test that ApiKeyRecord has all expected fields."""
        hints = get_type_hints(ApiKeyRecord)
        expected = {
            "id", "user_id", "organization_id", "name", "key_prefix",
            "scopes", "last_used_at", "use_count", "expires_at",
            "is_active", "created_at", "updated_at",
        }
        assert set(hints.keys()) == expected

    def test_does_not_expose_key_hash(self):
        """Test that ApiKeyRecord does not include key_hash (security)."""
        hints = get_type_hints(ApiKeyRecord)
        assert "key_hash" not in hints


class TestApiKeyVerifyRecord:
    """Tests for ApiKeyVerifyRecord TypedDict."""

    def test_extends_api_key_with_user_info(self):
        """Test that verify record has user info fields beyond ApiKeyRecord."""
        hints = get_type_hints(ApiKeyVerifyRecord)
        assert "user_email" in hints
        assert "user_display_name" in hints
        assert "user_role" in hints

    def test_has_all_api_key_fields(self):
        """Test that verify record includes all base API key fields."""
        base_hints = get_type_hints(ApiKeyRecord)
        verify_hints = get_type_hints(ApiKeyVerifyRecord)
        for field in base_hints:
            assert field in verify_hints


class TestOrganizationRecord:
    """Tests for OrganizationRecord TypedDict."""

    def test_fields(self):
        """Test OrganizationRecord has expected fields."""
        hints = get_type_hints(OrganizationRecord)
        assert set(hints.keys()) == {"id", "name", "created_at", "updated_at"}


class TestOrganizationListResult:
    """Tests for OrganizationListResult TypedDict."""

    def test_has_pagination(self):
        """Test pagination fields are present."""
        hints = get_type_hints(OrganizationListResult)
        assert "organizations" in hints
        assert "total_count" in hints
        assert "has_more" in hints


class TestAuditLogRecord:
    """Tests for AuditLogRecord TypedDict."""

    def test_has_expected_fields(self):
        """Test AuditLogRecord has all expected fields."""
        hints = get_type_hints(AuditLogRecord)
        expected = {
            "id", "memory_id", "user_id", "organization_id", "action_type",
            "created_at", "changed_fields", "old_values", "new_values",
            "context", "notes",
        }
        assert set(hints.keys()) == expected


class TestAuditLogResult:
    """Tests for AuditLogResult TypedDict."""

    def test_has_pagination(self):
        """Test AuditLogResult has standard pagination."""
        hints = get_type_hints(AuditLogResult)
        assert "entries" in hints
        assert "total_count" in hints
        assert "offset" in hints
        assert "limit" in hints
        assert "has_more" in hints


class TestAccessLogRecord:
    """Tests for AccessLogRecord TypedDict."""

    def test_has_expected_fields(self):
        """Test AccessLogRecord has expected fields."""
        hints = get_type_hints(AccessLogRecord)
        expected = {
            "id", "memory_id", "user_id", "organization_id",
            "access_type", "accessed_at", "context",
        }
        assert set(hints.keys()) == expected


class TestAccessLogResult:
    """Tests for AccessLogResult TypedDict."""

    def test_has_pagination(self):
        """Test AccessLogResult has standard pagination."""
        hints = get_type_hints(AccessLogResult)
        assert "entries" in hints
        assert "total_count" in hints
        assert "has_more" in hints


class TestMostAccessedRecord:
    """Tests for MostAccessedRecord TypedDict."""

    def test_fields(self):
        """Test MostAccessedRecord has expected fields."""
        hints = get_type_hints(MostAccessedRecord)
        assert set(hints.keys()) == {"memory_id", "access_count", "last_accessed"}


class TestModuleExports:
    """Tests for module-level exports."""

    def test_all_types_importable_from_db_package(self):
        """Test that all types are importable from lucent.db."""
        from lucent.db import (  # noqa: F401
            AccessLogRecord,
            AccessLogResult,
            ApiKeyRecord,
            ApiKeyVerifyRecord,
            AuditLogRecord,
            AuditLogResult,
            MemoryRecord,
            MemorySearchRecord,
            MemorySearchResult,
            MostAccessedRecord,
            OrganizationListResult,
            OrganizationRecord,
            TagCount,
            TagSuggestion,
            UserRecord,
        )

    def test_pagination_pattern_consistency(self):
        """Test that all paginated result types follow the same pattern."""
        paginated_types = [
            MemorySearchResult,
            OrganizationListResult,
            AuditLogResult,
            AccessLogResult,
        ]
        pagination_fields = {"total_count", "offset", "limit", "has_more"}

        for typed_dict in paginated_types:
            hints = get_type_hints(typed_dict)
            for field in pagination_fields:
                assert field in hints, (
                    f"{typed_dict.__name__} missing pagination field '{field}'"
                )
