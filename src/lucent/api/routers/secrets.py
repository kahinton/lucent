"""API router for secret storage.

Provides CRUD endpoints for managing secrets with ownership-based access control.
Secret values are NEVER included in list or create responses.
Every secret access is audit-logged.
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from lucent.access_control import AccessControlService
from lucent.api.deps import AdminUser, AuthenticatedUser
from lucent.db import GroupRepository, get_pool
from lucent.db.audit import AuditRepository
from lucent.db.definitions import DefinitionRepository
from lucent.db.integrations_repositories import IntegrationRepo
from lucent.db.sandbox import SandboxRepository
from lucent.db.sandbox_template import SandboxTemplateRepository
from lucent.integrations.encryption import BackendCredentialEncryptor, EncryptionError
from lucent.rbac import Role
from lucent.secrets import SecretRegistry, SecretScope
from lucent.secrets.utils import (
    CREDENTIAL_REF_PREFIX,
    SECRET_REF_PREFIX,
    is_sensitive_env_key,
)

router = APIRouter(prefix="/secrets", tags=["secrets"])

# Sentinel UUID for audit log entries that aren't tied to a memory
_SENTINEL_UUID = UUID("00000000-0000-4000-4000-000000000000")

# Audit action types for secret operations
SECRET_CREATE = "secret_create"
SECRET_READ = "secret_read"
SECRET_DELETE = "secret_delete"


# ── Request / Response Models ─────────────────────────────────────────────


class SecretCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=256, description="Secret key name")
    value: str = Field(..., min_length=1, description="Secret value (never returned)")
    owner_group_id: str | None = Field(
        default=None, description="Group owner (defaults to current user)"
    )


class SecretKeyResponse(BaseModel):
    key: str
    owner_user_id: str | None = None
    owner_group_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SecretListResponse(BaseModel):
    keys: list[SecretKeyResponse]


class SecretValueResponse(BaseModel):
    key: str
    value: str


class MigrationResult(BaseModel):
    migrated_mcp_env_vars: int = 0
    migrated_sandbox_env_vars: int = 0
    migrated_managed_tool_env_vars: int = 0
    migrated_integration_values: int = 0
    redacted_sandbox_runtime_configs: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────


def _user_scope(user: AuthenticatedUser, owner_group_id: str | None = None) -> SecretScope:
    """Build a SecretScope for the current user or a specified group."""
    if owner_group_id:
        return SecretScope(
            organization_id=str(user.organization_id),
            owner_group_id=owner_group_id,
        )
    return SecretScope(
        organization_id=str(user.organization_id),
        owner_user_id=str(user.id),
    )


async def _audit_secret(
    user: AuthenticatedUser,
    action: str,
    key: str,
    *,
    context: dict | None = None,
) -> None:
    """Log a secret operation to the audit trail."""
    pool = await get_pool()
    audit = AuditRepository(pool)
    await audit.log(
        memory_id=_SENTINEL_UUID,
        action_type=action,
        user_id=user.id,
        organization_id=user.organization_id,
        context={"secret_key": key, **(context or {})},
        notes=f"Secret {action}: {key}",
    )


async def _check_secret_access(
    user: AuthenticatedUser,
    key: str,
    scope: SecretScope,
    *,
    require_modify: bool = False,
) -> None:
    """Verify the user can access a secret via ACL. Raises 404 or 403."""
    pool = await get_pool()
    provider = SecretRegistry.get()

    # Get the secret's DB id for ACL check
    if not hasattr(provider, "get_secret_id"):
        return  # Non-builtin providers skip ACL
    secret_id = await provider.get_secret_id(key, scope)
    if secret_id is None:
        raise HTTPException(status_code=404, detail="Secret not found")

    acl = AccessControlService(pool)
    if require_modify:
        allowed = await acl.can_modify(
            str(user.id), "secret", secret_id, str(user.organization_id)
        )
    else:
        allowed = await acl.can_access(
            str(user.id), "secret", secret_id, str(user.organization_id)
        )
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")


async def _check_group_secret_scope(
    user: AuthenticatedUser,
    owner_group_id: str | None,
    *,
    require_modify: bool,
) -> None:
    if not owner_group_id:
        return
    pool = await get_pool()
    repo = GroupRepository(pool)
    group = await repo.get_group(owner_group_id, str(user.organization_id))
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if user.role >= Role.ADMIN:
        return
    if require_modify:
        allowed = await repo.is_group_admin(str(user.id), owner_group_id)
    else:
        allowed = await repo.is_member(str(user.id), owner_group_id)
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")


def _scope_from_row(row: dict) -> SecretScope:
    return SecretScope(
        organization_id=str(row["organization_id"]),
        owner_user_id=str(row["owner_user_id"]) if row.get("owner_user_id") else None,
        owner_group_id=str(row["owner_group_id"]) if row.get("owner_group_id") else None,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("", status_code=201, response_model=SecretKeyResponse)
async def create_secret(body: SecretCreate, user: AuthenticatedUser):
    """Store a secret. Returns key name only — never the value."""
    await _check_group_secret_scope(user, body.owner_group_id, require_modify=True)
    provider = SecretRegistry.get()
    scope = _user_scope(user, body.owner_group_id)
    await provider.set(body.key, body.value, scope)
    await _audit_secret(user, SECRET_CREATE, body.key)
    return SecretKeyResponse(
        key=body.key,
        owner_user_id=scope.owner_user_id,
        owner_group_id=scope.owner_group_id,
    )


@router.get("", response_model=SecretListResponse)
async def list_secrets(user: AuthenticatedUser, owner_group_id: str | None = None):
    """List secret key names (no values) for the current user or group."""
    await _check_group_secret_scope(user, owner_group_id, require_modify=False)
    provider = SecretRegistry.get()
    scope = _user_scope(user, owner_group_id)
    keys = await provider.list_keys(scope)
    return SecretListResponse(
        keys=[
            SecretKeyResponse(
                key=k,
                owner_user_id=scope.owner_user_id,
                owner_group_id=scope.owner_group_id,
            )
            for k in keys
        ]
    )


@router.get("/{key}", response_model=SecretValueResponse)
async def get_secret(key: str, user: AuthenticatedUser, owner_group_id: str | None = None):
    """Get a secret value. Requires explicit authorization via web session only."""
    if user.auth_method == "api_key":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Secret values cannot be accessed via API key. Use a web session.",
        )
    scope = _user_scope(user, owner_group_id)
    await _check_secret_access(user, key, scope)
    provider = SecretRegistry.get()
    value = await provider.get(key, scope)
    if value is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    await _audit_secret(user, SECRET_READ, key)
    return SecretValueResponse(key=key, value=value)


@router.delete("/{key}", status_code=200)
async def delete_secret(key: str, user: AuthenticatedUser, owner_group_id: str | None = None):
    """Delete a secret."""
    scope = _user_scope(user, owner_group_id)
    await _check_secret_access(user, key, scope, require_modify=True)
    provider = SecretRegistry.get()
    deleted = await provider.delete(key, scope)
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
    await _audit_secret(user, SECRET_DELETE, key)
    return {"deleted": True, "key": key}


@router.post("/migrate-plaintext-configs", response_model=MigrationResult)
async def migrate_plaintext_configs(user: AdminUser):
    """Migrate plaintext env/config credentials to secret:// references."""
    pool = await get_pool()
    provider = SecretRegistry.get()
    migrated_mcp = 0
    migrated_sandbox = 0
    migrated_managed_tools = 0
    migrated_integrations = 0
    redacted_sandbox_runtime_configs = 0

    definition_repo = DefinitionRepository(pool)
    sandbox_repo = SandboxRepository(pool)
    template_repo = SandboxTemplateRepository(pool)
    integration_repo = IntegrationRepo(pool)

    mcp_rows = await definition_repo.list_mcp_server_configs_for_credential_migration(
        user.organization_id
    )
    for row in mcp_rows:
        env_vars = row["env_vars"] or {}
        if isinstance(env_vars, str):
            env_vars = json.loads(env_vars)
        updated = dict(env_vars)
        changed = False
        scope = _scope_from_row(dict(row))
        for key, value in env_vars.items():
            if not isinstance(value, str) or value.startswith(
                (SECRET_REF_PREFIX, CREDENTIAL_REF_PREFIX)
            ):
                continue
            if not is_sensitive_env_key(key):
                continue
            secret_name = f"mcp.{row['id']}.{key.lower()}"
            await provider.set(secret_name, value, scope)
            updated[key] = f"{SECRET_REF_PREFIX}{secret_name}"
            changed = True
            migrated_mcp += 1
        if changed:
            await definition_repo.update_mcp_server_config_env_vars(
                row["id"], row["organization_id"], updated
            )

    sandbox_rows = await template_repo.list_for_credential_migration(
        user.organization_id
    )
    for row in sandbox_rows:
        env_vars = row["env_vars"] or {}
        if isinstance(env_vars, str):
            env_vars = json.loads(env_vars)
        updated = dict(env_vars)
        changed = False
        scope = _scope_from_row(dict(row))
        for key, value in env_vars.items():
            if not isinstance(value, str) or value.startswith(
                (SECRET_REF_PREFIX, CREDENTIAL_REF_PREFIX)
            ):
                continue
            if not is_sensitive_env_key(key):
                continue
            secret_name = f"sandbox.{row['id']}.{key.lower()}"
            await provider.set(secret_name, value, scope)
            updated[key] = f"{SECRET_REF_PREFIX}{secret_name}"
            changed = True
            migrated_sandbox += 1
        if changed:
            await template_repo.update_env_vars(
                row["id"], row["organization_id"], updated
            )

    managed_tool_rows = (
        await definition_repo.list_managed_tools_for_credential_migration(
            user.organization_id
        )
    )
    for row in managed_tool_rows:
        env_vars = row["env_vars"] or {}
        if isinstance(env_vars, str):
            env_vars = json.loads(env_vars)
        updated = dict(env_vars)
        changed = False
        scope = _scope_from_row(dict(row))
        for key, value in env_vars.items():
            if not isinstance(value, str) or value.startswith(
                (SECRET_REF_PREFIX, CREDENTIAL_REF_PREFIX)
            ):
                continue
            if not is_sensitive_env_key(key):
                continue
            secret_name = f"managed-tool.{row['id']}.{key.lower()}"
            await provider.set(secret_name, value, scope)
            updated[key] = f"{SECRET_REF_PREFIX}{secret_name}"
            changed = True
            migrated_managed_tools += 1
        if changed:
            await definition_repo.update_managed_tool_env_vars(
                row["id"], row["organization_id"], updated
            )

    redacted_sandbox_runtime_configs = await sandbox_repo.redact_runtime_configs(
        user.organization_id
    )

    integration_rows = await integration_repo.list_by_org(user.organization_id)
    if integration_rows:
        try:
            encryptor = BackendCredentialEncryptor()
        except EncryptionError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Integration encryption is not configured: {exc}",
            ) from exc

        for row in integration_rows:
            config = encryptor.decrypt(row["encrypted_config"])
            updated_cfg = dict(config)
            changed = False
            scope = SecretScope(
                organization_id=str(row["organization_id"]),
                owner_user_id=str(row["created_by"]),
            )
            for key, value in config.items():
                if not isinstance(value, str) or value.startswith(
                    (SECRET_REF_PREFIX, CREDENTIAL_REF_PREFIX)
                ):
                    continue
                if not is_sensitive_env_key(key):
                    continue
                secret_name = f"integration.{row['id']}.{key.lower()}"
                await provider.set(secret_name, value, scope)
                updated_cfg[key] = f"{SECRET_REF_PREFIX}{secret_name}"
                changed = True
                migrated_integrations += 1
            if changed:
                await integration_repo.update_encrypted_config(
                    row["id"],
                    row["organization_id"],
                    encrypted_config=encryptor.encrypt(updated_cfg),
                    updated_by=row["created_by"],
                )

    return MigrationResult(
        migrated_mcp_env_vars=migrated_mcp,
        migrated_sandbox_env_vars=migrated_sandbox,
        migrated_managed_tool_env_vars=migrated_managed_tools,
        migrated_integration_values=migrated_integrations,
        redacted_sandbox_runtime_configs=redacted_sandbox_runtime_configs,
    )
