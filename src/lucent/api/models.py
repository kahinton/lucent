"""Pydantic models for API requests and responses."""

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from lucent.models.memory import GoalStatus

# RFC 6838 restricted-name token: type and subtype start with an ASCII
# alphanumeric and continue with alphanumerics or !#$&^_.+-. Parameters such as
# ``; charset=utf-8`` are deliberately NOT accepted because these values are
# placed directly into ``Response(media_type=...)``; the pattern also rejects
# CR/LF and every other control character, so a value can never split a
# response header.
_MIME_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
DEFAULT_MIME_TYPE = "application/octet-stream"


def normalize_mime_type(value: str) -> str:
    """Validate and canonicalise a media type as a bare ``type/subtype`` token.

    Raises ``ValueError`` for anything that is not a valid media type, which
    includes CR/LF-bearing values and other control-character injection
    attempts. Surrounding whitespace is trimmed and the result is lowercased.
    """
    candidate = value.strip().lower()
    if not candidate or not _MIME_TYPE_RE.fullmatch(candidate):
        raise ValueError(
            "mime_type must be a valid media type of the form 'type/subtype' "
            "(no parameters, CR/LF, or other control characters)"
        )
    return candidate


def safe_media_type(value: str | None) -> str:
    """Return a header-safe media type for a stored value.

    Used when rendering a download: rows written before validation existed (or
    through any path that bypasses it) must never reach
    ``Response(media_type=...)`` unchecked. Invalid values fall back to
    ``application/octet-stream``.
    """
    if not isinstance(value, str):
        return DEFAULT_MIME_TYPE
    try:
        return normalize_mime_type(value)
    except ValueError:
        return DEFAULT_MIME_TYPE

# =============================================================================
# Memory Models
# =============================================================================


class MemoryCreate(BaseModel):
    """Request model for creating a memory."""

    username: str | None = Field(
        default=None, description="Username for this memory (defaults to authenticated user)"
    )
    type: str = Field(..., description="Type: experience, technical, goal, individual")
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


class GoalMilestoneUpdate(BaseModel):
    """Bounded update for one existing goal milestone."""

    status: GoalStatus = Field(..., description="New status for this milestone")
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description="Optional current memory version for optimistic locking",
    )


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
    last_accessed_at: datetime | None
    access_count: int = 0


class MemoryAccessGrantCreate(BaseModel):
    """Request to grant memory read access to an organization, user, or group."""

    grantee_type: str = Field(pattern=r"^(organization|user|group)$")
    grantee_id: UUID | None = Field(
        default=None,
        description="Required for user and group grants; omitted for organization grants",
    )


class MemoryAccessGrantRevoke(BaseModel):
    """Request to revoke one memory read grant."""

    grantee_type: str = Field(pattern=r"^(organization|user|group)$")
    grantee_id: UUID | None = Field(
        default=None,
        description="Required for user and group grants; omitted for organization grants",
    )


class MemoryAccessGrantResponse(BaseModel):
    """A configured read grant for a memory."""

    id: UUID
    memory_id: UUID
    organization_id: UUID
    grantee_type: str
    grantee_user_id: UUID | None
    grantee_group_id: UUID | None
    created_by: UUID | None
    created_at: datetime
    user_display_name: str | None = None
    user_email: str | None = None
    group_name: str | None = None


class MemoryAccessGrantListResponse(BaseModel):
    """Read grants configured for a memory."""

    grants: list[MemoryAccessGrantResponse]


class AuthAccessGrantUpsert(BaseModel):
    """Set one principal's resource role; the new role replaces the old role."""

    grantee_type: Literal["organization", "user", "group"]
    grantee_id: UUID | None = None
    role: Literal["read", "write", "owner"] = "read"


class AuthAccessGrantRevoke(BaseModel):
    """Remove one principal's resource grant."""

    grantee_type: Literal["organization", "user", "group"]
    grantee_id: UUID | None = None


class AuthAccessGrantResponse(BaseModel):
    """A configured role grant for any auth-ID-backed resource."""

    id: UUID
    resource_type: str
    resource_id: UUID
    organization_id: UUID
    grantee_type: Literal["organization", "user", "group"]
    grantee_id: UUID
    role: Literal["read", "write", "owner"]
    granted_by: UUID | None
    created_at: datetime
    user_display_name: str | None = None
    user_email: str | None = None
    group_name: str | None = None
    organization_name: str | None = None


class AuthAccessGrantListResponse(BaseModel):
    """Roles configured for one resource."""

    grants: list[AuthAccessGrantResponse]




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

    query: str | None = Field(default=None, description="Hybrid relevance search query")
    username: str | None = Field(default=None, description="Filter by username")
    type: str | None = Field(default=None, description="Filter by memory type")
    tags: list[str] | None = Field(default=None, description="Filter by tags (any match)")
    importance_min: int | None = Field(default=None, ge=1, le=10)
    importance_max: int | None = Field(default=None, ge=1, le=10)
    created_after: datetime | None = Field(default=None)
    created_before: datetime | None = Field(default=None)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    include_archived: bool = Field(
        default=False,
        description=(
            "Include memories with lifecycle_stage in ('archived','forgotten'). "
            "When False (default) and the LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED "
            "rollout flag is on, those rows are excluded; with the flag off this "
            "field has no effect (baseline SQL preserved)."
        ),
    )


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
    last_accessed_at: datetime | None
    access_count: int = 0
    lifecycle_stage: str | None = None


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
    last_accessed: datetime | None


class AccessFrequencyItem(BaseModel):
    """Access frequency bucket item."""

    bucket_start: datetime
    access_count: int


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
    role: str = Field(
        default="member", description="Role: member, daemon, admin, owner, hyperadmin"
    )


class UserUpdate(BaseModel):
    """Request model for updating a user."""

    email: str | None = Field(default=None)
    display_name: str | None = Field(default=None)
    avatar_url: str | None = Field(default=None)
    is_active: bool | None = Field(default=None)


class UserRoleUpdate(BaseModel):
    """Request model for updating a user's role."""

    role: str = Field(..., description="New role: member, daemon, admin, owner, hyperadmin")


class PasswordResetResponse(BaseModel):
    """Response model for admin password reset."""

    success: bool
    message: str
    temp_password: str = Field(..., description="Temporary password (share securely)")


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
# Group Models
# =============================================================================


class GroupCreate(BaseModel):
    """Request model for creating a group."""

    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None


class GroupUpdate(BaseModel):
    """Request model for updating a group."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None


class GroupResponse(BaseModel):
    """Response model for a group."""

    id: UUID
    name: str
    description: str | None
    org_id: UUID
    member_count: int
    created_at: datetime
    updated_at: datetime


class GroupMemberAdd(BaseModel):
    """Request model for adding a member to a group."""

    user_id: UUID
    role: str = Field(default="member", pattern=r"^(member|admin)$")


class GroupMemberResponse(BaseModel):
    """Response model for a group member."""

    user_id: UUID
    display_name: str | None
    email: str | None
    role: str
    joined_at: datetime


# =============================================================================
# Organization Models
# =============================================================================


class OrganizationCreate(BaseModel):
    """Request model for creating an organization."""

    name: str = Field(..., min_length=1, max_length=255, description="Organization name")


class OrganizationUpdate(BaseModel):
    """Request model for updating an organization."""

    name: str = Field(..., min_length=1, max_length=255, description="New organization name")


class OrganizationResponse(BaseModel):
    """Response model for an organization."""

    id: UUID
    name: str
    status: str
    suspended_at: datetime | None
    created_at: datetime
    updated_at: datetime
    user_count: int | None = Field(
        default=None, description="Active human members (excludes daemon accounts)"
    )
    admin_count: int | None = Field(default=None, description="Members with admin+ role")
    owner_names: list[str] | None = Field(
        default=None, description="Display names of active owners"
    )
    api_key_count: int | None = Field(default=None, description="Active API keys")


class OrganizationListResponse(BaseModel):
    """Response model for a list of organizations."""

    organizations: list[OrganizationResponse]
    total_count: int
    offset: int
    limit: int
    has_more: bool


# =============================================================================
# Organization Provisioning (hyperadmin)
# =============================================================================


class OrganizationFirstAdmin(BaseModel):
    """The first human member created with a new organization."""

    email: str | None = Field(default=None, description="Login identifier")
    display_name: str | None = Field(default=None)
    external_id: str | None = Field(
        default=None, description="Identity id; defaults to the email"
    )
    password: str | None = Field(
        default=None,
        description=(
            "Plaintext password. Omit to have one generated and returned once "
            "in the response."
        ),
    )
    role: str = Field(default="owner", description="owner or admin")


class OrganizationProvisionRequest(BaseModel):
    """Create a fully usable organization in one call.

    This is the shape an external signup site needs: name the tenant, name its
    first admin, and get back the credentials that make it usable.
    """

    name: str = Field(..., min_length=1, max_length=255)
    first_admin: OrganizationFirstAdmin | None = Field(
        default=None,
        description="First member. Omit to create the organization only (legacy behaviour).",
    )
    api_key_name: str | None = Field(
        default="Default API key",
        description="Name for the minted key, or null to skip minting.",
    )
    model_id: str | None = Field(
        default=None,
        description=(
            "Model to enable and grant so the tenant can chat immediately. Also "
            "applied as the tenant's models.default_model. Only meaningful when "
            "the tenant has (or is given) a credential for that model's provider: "
            "a model id alone does not grant access to a provider."
        ),
    )
    providers: list["OrganizationProvisionProvider"] = Field(
        default_factory=list,
        description=(
            "Provider API keys to store for the new tenant, so it can call a "
            "model provider on first login. Values are stored encrypted and "
            "never returned."
        ),
    )
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Runtime settings to apply, keyed by setting key. Validated by the "
            "same rules the settings UI uses; an unappliable key is reported "
            "in `warnings` rather than aborting the tenant."
        ),
    )


class OrganizationProvisionProvider(BaseModel):
    """One provider credential to create with a new organization."""

    provider: str = Field(..., min_length=1, max_length=32)
    api_key: str = Field(..., min_length=1)


class OrganizationProvisionSettingsUpdate(BaseModel):
    """Apply runtime settings to an existing organization in one call."""

    settings: dict[str, Any] = Field(default_factory=dict)


class ProvisionedUserResponse(BaseModel):
    """The created first member."""

    id: UUID
    email: str | None
    display_name: str | None
    role: str
    must_change_password: bool


class OrganizationProvisionResponse(BaseModel):
    """Result of provisioning, including the one-time credentials."""

    organization: OrganizationResponse
    user: ProvisionedUserResponse
    api_key: str | None = Field(
        default=None, description="Plaintext API key — shown once, never stored"
    )
    temporary_password: str | None = Field(
        default=None, description="Generated password — shown once"
    )
    seeded: dict[str, int] = Field(default_factory=dict)
    model_id: str | None = None
    providers_configured: list[str] = Field(default_factory=list)
    models_discovered: int = Field(
        default=0,
        description="Models registered by discovery for the supplied provider credentials.",
    )
    settings_applied: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class OrganizationAdminCreate(BaseModel):
    """Add a member to any organization (hyperadmin)."""

    email: str | None = Field(default=None)
    display_name: str | None = Field(default=None)
    external_id: str | None = Field(default=None)
    role: str = Field(default="admin", description="member, admin, or owner")
    password: str | None = Field(
        default=None, description="Omit to generate a temporary password."
    )


class OrganizationAdminCreated(BaseModel):
    """A member created by a hyperadmin, with one-time credentials."""

    user: UserResponse
    temporary_password: str | None = None
    api_key: str | None = None


class OrganizationApiKeyCreate(BaseModel):
    """Mint an API key for a member of any organization."""

    name: str = Field(default="Default API key", max_length=200)


class OrganizationApiKeyCreated(BaseModel):
    """The minted key. The plaintext is returned exactly once."""

    id: UUID
    name: str
    key_prefix: str
    api_key: str


class OrganizationSettingValue(BaseModel):
    """A single runtime setting value for an organization.

    ``value`` is the raw value (a bool, number, string, or JSON object) so a
    caller can read it, edit it, and write it back unchanged. ``display_value``
    is the human-readable rendering the settings UI shows (for example
    "Disabled" for a false boolean) and is for presentation only.
    """

    key: str
    value: Any
    value_type: str
    source: str = Field(description="database, environment, or default")
    title: str | None = None
    section: str | None = None
    display_value: str | None = None


class OrganizationSettingsResponse(BaseModel):
    """Runtime settings for one organization, grouped by section."""

    organization_id: UUID
    settings: list[OrganizationSettingValue]
    sections: dict[str, list[OrganizationSettingValue]] = Field(default_factory=dict)


class OrganizationSettingUpdate(BaseModel):
    """Set or reset one runtime setting."""

    value: Any = Field(default=None, description="Ignored when reset is true")
    reset: bool = Field(
        default=False, description="Delete the DB override and fall back to env/default"
    )


class InstanceAuditEntry(BaseModel):
    """One instance-level (cross-organization) audit event."""

    id: UUID
    organization_id: UUID
    actor_user_id: UUID | None
    actor_display_name: str | None = None
    action: str
    entity_type: str
    entity_id: UUID | None = None
    entity_label: str | None = None
    target_organization_id: str | None = None
    changed_fields: list[str] | None = None
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    notes: str | None = None
    created_at: datetime


class InstanceAuditResponse(BaseModel):
    """Paginated instance audit trail."""

    entries: list[InstanceAuditEntry]
    total_count: int
    offset: int
    limit: int
    has_more: bool


class OrganizationDeleteRequest(BaseModel):
    """Permanently delete a tenant. ``confirm_name`` must match exactly."""

    confirm_name: str = Field(..., description="The organization's current name")


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

    type: str = Field(..., description="Type: experience, technical, goal, individual")
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
# =============================================================================
# Organization Providers & Models (hyperadmin)
# =============================================================================


class OrganizationCredentialUpdate(BaseModel):
    """Store one provider API key for an organization."""

    value: str = Field(
        ..., min_length=1, description="Provider API key. Stored encrypted, never returned."
    )


class OrganizationBackendCreate(BaseModel):
    """Create an engine backend (routing override) for an organization."""

    name: str = Field(..., min_length=1, max_length=128)
    engine: str = Field(..., description="langchain, copilot, claude_code, or codex")
    provider: str = Field(..., min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Engine secret fields keyed by field name (gateway token etc.). "
            "Provider API keys are set on the credential endpoint instead."
        ),
    )
    is_enabled: bool = True


class OrganizationModelCreate(BaseModel):
    """Define a model for a specific organization and entitle the org to it."""

    model_id: str = Field(..., min_length=1, max_length=128)
    provider: str = Field(..., min_length=1, max_length=32)
    name: str = Field(default="", max_length=128)
    engine: str | None = Field(
        default=None, description="Pin this model to one engine, or leave unset"
    )
    category: str = "general"
    model_class: str = Field(default="language", description="language or decision")
    context_window: int = Field(default=0, ge=0)
    supports_tools: bool = True
    supports_vision: bool = False
    notes: str = ""
    is_enabled: bool = True
    grant_to_org: bool = Field(
        default=True,
        description=(
            "Grant the organization read clearance. An enabled model without a "
            "grant is not usable."
        ),
    )


class OrganizationModelToggle(BaseModel):
    """Enable or disable one model."""

    enabled: bool


class EngineProviderOption(BaseModel):
    """One provider an engine can be configured with."""

    name: str
    label: str


class EngineCatalogEntry(BaseModel):
    """An engine, the providers it accepts, and its config fields."""

    engine: str
    providers: list[EngineProviderOption]
    fields: list["CredentialFieldSpec"] = Field(default_factory=list)
    availability: Any = None


class CredentialFieldSpec(BaseModel):
    """One configurable field on an engine backend."""

    key: str
    label: str
    field_type: str
    required: bool = False
    is_secret: bool = False
    choices: list[str] = Field(default_factory=list)
    help_text: str = ""
