"""Models for enterprise credential management."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class CredentialIntegrationType(str, Enum):
    GITHUB = "github"
    SLACK = "slack"
    JIRA = "jira"
    CUSTOM = "custom"


class CredentialKind(str, Enum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"


class CredentialScopeType(str, Enum):
    USER = "user"
    AGENT = "agent"


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class OAuthProvider(str, Enum):
    GITHUB = "github"
    SLACK = "slack"
    JIRA = "jira"


class CredentialCreate(BaseModel):
    integration_type: CredentialIntegrationType
    credential_kind: CredentialKind = CredentialKind.OAUTH2
    scope_type: CredentialScopeType = CredentialScopeType.USER
    owner_user_id: UUID | None = None
    owner_agent_id: UUID | None = None
    display_name: str = Field(..., min_length=1, max_length=256)
    scopes: list[str] = Field(default_factory=list)

    access_token: str | None = None
    refresh_token: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CredentialUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    scopes: list[str] | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    metadata: dict[str, str] | None = None
    status: CredentialStatus | None = None


class CredentialResponse(BaseModel):
    id: UUID
    organization_id: UUID
    integration_type: CredentialIntegrationType
    credential_kind: CredentialKind
    scope_type: CredentialScopeType
    owner_user_id: UUID | None = None
    owner_agent_id: UUID | None = None
    display_name: str
    scopes: list[str]
    status: CredentialStatus
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    refresh_token_version: int
    token_rotated_at: datetime | None = None
    created_by: UUID
    updated_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class CredentialListResponse(BaseModel):
    credentials: list[CredentialResponse]
    total_count: int


class OAuthStartRequest(BaseModel):
    provider: OAuthProvider
    display_name: str = Field(..., min_length=1, max_length=256)
    scope_type: CredentialScopeType = CredentialScopeType.USER
    owner_user_id: UUID | None = None
    owner_agent_id: UUID | None = None
    redirect_uri: str = Field(..., min_length=1)
    scopes: list[str] = Field(default_factory=list)


class OAuthStartResponse(BaseModel):
    provider: OAuthProvider
    authorization_url: str
    state: str
    expires_at: datetime


class OAuthCallbackRequest(BaseModel):
    provider: OAuthProvider
    code: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)


class CredentialRefreshResponse(BaseModel):
    id: UUID
    refreshed: bool
    rotated_refresh_token: bool
    access_token_expires_at: datetime | None = None
    refresh_token_version: int
