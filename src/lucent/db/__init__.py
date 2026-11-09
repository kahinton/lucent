"""Database module for Lucent.

This module provides the database layer for Lucent, including:
- Connection pool management (pool.py)
- Repository classes for each entity type

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
from lucent.db.access_control import (
    AuthAccessRepository,
    normalize_auth_resource_type,
)
from lucent.db.admin_audit import AdminAuditRepository
from lucent.db.api_key import ApiKeyRepository
from lucent.db.audit import AuditRepository
from lucent.db.auth import AuthRepository
from lucent.db.pool import (
    AuthorizedDatabaseConnection,
    AuthorizedDatabasePool,
    AuthorizedQueryError,
    AuthPrincipal,
    AuthAccessRole,
    AuthTablePolicy,
    get_authorized_pool,
)
from lucent.db.credentials import CredentialRepository
from lucent.db.dashboard import DashboardRepository
from lucent.db.definitions import DefinitionRepository
from lucent.db.entitlements import EntitlementRepository
from lucent.db.groups import GroupRepository
from lucent.db.identity import (
    InvitationRepository,
    SamlReplayRepository,
    ScimTokenRepository,
    SsoConnectionRepository,
    SsoLoginStateRepository,
)
from lucent.db.integrations import IntegrationRepository
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.memory import (
    ContentShrinkError,
    DuplicateTechnicalMemoryError,
    MemoryRepository,
    VersionConflictError,
    get_first_memory_owner,
    get_authorized_memories_pool,
)
from lucent.db.models import ModelRepository
from lucent.db.organization import OrganizationRepository
from lucent.db.pool import close_db, get_pool, init_db
from lucent.db.projects import ProjectRepository, get_authorized_projects_pool
from lucent.db.reviews import ReviewRepository
from lucent.db.runtime_settings import RuntimeSettingsRepository
from lucent.db.secrets import SecretMigrationRepository, SecretRepository
from lucent.db.token_usage import TokenUsageRepository
from lucent.db.token_quota import TokenQuotaRepository
from lucent.db.tool_audit import ToolAuditRepository
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
    "TokenQuotaRepository",
    "ContentShrinkError",
    "DuplicateTechnicalMemoryError",
    "VersionConflictError",
    "DefinitionRepository",
    "EntitlementRepository",
    "GroupRepository",
    "InvitationRepository",
    "SsoConnectionRepository",
    "SsoLoginStateRepository",
    "SamlReplayRepository",
    "ScimTokenRepository",
    "IntegrationRepository",
    "LLMSessionRepository",
    "ProjectRepository",
    "UserRepository",
    "ApiKeyRepository",
    "OrganizationRepository",
    "AuthRepository",
    "AuthorizedDatabaseConnection",
    "AuthorizedDatabasePool",
    "AuthorizedQueryError",
    "AuthPrincipal",
    "AuthTablePolicy",
    "AuthAccessRole",
    "AuthAccessRepository",
    "normalize_auth_resource_type",
    "get_authorized_pool",
    "get_authorized_projects_pool",
    "get_authorized_memories_pool",
    "get_first_memory_owner",
    "AuditRepository",
    "AdminAuditRepository",
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
]
