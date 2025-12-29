"""Pydantic models for audit logging."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class AuditActionType(str, Enum):
    """Types of auditable actions on memories."""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"
    SHARE = "share"
    UNSHARE = "unshare"
    HARD_DELETE = "hard_delete"


class CreateAuditLogInput(BaseModel):
    """Input model for creating an audit log entry."""
    memory_id: UUID
    user_id: UUID | None = None
    organization_id: UUID | None = None
    action_type: AuditActionType
    changed_fields: list[str] | None = None
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class AuditLogEntry(BaseModel):
    """Full audit log entry returned from database."""
    id: UUID
    memory_id: UUID
    user_id: UUID | None
    organization_id: UUID | None
    action_type: AuditActionType
    created_at: datetime
    changed_fields: list[str] | None
    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None
    context: dict[str, Any]
    notes: str | None


class AuditLogSummary(BaseModel):
    """Summary view of audit log for listings."""
    id: UUID
    memory_id: UUID
    user_id: UUID | None
    action_type: AuditActionType
    created_at: datetime
    changed_fields: list[str] | None
