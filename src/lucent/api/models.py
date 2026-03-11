"""Pydantic models for API requests and responses."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# =============================================================================
# Memory Models
# =============================================================================


class MemoryCreate(BaseModel):
    """Request model for creating a memory."""

    username: str | None = Field(
        default=None, description="Username for this memory (defaults to authenticated user)"
    )
    type: str = Field(..., description="Type: experience, technical, procedural, goal, individual")
    content: str = Field(..., description="Main content/description of the memory")
    tags: list[str] | None = Field(default=None, description="Tags for categorization")
    importance: int = Field(default=5, ge=1, le=10, description="Importance rating 1-10")
    related_memory_ids: list[UUID] | None = Field(default=None, description="Related memory UUIDs")
    metadata: dict[str, Any] | None = Field(default=None, description="Type-specific metadata")
    shared: bool = Field(default=False, description="Visible to other org members")


class MemoryUpdate(BaseModel):
    """Request model for updating a memory."""

    content: str | None = Field(default=None, description="New content")
    tags: list[str] | None = Field(default=None, description="New tags (replaces existing)")
    importance: int | None = Field(default=None, ge=1, le=10, description="New importance")
    related_memory_ids: list[UUID] | None = Field(default=None, description="New related memories")
    metadata: dict[str, Any] | None = Field(default=None, description="New metadata")


class MemoryResponse(BaseModel):
    """Response model for a memory."""

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


class MemoryListResponse(BaseModel):
    """Response model for a list of memories."""

    memories: list[MemoryResponse]
    total_count: int
    offset: int
    limit: int
    has_more: bool


# =============================================================================
# Search Models
# =============================================================================


class SearchRequest(BaseModel):
    """Request model for searching memories."""

    query: str | None = Field(default=None, description="Search query (fuzzy match)")
    username: str | None = Field(default=None, description="Filter by username")
    type: str | None = Field(default=None, description="Filter by memory type")
    tags: list[str] | None = Field(default=None, description="Filter by tags (any match)")
    importance_min: int | None = Field(default=None, ge=1, le=10)
    importance_max: int | None = Field(default=None, ge=1, le=10)
    created_after: datetime | None = Field(default=None)
    created_before: datetime | None = Field(default=None)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class SearchResultMemory(BaseModel):
    """Memory in search results (with similarity score, content may be truncated)."""

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


class SearchResponse(BaseModel):
    """Response model for search results."""

    memories: list[SearchResultMemory]
    total_count: int
    offset: int
    limit: int
    has_more: bool


# =============================================================================
# Audit Models
# =============================================================================


class AuditLogEntry(BaseModel):
    """Audit log entry."""

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


class AuditLogResponse(BaseModel):
    """Response model for audit log queries."""

    entries: list[AuditLogEntry]
    total_count: int
    offset: int
    limit: int
    has_more: bool


# =============================================================================
# Access Log Models
# =============================================================================


class AccessLogEntry(BaseModel):
    """Access log entry."""

    id: UUID
    memory_id: UUID
    user_id: UUID | None
    organization_id: UUID | None
    access_type: str
    accessed_at: datetime
    context: dict[str, Any]


class AccessLogResponse(BaseModel):
    """Response model for access log queries."""

    entries: list[AccessLogEntry]
    total_count: int
    offset: int
    limit: int
    has_more: bool


class MostAccessedItem(BaseModel):
    """Item in most accessed list."""

    memory_id: UUID
    access_count: int
    last_accessed: datetime


# =============================================================================
# User Models
# =============================================================================


class UserCreate(BaseModel):
    """Request model for creating a user."""

    external_id: str = Field(..., description="ID from auth provider")
    provider: str = Field(..., description="Auth provider: google, github, saml, local")
    email: str | None = Field(default=None)
    display_name: str | None = Field(default=None)
    avatar_url: str | None = Field(default=None)
    role: str = Field(default="member", description="Role: member, admin, owner")


class UserUpdate(BaseModel):
    """Request model for updating a user."""

    email: str | None = Field(default=None)
    display_name: str | None = Field(default=None)
    avatar_url: str | None = Field(default=None)
    is_active: bool | None = Field(default=None)


class UserRoleUpdate(BaseModel):
    """Request model for updating a user's role."""

    role: str = Field(..., description="New role: member, admin, owner")


class UserResponse(BaseModel):
    """Response model for a user."""

    id: UUID
    external_id: str
    provider: str
    organization_id: UUID | None
    email: str | None
    display_name: str | None
    avatar_url: str | None
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None


class UserListResponse(BaseModel):
    """Response model for a list of users."""

    users: list[UserResponse]
    total_count: int


# =============================================================================
# Organization Models
# =============================================================================


class OrganizationCreate(BaseModel):
    """Request model for creating an organization."""

    name: str = Field(..., description="Organization name")


class OrganizationUpdate(BaseModel):
    """Request model for updating an organization."""

    name: str = Field(..., description="New organization name")


class OrganizationResponse(BaseModel):
    """Response model for an organization."""

    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


class OrganizationListResponse(BaseModel):
    """Response model for a list of organizations."""

    organizations: list[OrganizationResponse]
    total_count: int
    offset: int
    limit: int
    has_more: bool


# =============================================================================
# Tag Models
# =============================================================================


class TagCount(BaseModel):
    """Tag with usage count."""

    tag: str
    count: int


class TagListResponse(BaseModel):
    """Response model for tag list."""

    tags: list[TagCount]
    total_count: int


class TagSuggestion(BaseModel):
    """Tag suggestion with similarity."""

    tag: str
    count: int
    similarity: float


class TagSuggestionsResponse(BaseModel):
    """Response model for tag suggestions."""

    suggestions: list[TagSuggestion]
    query: str


# =============================================================================
# Export Models
# =============================================================================


class ExportMetadata(BaseModel):
    """Metadata about the export."""

    exported_at: datetime
    total_count: int
    filters: dict[str, Any]
    format: str


class ExportResponse(BaseModel):
    """Response model for memory export."""

    metadata: ExportMetadata
    memories: list[MemoryResponse]


class ImportMemory(BaseModel):
    """A single memory in an import request."""

    type: str = Field(..., description="Type: experience, technical, procedural, goal, individual")
    content: str = Field(..., min_length=1, description="Main content of the memory")
    username: str | None = Field(
        default=None, description="Original username (defaults to authenticated user)"
    )
    tags: list[str] | None = Field(default=None, description="Tags for categorization")
    importance: int = Field(default=5, ge=1, le=10, description="Importance rating 1-10")
    related_memory_ids: list[str] | None = Field(
        default=None, description="Related memory UUIDs as strings"
    )
    metadata: dict[str, Any] | None = Field(default=None, description="Type-specific metadata")
    created_at: datetime | None = Field(default=None, description="Original creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Original update timestamp")


class ImportRequest(BaseModel):
    """Request model for importing memories."""

    memories: list[ImportMemory] = Field(..., description="List of memories to import")


class ImportResponse(BaseModel):
    """Response model for memory import."""

    imported: int = Field(..., description="Number of memories successfully imported")
    skipped: int = Field(..., description="Number of duplicate memories skipped")
    errors: list[dict[str, str]] = Field(default_factory=list, description="Errors encountered")
    total: int = Field(..., description="Total memories in request")


# =============================================================================
# Daemon Task Models
# =============================================================================


class DaemonTaskCreate(BaseModel):
    """Request model for submitting a daemon task."""

    description: str = Field(..., min_length=1, description="Task description/instructions")
    agent_type: str = Field(
        default="code",
        description="Agent type: research, code, memory, reflection, documentation, planning",
    )
    priority: str = Field(default="medium", description="Priority: low, medium, high")
    context: str | None = Field(default=None, description="Additional context for the agent")
    tags: list[str] | None = Field(default=None, description="Extra tags for categorization")


class DaemonTaskResponse(BaseModel):
    """Response model for a daemon task."""

    id: UUID
    description: str
    agent_type: str
    priority: str
    status: str  # pending, claimed, completed, failed
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    result: str | None = None
    claimed_by: str | None = None


class DaemonTaskListResponse(BaseModel):
    """Response model for a list of daemon tasks."""

    tasks: list[DaemonTaskResponse]
    total_count: int


# =============================================================================
# Daemon Message Models
# =============================================================================


class DaemonMessageCreate(BaseModel):
    """Request model for sending a daemon message."""

    content: str = Field(..., min_length=1, description="Message content")
    in_reply_to: UUID | None = Field(default=None, description="ID of message being replied to")


class DaemonMessageResponse(BaseModel):
    """Response model for a daemon message."""

    id: UUID
    content: str
    sender: str  # "human" or "daemon"
    acknowledged: bool
    created_at: datetime
    updated_at: datetime
    in_reply_to: UUID | None = None
    acknowledged_at: str | None = None


class DaemonMessageListResponse(BaseModel):
    """Response model for a list of daemon messages."""

    messages: list[DaemonMessageResponse]
    total_count: int


# =============================================================================
# Error Models
# =============================================================================


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str | None = None


class SuccessResponse(BaseModel):
    """Standard success response."""

    success: bool
    message: str
