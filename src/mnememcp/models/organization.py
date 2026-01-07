"""Pydantic models for organization management."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreateOrganizationInput(BaseModel):
    """Input model for creating a new organization."""
    name: str = Field(..., min_length=1, description="Organization name")


class UpdateOrganizationInput(BaseModel):
    """Input model for updating an organization."""
    name: str | None = None


class Organization(BaseModel):
    """Full organization model returned from database."""
    id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


class OrganizationSummary(BaseModel):
    """Condensed organization info for embedding in responses."""
    id: UUID
    name: str
