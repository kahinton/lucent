"""Data models for the integrations subsystem.

IntegrationEvent is the normalized dataclass shared across all adapters.
Pydantic models define the API request/response schemas for REST endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# =============================================================================
# Enums (mirror DB CHECK constraints from migration 028)
# =============================================================================


class IntegrationType(str, Enum):
    """Supported integration platforms."""

    SLACK = "slack"
    DISCORD = "discord"


class IntegrationStatus(str, Enum):
    """Lifecycle states for an integration."""

    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"
    DELETED = "deleted"


class UserLinkStatus(str, Enum):
    """Lifecycle states for a user link."""

    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    ORPHANED = "orphaned"
    DISABLED = "disabled"


class VerificationMethod(str, Enum):
    """How a user link was verified."""

    PAIRING_CODE = "pairing_code"
    ADMIN = "admin"
    OAUTH = "oauth"


class PairingChallengeStatus(str, Enum):
    """Lifecycle states for a pairing challenge."""

    PENDING = "pending"
    USED = "used"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class EventType(str, Enum):
    """Normalized event types across platforms."""

    MESSAGE = "message"
    COMMAND = "command"
    INTERACTION = "interaction"
    URL_VERIFICATION = "url_verification"
    UNKNOWN = "unknown"


# =============================================================================
# Core dataclass — the normalized event all adapters produce
# =============================================================================


@dataclass(frozen=True)
class IntegrationEvent:
    """Platform-agnostic representation of an inbound integration event.

    Produced by IntegrationAdapter.parse_event(). Downstream handlers
    work exclusively with this type, never with raw platform payloads.
    """

    platform: str
    event_type: EventType
    external_user_id: str
    channel_id: str
    text: str = ""
    thread_id: str | None = None
    external_workspace_id: str | None = None
    timestamp: datetime | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# API request/response schemas (Pydantic)
# =============================================================================


class IntegrationCreate(BaseModel):
    """Request model for creating an integration."""

    type: IntegrationType = Field(..., description="Platform type")
    external_workspace_id: str | None = Field(
        default=None, description="Platform workspace/guild ID"
    )
    config: dict[str, Any] = Field(
        ..., description="Platform credentials (will be encrypted at rest)"
    )
    allowed_channels: list[str] = Field(
        default_factory=list, description="Channel IDs allowed for this integration"
    )


class IntegrationUpdate(BaseModel):
    """Request model for updating an integration."""

    status: IntegrationStatus | None = Field(default=None, description="New status")
    allowed_channels: list[str] | None = Field(
        default=None, description="New allowed channels (replaces existing)"
    )
    config: dict[str, Any] | None = Field(
        default=None, description="New credentials (will be encrypted at rest)"
    )


class IntegrationResponse(BaseModel):
    """Response model for an integration (config is never returned)."""

    id: UUID
    organization_id: UUID
    type: IntegrationType
    status: IntegrationStatus
    external_workspace_id: str | None
    allowed_channels: list[str]
    config_version: int
    created_by: UUID
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None
    revoked_at: datetime | None


class IntegrationListResponse(BaseModel):
    """Response model for listing integrations."""

    integrations: list[IntegrationResponse]
    total_count: int


class UserLinkCreate(BaseModel):
    """Request model for creating a user link (admin flow)."""

    integration_id: UUID = Field(..., description="Target integration")
    user_id: UUID = Field(..., description="Lucent user to link")
    external_user_id: str = Field(..., description="Platform user ID")
    external_workspace_id: str | None = Field(
        default=None, description="Platform workspace/guild ID"
    )
    verification_method: VerificationMethod = Field(
        default=VerificationMethod.PAIRING_CODE,
        description="How the link was verified",
    )


class UserLinkResponse(BaseModel):
    """Response model for a user link."""

    id: UUID
    organization_id: UUID
    integration_id: UUID
    user_id: UUID
    provider: IntegrationType
    external_user_id: str
    external_workspace_id: str | None
    status: UserLinkStatus
    verification_method: VerificationMethod
    linked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserLinkListResponse(BaseModel):
    """Response model for listing user links."""

    links: list[UserLinkResponse]
    total_count: int


class PairingChallengeCreate(BaseModel):
    """Request model for generating a pairing code."""

    integration_id: UUID = Field(..., description="Target integration")


class PairingChallengeResponse(BaseModel):
    """Response model for a pairing challenge (code only shown once)."""

    id: UUID
    integration_id: UUID
    user_id: UUID
    code: str | None = Field(
        default=None, description="Plaintext pairing code (only in create response)"
    )
    expires_at: datetime
    status: PairingChallengeStatus
    created_at: datetime


class PairingRedeemRequest(BaseModel):
    """Request model for redeeming a pairing code (from platform DM)."""

    code: str = Field(..., min_length=1, description="Pairing code from Lucent UI")
