"""Pydantic models for mnemeMCP memory types."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class MemoryType(str, Enum):
    """Types of memories that can be stored."""

    EXPERIENCE = "experience"
    TECHNICAL = "technical"
    PROCEDURAL = "procedural"
    GOAL = "goal"
    INDIVIDUAL = "individual"


class GoalStatus(str, Enum):
    """Status values for goal memories."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


# Type-specific metadata models


class ExperienceMetadata(BaseModel):
    """Metadata specific to experience memories."""

    context: str | None = Field(default=None, description="What was happening during this experience")
    outcome: str | None = Field(default=None, description="What resulted from this experience")
    lessons_learned: list[str] = Field(default_factory=list, description="Key takeaways from this experience")
    related_entities: list[str] = Field(default_factory=list, description="People, projects, or things involved")


class TechnicalMetadata(BaseModel):
    """Metadata specific to technical memories."""

    category: str | None = Field(default=None, description="Category like 'programming', 'architecture', 'devops'")
    language: str | None = Field(default=None, description="Programming language if applicable")
    code_snippet: str | None = Field(default=None, description="Example code demonstrating this knowledge")
    references: list[str] = Field(default_factory=list, description="URLs, documentation links, or other references")
    version_info: str | None = Field(default=None, description="Version-specific information")
    repo: str | None = Field(default=None, description="Repository name or URL related to this knowledge")
    filename: str | None = Field(default=None, description="Specific file this knowledge relates to")


class ProceduralStep(BaseModel):
    """A single step in a procedure."""

    order: int = Field(..., description="Step number in sequence")
    description: str = Field(..., description="What to do in this step")
    notes: str | None = Field(default=None, description="Additional notes or tips for this step")


class ProceduralMetadata(BaseModel):
    """Metadata specific to procedural memories."""

    steps: list[ProceduralStep] = Field(default_factory=list, description="Ordered steps to complete the procedure")
    prerequisites: list[str] = Field(default_factory=list, description="Required knowledge, tools, or setup")
    estimated_time: str | None = Field(default=None, description="How long this procedure typically takes")
    success_criteria: str | None = Field(default=None, description="How to know the procedure completed successfully")
    common_pitfalls: list[str] = Field(default_factory=list, description="Things that commonly go wrong")


class Milestone(BaseModel):
    """A milestone within a goal."""

    description: str = Field(..., description="What this milestone represents")
    status: GoalStatus = Field(default=GoalStatus.ACTIVE, description="Current status of this milestone")
    completed_at: datetime | None = Field(default=None, description="When this milestone was completed")


class ProgressNote(BaseModel):
    """A progress note for a goal."""

    date: datetime = Field(default_factory=datetime.now, description="When this note was added")
    note: str = Field(..., description="Progress update or note content")


class GoalMetadata(BaseModel):
    """Metadata specific to goal memories."""

    status: GoalStatus = Field(default=GoalStatus.ACTIVE, description="Current status of the goal")
    deadline: datetime | None = Field(default=None, description="Target completion date")
    milestones: list[Milestone] = Field(default_factory=list, description="Milestones to track progress")
    blockers: list[str] = Field(default_factory=list, description="Current obstacles or blockers")
    progress_notes: list[ProgressNote] = Field(default_factory=list, description="Progress updates over time")
    priority: int = Field(default=3, ge=1, le=5, description="Priority level from 1 (low) to 5 (critical)")


class ContactInfo(BaseModel):
    """Contact information for an individual."""

    email: str | None = Field(default=None, description="Email address")
    phone: str | None = Field(default=None, description="Phone number")
    linkedin: str | None = Field(default=None, description="LinkedIn profile URL")
    github: str | None = Field(default=None, description="GitHub profile URL")
    other: dict[str, str] = Field(default_factory=dict, description="Other contact methods")


class InteractionRecord(BaseModel):
    """Record of an interaction with an individual."""

    date: datetime = Field(default_factory=datetime.now, description="When the interaction occurred")
    context: str = Field(..., description="Context or setting of the interaction")
    notes: str | None = Field(default=None, description="Notes about the interaction")


class IndividualMetadata(BaseModel):
    """Metadata specific to individual memories."""

    user_id: UUID | None = Field(default=None, description="ID of the linked user in the system (if they are a system user)")
    name: str = Field(..., description="Person's name")
    relationship: str | None = Field(default=None, description="How you know this person")
    organization: str | None = Field(default=None, description="Company or organization affiliation")
    role: str | None = Field(default=None, description="Their role or title")
    contact_info: ContactInfo = Field(default_factory=ContactInfo, description="Contact information")
    preferences: list[str] = Field(default_factory=list, description="Known preferences or working style")
    interaction_history: list[InteractionRecord] = Field(
        default_factory=list, description="History of interactions"
    )
    last_interaction: datetime | None = Field(default=None, description="When you last interacted")


# Union type for all metadata types
MemoryMetadata = ExperienceMetadata | TechnicalMetadata | ProceduralMetadata | GoalMetadata | IndividualMetadata


class Memory(BaseModel):
    """A complete memory record."""

    id: UUID
    username: str = Field(..., description="Username of the user who created this memory")
    type: MemoryType
    content: str = Field(..., description="Main content of the memory")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    importance: int = Field(default=5, ge=1, le=10, description="Importance rating 1-10")
    related_memory_ids: list[UUID] = Field(default_factory=list, description="IDs of related memories")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Type-specific metadata")
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class TruncatedMemory(BaseModel):
    """A memory with truncated content for search results."""

    id: UUID
    username: str
    type: MemoryType
    content: str = Field(..., description="Truncated content (max 1000 chars)")
    content_truncated: bool = Field(..., description="Whether content was truncated")
    tags: list[str]
    importance: int
    related_memory_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


# Input models for CRUD operations


class CreateMemoryInput(BaseModel):
    """Input for creating a new memory."""

    username: str | None = Field(default=None, description="Username for this memory (defaults to authenticated user)")
    type: MemoryType = Field(..., description="Type of memory to create")
    content: str = Field(..., min_length=1, description="Main content of the memory")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    importance: int = Field(default=5, ge=1, le=10, description="Importance rating 1-10")
    related_memory_ids: list[UUID] = Field(default_factory=list, description="IDs of related memories")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Type-specific metadata")

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: list[str]) -> list[str]:
        """Normalize tags to lowercase and remove duplicates."""
        if v is None:
            return []
        return list(set(tag.lower().strip() for tag in v if tag.strip()))


class UpdateMemoryInput(BaseModel):
    """Input for updating an existing memory."""

    content: str | None = Field(default=None, min_length=1, description="Updated content")
    tags: list[str] | None = Field(default=None, description="Updated tags")
    importance: int | None = Field(default=None, ge=1, le=10, description="Updated importance")
    related_memory_ids: list[UUID] | None = Field(default=None, description="Updated related memory IDs")
    metadata: dict[str, Any] | None = Field(default=None, description="Updated metadata")

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: list[str] | None) -> list[str] | None:
        """Normalize tags to lowercase and remove duplicates."""
        if v is None:
            return None
        return list(set(tag.lower().strip() for tag in v if tag.strip()))


class SearchMemoriesInput(BaseModel):
    """Input for searching memories."""

    query: str | None = Field(default=None, description="Fuzzy search query for content")
    username: str | None = Field(default=None, description="Filter by username")
    type: MemoryType | None = Field(default=None, description="Filter by memory type")
    tags: list[str] | None = Field(default=None, description="Filter by tags (any match)")
    importance_min: int | None = Field(default=None, ge=1, le=10, description="Minimum importance")
    importance_max: int | None = Field(default=None, ge=1, le=10, description="Maximum importance")
    created_after: datetime | None = Field(default=None, description="Filter memories created after this date")
    created_before: datetime | None = Field(default=None, description="Filter memories created before this date")
    memory_ids: list[UUID] | None = Field(default=None, description="Filter by specific memory IDs")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
    limit: int = Field(default=5, ge=1, le=50, description="Maximum results to return")


class SearchResult(BaseModel):
    """Result of a memory search operation."""

    memories: list[TruncatedMemory]
    total_count: int = Field(..., description="Total number of matching memories")
    offset: int = Field(..., description="Current offset")
    limit: int = Field(..., description="Results per page")
    has_more: bool = Field(..., description="Whether more results are available")


class MemorySearchResult(BaseModel):
    """Extended search result with similarity scores."""

    memory: TruncatedMemory
    similarity_score: float | None = Field(default=None, description="Similarity score for fuzzy matches")
