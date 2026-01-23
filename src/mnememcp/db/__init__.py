"""Database module for mnemeMCP.

This module provides the database layer for mnemeMCP, including:
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
from mnememcp.db.pool import get_pool, init_db, close_db

# Repositories
from mnememcp.db.memory import MemoryRepository
from mnememcp.db.user import UserRepository
from mnememcp.db.api_key import ApiKeyRepository
from mnememcp.db.organization import OrganizationRepository
from mnememcp.db.audit import AuditRepository
from mnememcp.db.access import AccessRepository

# TypedDict definitions for repository return values
from mnememcp.db.types import (
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
