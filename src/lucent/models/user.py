"""Pydantic models for user management."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class AuthProvider(str, Enum):
    """Supported authentication providers."""
    GOOGLE = "google"
    GITHUB = "github"
    SAML = "saml"
    LOCAL = "local"  # For development/testing without external auth


class CreateUserInput(BaseModel):
    """Input model for creating a new user."""
    external_id: str = Field(..., min_length=1, description="Unique ID from auth provider")
    provider: AuthProvider
    organization_id: UUID = Field(..., description="Organization this user belongs to")
    email: EmailStr | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateUserInput(BaseModel):
    """Input model for updating a user."""
    email: EmailStr | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    provider_metadata: dict[str, Any] | None = None
    is_active: bool | None = None


class User(BaseModel):
    """Full user model returned from database."""
    id: UUID
    external_id: str
    provider: AuthProvider
    organization_id: UUID
    email: str | None
    display_name: str | None
    avatar_url: str | None
    provider_metadata: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None


class UserSummary(BaseModel):
    """Condensed user info for embedding in responses."""
    id: UUID
    organization_id: UUID
    display_name: str | None
    email: str | None
    provider: AuthProvider
