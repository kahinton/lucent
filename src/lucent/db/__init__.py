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
from lucent.db.pool import get_pool, init_db, close_db

# Repositories
from lucent.db.memory import MemoryRepository, VersionConflictError
from lucent.db.user import UserRepository
from lucent.db.api_key import ApiKeyRepository
from lucent.db.organization import OrganizationRepository
from lucent.db.audit import AuditRepository
from lucent.db.access import AccessRepository

# TypedDict definitions for repository return values
from lucent.db.types import (
    MemoryRecord,
    MemorySearchRecord,
    MemorySearchResult,
    TagCount,
    TagSuggestion,
    UserRecord,
    ApiKeyRecord,
    ApiKeyVerifyRecord,
    OrganizationRecord,
    OrganizationListResult,
    AuditLogRecord,
    AuditLogResult,
    AccessLogRecord,
    AccessLogResult,
    MostAccessedRecord,
)

__all__ = [
    # Pool management
    "get_pool",
    "init_db", 
    "close_db",
    # Repositories
    "MemoryRepository",
    "VersionConflictError",
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
