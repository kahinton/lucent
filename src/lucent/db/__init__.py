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
from lucent.db.api_key import ApiKeyRepository
from lucent.db.audit import AuditRepository

# Repositories
from lucent.db.definitions import DefinitionRepository
from lucent.db.integrations import IntegrationRepository
from lucent.db.memory import MemoryRepository, VersionConflictError
from lucent.db.organization import OrganizationRepository
from lucent.db.pool import close_db, get_pool, init_db

# TypedDict definitions for repository return values
from lucent.db.types import (
    AccessLogRecord,
    AccessLogResult,
    ApiKeyRecord,
    ApiKeyVerifyRecord,
    AuditLogRecord,
    AuditLogResult,
    MemoryRecord,
    MemorySearchRecord,
    MemorySearchResult,
    MostAccessedRecord,
    OrganizationListResult,
    OrganizationRecord,
    TagCount,
    TagSuggestion,
    UserRecord,
)
from lucent.db.user import UserRepository

__all__ = [
    # Pool management
    "get_pool",
    "init_db",
    "close_db",
    # Repositories
    "MemoryRepository",
    "VersionConflictError",
    "DefinitionRepository",
    "IntegrationRepository",
    "UserRepository",
    "ApiKeyRepository",
    "OrganizationRepository",
    "AuditRepository",
    "AccessRepository",
    # TypedDict definitions
    "MemoryRecord",
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
    "MostAccessedRecord",
]
