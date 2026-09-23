"""Database module for Lucent.

This module provides the database layer for Lucent, including:
- Connection pool management (pool.py)
- Repository classes for each entity type
- TypedDict definitions for repository return values

Repositories:
- MemoryRepository: Memory CRUD and search operations
- UserRepository: User management with individual memory auto-creation
- ApiKeyRepository: API key authentication
- OrganizationRepository: Organization management
- AuditRepository: Audit log for memory changes
- AccessRepository: Memory access tracking and analytics
"""

# Pool management
from lucent.db.access import AccessRepository
from lucent.db.admin_audit import AdminAuditRepository
from lucent.db.api_key import ApiKeyRepository
from lucent.db.audit import AuditRepository
from lucent.db.auth import AuthRepository
from lucent.db.bootstrap import BootstrapRepository
from lucent.db.credentials import CredentialRepository

# Repositories
from lucent.db.definitions import DefinitionRepository
from lucent.db.groups import GroupRepository
from lucent.db.dashboard import DashboardRepository
from lucent.db.integrations import IntegrationRepository
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.memory import (
    DuplicateTechnicalMemoryError,
    MemoryRepository,
    VersionConflictError,
)
from lucent.db.models import ModelRepository
from lucent.db.organization import OrganizationRepository
from lucent.db.pool import close_db, get_pool, init_db
from lucent.db.projects import ProjectRepository
from lucent.db.reviews import ReviewRepository
from lucent.db.runtime_settings import RuntimeSettingsRepository
from lucent.db.secrets import SecretRepository
from lucent.db.secrets import SecretMigrationRepository
from lucent.db.token_usage import TokenUsageRepository
from lucent.db.tool_audit import ToolAuditRepository

# TypedDict definitions for repository return values
from lucent.db.types import (
    AccessFrequencyRecord,
    AccessLogRecord,
    AccessLogResult,
    ApiKeyRecord,
    ApiKeyVerifyRecord,
    AuditLogRecord,
    AuditLogResult,
    MemoryRecord,
    MemorySearchRecord,
    MemorySearchResult,
    MemoryShadowScoreRecord,
    MostAccessedRecord,
    OrganizationListResult,
    OrganizationRecord,
    TagCount,
    TagSuggestion,
    UserRecord,
)
from lucent.db.user import UserRepository
from lucent.db.user_interactions import UserInteractionRepository

__all__ = [
    # Pool management
    "get_pool",
    "init_db",
    "close_db",
    # Repositories
    "MemoryRepository",
    "TokenUsageRepository",
    "DuplicateTechnicalMemoryError",
    "VersionConflictError",
    "DefinitionRepository",
    "GroupRepository",
    "IntegrationRepository",
    "LLMSessionRepository",
    "ProjectRepository",
    "UserRepository",
    "ApiKeyRepository",
    "OrganizationRepository",
    "AuthRepository",
    "AuditRepository",
    "AdminAuditRepository",
    "BootstrapRepository",
    "DashboardRepository",
    "CredentialRepository",
    "AccessRepository",
    "ModelRepository",
    "ReviewRepository",
    "RuntimeSettingsRepository",
    "ToolAuditRepository",
    "SecretRepository",
    "SecretMigrationRepository",
    "UserInteractionRepository",
    # TypedDict definitions
    "MemoryRecord",
    "MemoryShadowScoreRecord",
    "MemorySearchRecord",
    "MemorySearchResult",
    "TagCount",
    "TagSuggestion",
    "UserRecord",
    "ApiKeyRecord",
    "ApiKeyVerifyRecord",
    "OrganizationRecord",
    "OrganizationListResult",
    "AuditLogRecord",
    "AuditLogResult",
    "AccessLogRecord",
    "AccessLogResult",
    "AccessFrequencyRecord",
    "MostAccessedRecord",
]
