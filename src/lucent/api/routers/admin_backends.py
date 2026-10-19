"""Admin engine-backend CRUD API.

Backends bind an engine to a provider family with non-secret config and
secret references (see migration 136). Secret values are written to the secret
provider; only their key names appear in ``backends.secret_refs``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from lucent.api.deps import AuthenticatedUser
from lucent.db import get_pool
from lucent.db.backends import BackendRepository
from lucent.llm.engine_credentials import engine_config_schema, secret_key_for
from lucent.llm.engine_registry import UnknownEngineError, engine_spec

router = APIRouter()


def _require_admin_user(user: AuthenticatedUser) -> None:
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required")


def _require_org(user: AuthenticatedUser) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=400, detail="An organization is required")
    return str(user.organization_id)


def _validate_engine(engine: str) -> str:
    try:
        return engine_spec(engine.strip().lower()).name
    except UnknownEngineError:
        raise HTTPException(status_code=400, detail=f"Unknown engine: {engine!r}")


def _secret_field_names(engine: str) -> set[str]:
    return {f.key for f in engine_config_schema(engine) if f.is_secret}


class BackendCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    engine: str
    provider: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    is_enabled: bool = True
    model_config = ConfigDict(extra="forbid")


class BackendUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    config: dict[str, Any] | None = None
    secret_refs: dict[str, str] | None = None
    is_enabled: bool | None = None
    model_config = ConfigDict(extra="forbid")


class BackendSecretRequest(BaseModel):
    field: str
    value: str
    model_config = ConfigDict(extra="forbid")


async def _audit(request: Request, user: AuthenticatedUser, pool, **kwargs) -> None:
    try:
        from lucent.db import admin_audit as audit_actions
        from lucent.db.admin_audit import AdminAuditRepository

        kwargs.setdefault("action", audit_actions.SETTING_UPDATE)
        await AdminAuditRepository(pool).log_for_user(user, request, **kwargs)
    except Exception:
        # Auditing must never break the write itself.
        pass


@router.get("")
async def list_backends(
    user: AuthenticatedUser,
    engine: str | None = None,
    provider: str | None = None,
):
    """List engine backends for the caller's organization plus shared rows."""
    user.require_scope("read")
    _require_admin_user(user)
    org_id = _require_org(user)
    pool = await get_pool()
    items = await BackendRepository(pool).list(org_id=org_id, engine=engine, provider=provider)
    return {"items": items, "total_count": len(items)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_backend(body: BackendCreateRequest, request: Request, user: AuthenticatedUser):
    user.require_scope("write")
    _require_admin_user(user)
    org_id = _require_org(user)
    engine = _validate_engine(body.engine)
    provider = body.provider.strip().lower()
    from lucent.llm.engine_injection import (
        compatible_provider_options,
        provider_allowed_for_engine,
    )

    if not provider_allowed_for_engine(engine, provider):
        allowed = ", ".join(
            option["name"] for option in compatible_provider_options(engine)
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Engine '{engine}' cannot use provider '{provider}'. "
                f"Supported providers: {allowed or 'none'}."
            ),
        )
    pool = await get_pool()
    repo = BackendRepository(pool)
    existing = await repo.list(org_id=org_id, engine=engine, provider=body.provider)
    if any(row["name"] == body.name for row in existing):
        raise HTTPException(status_code=409, detail="A backend with that name already exists")
    backend = await repo.create(
        name=body.name,
        engine=engine,
        provider=provider,
        config=body.config,
        secret_refs=body.secret_refs,
        is_enabled=body.is_enabled,
        org_id=org_id,
    )
    await _audit(
        request,
        user,
        pool,
        entity_type="engine_backend",
        entity_id=backend["id"],
        entity_label=backend["name"],
        changed_fields=["created"],
        new_values={"engine": engine, "provider": backend["provider"]},
        notes="engine backend created",
    )
    return backend


@router.patch("/{backend_id}")
async def update_backend(
    backend_id: str,
    body: BackendUpdateRequest,
    request: Request,
    user: AuthenticatedUser,
):
    user.require_scope("write")
    _require_admin_user(user)
    org_id = _require_org(user)
    pool = await get_pool()
    repo = BackendRepository(pool)
    updated = await repo.update(
        backend_id,
        name=body.name,
        config=body.config,
        secret_refs=body.secret_refs,
        is_enabled=body.is_enabled,
        org_id=org_id,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Backend not found")
    await _audit(
        request,
        user,
        pool,
        entity_type="engine_backend",
        entity_id=backend_id,
        entity_label=updated["name"],
        changed_fields=[k for k, v in body.model_dump().items() if v is not None],
        notes="engine backend updated",
    )
    return updated


@router.delete("/{backend_id}")
async def delete_backend(backend_id: str, request: Request, user: AuthenticatedUser):
    user.require_scope("write")
    _require_admin_user(user)
    org_id = _require_org(user)
    pool = await get_pool()
    repo = BackendRepository(pool)
    existing = await repo.get(backend_id, org_id=org_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Backend not found")
    deleted = await repo.delete(backend_id, org_id=org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Backend not found")
    await _audit(
        request,
        user,
        pool,
        entity_type="engine_backend",
        entity_id=backend_id,
        entity_label=existing["name"],
        changed_fields=["deleted"],
        notes="engine backend deleted",
    )
    return {"deleted": True, "id": backend_id}


@router.post("/{backend_id}/secrets")
async def set_backend_secret(
    backend_id: str,
    body: BackendSecretRequest,
    request: Request,
    user: AuthenticatedUser,
):
    """Store one secret field for a backend in the secret provider."""
    user.require_scope("write")
    _require_admin_user(user)
    org_id = _require_org(user)
    pool = await get_pool()
    repo = BackendRepository(pool)
    backend = await repo.get(backend_id, org_id=org_id)
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")
    secret_fields = _secret_field_names(backend["engine"])
    if body.field not in secret_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Field {body.field!r} is not a secret field of engine {backend['engine']!r}",
        )
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Secret value is required")
    key = secret_key_for(backend["engine"], backend["name"], body.field)

    from lucent.model_discovery import model_provider_secret_scope
    from lucent.secrets import SecretRegistry

    await SecretRegistry.get().set(key, value, model_provider_secret_scope(org_id))
    secret_refs = dict(backend.get("secret_refs") or {})
    secret_refs[body.field] = key
    await repo.update(backend_id, secret_refs=secret_refs, org_id=org_id)

    await _audit(
        request,
        user,
        pool,
        entity_type="engine_backend",
        entity_id=backend_id,
        entity_label=backend["name"],
        changed_fields=[f"secret_refs.{body.field}"],
        new_values={"field": body.field, "secret_key": key, "configured": True},
        notes="engine backend secret updated",
    )
    return {"configured": True, "field": body.field}


@router.post("/{backend_id}/secrets/clear")
async def clear_backend_secret(
    backend_id: str,
    body: BackendSecretRequest,
    request: Request,
    user: AuthenticatedUser,
):
    """Remove a stored secret field so an env fallback can apply."""
    user.require_scope("write")
    _require_admin_user(user)
    org_id = _require_org(user)
    pool = await get_pool()
    repo = BackendRepository(pool)
    backend = await repo.get(backend_id, org_id=org_id)
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")
    secret_refs = dict(backend.get("secret_refs") or {})
    key = secret_refs.pop(body.field, None)
    if key:
        from lucent.model_discovery import model_provider_secret_scope
        from lucent.secrets import SecretRegistry

        try:
            await SecretRegistry.get().delete(key, model_provider_secret_scope(org_id))
        except Exception:
            pass
        await repo.update(backend_id, secret_refs=secret_refs, org_id=org_id)
    await _audit(
        request,
        user,
        pool,
        entity_type="engine_backend",
        entity_id=backend_id,
        entity_label=backend["name"],
        changed_fields=[f"secret_refs.{body.field}"],
        new_values={"field": body.field, "configured": False},
        notes="engine backend secret cleared",
    )
    return {"configured": False, "field": body.field}
