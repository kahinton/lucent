"""Admin model management API endpoints."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from lucent.api.deps import AuthenticatedUser
from lucent.db import get_pool
from lucent.db.models import ModelRepository
from lucent.llm.model_engine_validation import normalize_engine, validate_engine_override

router = APIRouter()


class ModelUpsertRequest(BaseModel):
    model_id: str = Field(min_length=1)
    provider: str
    name: str
    category: str = "general"
    api_model_id: str = ""
    context_window: int = 0
    supports_tools: bool = True
    supports_vision: bool = False
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    is_enabled: bool = True
    engine: Literal["copilot", "langchain"] | None = None

    @field_validator("engine", mode="before")
    @classmethod
    def _normalize_engine(cls, value: Any) -> str | None:
        return normalize_engine(value)


class ModelPatchRequest(BaseModel):
    provider: str | None = None
    name: str | None = None
    category: str | None = None
    api_model_id: str | None = None
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    notes: str | None = None
    tags: list[str] | None = None
    is_enabled: bool | None = None
    engine: Literal["copilot", "langchain"] | None = None

    @field_validator("engine", mode="before")
    @classmethod
    def _normalize_engine(cls, value: Any) -> str | None:
        return normalize_engine(value)


def _to_response(model: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    payload = dict(model)
    if warnings:
        payload["warnings"] = warnings
    return payload


@router.get("")
async def list_models(user: AuthenticatedUser, limit: int = 100, offset: int = 0):
    user.require_scope("read")
    pool = await get_pool()
    repo = ModelRepository(pool)
    org_id = str(user.organization_id) if user.organization_id else None
    return await repo.list_models(limit=limit, offset=offset, org_id=org_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_model(body: ModelUpsertRequest, user: AuthenticatedUser):
    user.require_scope("write")
    pool = await get_pool()
    repo = ModelRepository(pool)
    existing = await repo.get_model(body.model_id)
    if existing:
        raise HTTPException(status_code=409, detail="Model ID already exists")
    try:
        warnings = validate_engine_override(body.provider, body.engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    model = await repo.create_model(
        model_id=body.model_id,
        provider=body.provider,
        name=body.name,
        category=body.category,
        api_model_id=body.api_model_id or body.model_id,
        context_window=body.context_window,
        supports_tools=body.supports_tools,
        supports_vision=body.supports_vision,
        notes=body.notes,
        tags=body.tags,
        is_enabled=body.is_enabled,
        org_id=str(user.organization_id) if user.organization_id else None,
        engine=body.engine,
    )
    return _to_response(model, warnings=warnings)


@router.put("/{model_id:path}")
async def update_model(model_id: str, body: ModelPatchRequest, user: AuthenticatedUser):
    user.require_scope("write")
    pool = await get_pool()
    repo = ModelRepository(pool)
    existing = await repo.get_model(model_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Model not found")
    provider = body.provider if "provider" in body.model_fields_set else existing["provider"]
    engine = body.engine if "engine" in body.model_fields_set else existing.get("engine")
    try:
        warnings = validate_engine_override(provider, engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    updates = body.model_dump(exclude_unset=True)
    updated = await repo.update_model(model_id, **updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Model not found")
    return _to_response(updated, warnings=warnings)
