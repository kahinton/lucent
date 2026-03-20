"""API router for secret storage.

Provides CRUD endpoints for managing secrets with ownership-based access control.
Secret values are NEVER included in list or create responses.
Every secret access is audit-logged.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lucent.access_control import AccessControlService
from lucent.api.deps import AdminUser, AuthenticatedUser
from lucent.db import get_pool
from lucent.db.audit import AuditRepository
from lucent.integrations.encryption import EncryptionError, FernetEncryptor
from lucent.secrets import SecretRegistry, SecretScope
from lucent.secrets.utils import SECRET_REF_PREFIX

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
    migrated_integration_values: int = 0


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


def _is_sensitive_env_key(key: str) -> bool:
    token = key.lower()
    sensitive_tokens = (
        "token",
        "secret",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "client_secret",
        "private_key",
        "access_key",
        "auth",
    )
    return any(t in token for t in sensitive_tokens)


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
    """Get a secret value. Requires explicit authorization."""
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
    migrated_integrations = 0

    async with pool.acquire() as conn:
        mcp_rows = await conn.fetch(
            """
            SELECT id, organization_id, owner_user_id, owner_group_id, env_vars
            FROM mcp_server_configs
            WHERE organization_id = $1
            """,
            user.organization_id,
        )
        for row in mcp_rows:
            env_vars = row["env_vars"] or {}
            if isinstance(env_vars, str):
                import json

                env_vars = json.loads(env_vars)
            updated = dict(env_vars)
            changed = False
            scope = _scope_from_row(dict(row))
            for key, value in env_vars.items():
                if not isinstance(value, str) or value.startswith(SECRET_REF_PREFIX):
                    continue
                if not _is_sensitive_env_key(key):
                    continue
                secret_name = f"mcp.{row['id']}.{key.lower()}"
                await provider.set(secret_name, value, scope)
                updated[key] = f"{SECRET_REF_PREFIX}{secret_name}"
                changed = True
                migrated_mcp += 1
            if changed:
                await conn.execute(
                    "UPDATE mcp_server_configs SET env_vars = $2::jsonb, updated_at = NOW() WHERE id = $1",
                    row["id"],
                    __import__("json").dumps(updated),
                )

        sandbox_rows = await conn.fetch(
            """
            SELECT id, organization_id, owner_user_id, owner_group_id, env_vars
            FROM sandbox_templates
            WHERE organization_id = $1
            """,
            user.organization_id,
        )
        for row in sandbox_rows:
            env_vars = row["env_vars"] or {}
            if isinstance(env_vars, str):
                import json

                env_vars = json.loads(env_vars)
            updated = dict(env_vars)
            changed = False
            scope = _scope_from_row(dict(row))
            for key, value in env_vars.items():
                if not isinstance(value, str) or value.startswith(SECRET_REF_PREFIX):
                    continue
                if not _is_sensitive_env_key(key):
                    continue
                secret_name = f"sandbox.{row['id']}.{key.lower()}"
                await provider.set(secret_name, value, scope)
                updated[key] = f"{SECRET_REF_PREFIX}{secret_name}"
                changed = True
                migrated_sandbox += 1
            if changed:
                await conn.execute(
                    "UPDATE sandbox_templates SET env_vars = $2::jsonb, updated_at = NOW() WHERE id = $1",
                    row["id"],
                    __import__("json").dumps(updated),
                )

        integration_rows = await conn.fetch(
            """
            SELECT id, organization_id, created_by, encrypted_config
            FROM integrations
            WHERE organization_id = $1
            """,
            user.organization_id,
        )
        if integration_rows:
            try:
                encryptor = FernetEncryptor()
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
                    if not isinstance(value, str) or value.startswith(SECRET_REF_PREFIX):
                        continue
                    if not _is_sensitive_env_key(key):
                        continue
                    secret_name = f"integration.{row['id']}.{key.lower()}"
                    await provider.set(secret_name, value, scope)
                    updated_cfg[key] = f"{SECRET_REF_PREFIX}{secret_name}"
                    changed = True
                    migrated_integrations += 1
                if changed:
                    await conn.execute(
                        "UPDATE integrations SET encrypted_config = $2, updated_at = NOW() WHERE id = $1",
                        row["id"],
                        encryptor.encrypt(updated_cfg),
                    )

    return MigrationResult(
        migrated_mcp_env_vars=migrated_mcp,
        migrated_sandbox_env_vars=migrated_sandbox,
        migrated_integration_values=migrated_integrations,
    )
