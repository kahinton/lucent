"""TypedDict definitions for database repository return values.

These provide type hints for the dictionaries returned by repository methods,
improving type safety and IDE support without modifying the repository implementations.
"""

from datetime import datetime
from typing import Any, TypedDict
from uuid import UUID


class MemoryRecord(TypedDict):
    """Record returned by MemoryRepository methods."""
    
    id: UUID
    username: str
    type: str
    content: str
    tags: list[str]
    importance: int
    related_memory_ids: list[UUID]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    user_id: UUID | None
    organization_id: UUID | None
    shared: bool
    last_accessed_at: datetime | None


class MemorySearchRecord(TypedDict):
    """Record returned in memory search results (includes similarity score)."""
    
    id: UUID
    username: str
    type: str
    content: str
    content_truncated: bool
    tags: list[str]
    importance: int
    related_memory_ids: list[UUID]
    created_at: datetime
    updated_at: datetime
    similarity_score: float | None
    user_id: UUID | None
    organization_id: UUID | None
    shared: bool
    last_accessed_at: datetime | None


class MemorySearchResult(TypedDict):
    """Result returned by MemoryRepository.search() and search_full()."""
    
    memories: list[MemorySearchRecord]
    total_count: int
    offset: int
    limit: int
    has_more: bool


class TagCount(TypedDict):
    """Tag with usage count returned by get_existing_tags()."""
    
    tag: str
    count: int


class TagSuggestion(TypedDict):
    """Tag suggestion with similarity returned by get_tag_suggestions()."""
    
    tag: str
    count: int
    similarity: float


class UserRecord(TypedDict):
    """Record returned by UserRepository methods."""
    
    id: UUID
    external_id: str
    provider: str
    organization_id: UUID | None
    email: str | None
    display_name: str | None
    avatar_url: str | None
    provider_metadata: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    role: str


class ApiKeyRecord(TypedDict):
    """Record returned by ApiKeyRepository methods."""
    
    id: UUID
    user_id: UUID
    organization_id: UUID | None
    name: str
    key_prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    use_count: int
    expires_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ApiKeyVerifyRecord(TypedDict):
    """Record returned by ApiKeyRepository.verify() (includes user info)."""
    
    id: UUID
    user_id: UUID
    organization_id: UUID | None
    name: str
    key_prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    use_count: int
    expires_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    user_email: str | None
    user_display_name: str | None
    user_role: str


class OrganizationRecord(TypedDict):
    """Record returned by OrganizationRepository methods."""
    
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


class OrganizationListResult(TypedDict):
    """Result returned by OrganizationRepository.list()."""
    
    organizations: list[OrganizationRecord]
    total_count: int
    offset: int
    limit: int
    has_more: bool


class AuditLogRecord(TypedDict):
    """Record returned by AuditRepository methods."""
    
    id: UUID
    memory_id: UUID
    user_id: UUID | None
    organization_id: UUID | None
    action_type: str
    created_at: datetime
    changed_fields: list[str] | None
    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None
    context: dict[str, Any]
    notes: str | None


class AuditLogResult(TypedDict):
    """Result returned by AuditRepository query methods."""
    
    entries: list[AuditLogRecord]
    total_count: int
    offset: int
    limit: int
    has_more: bool


class AccessLogRecord(TypedDict):
    """Record returned by AccessRepository methods."""
    
    id: UUID
    memory_id: UUID
    user_id: UUID | None
    organization_id: UUID | None
    access_type: str
    accessed_at: datetime
    context: dict[str, Any]


class AccessLogResult(TypedDict):
    """Result returned by AccessRepository.get_access_history()."""
    
    entries: list[AccessLogRecord]
    total_count: int
    offset: int
    limit: int
    has_more: bool


class MostAccessedRecord(TypedDict):
    """Record returned by AccessRepository.get_most_accessed()."""
    
    memory_id: UUID
    access_count: int
    last_accessed: datetime
