"""API endpoints for enterprise credential management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from lucent.api.deps import AuthenticatedUser
from lucent.db.pool import get_pool
from lucent.integrations.credential_models import (
    CredentialCreate,
    CredentialListResponse,
    CredentialRefreshResponse,
    CredentialResponse,
    CredentialUpdate,
    OAuthCallbackRequest,
    OAuthStartRequest,
    OAuthStartResponse,
)
from lucent.integrations.credential_repository import CredentialRepository
from lucent.integrations.credential_service import CredentialService

router = APIRouter(prefix="/credentials", tags=["Credentials"])


async def _service() -> CredentialService:
    pool = await get_pool()
    repo = CredentialRepository(pool)
    return CredentialService(repo)


def _to_response(row: dict) -> CredentialResponse:
    return CredentialResponse(
        id=row["id"],
        organization_id=row["organization_id"],
        integration_type=row["integration_type"],
        credential_kind=row["credential_kind"],
        scope_type=row["scope_type"],
        owner_user_id=row.get("owner_user_id"),
        owner_agent_id=row.get("owner_agent_id"),
        display_name=row["display_name"],
        scopes=row.get("scopes") or [],
        status=row["status"],
        access_token_expires_at=row.get("access_token_expires_at"),
        refresh_token_expires_at=row.get("refresh_token_expires_at"),
        last_refreshed_at=row.get("last_refreshed_at"),
        refresh_token_version=row.get("refresh_token_version", 1),
        token_rotated_at=row.get("token_rotated_at"),
        created_by=row["created_by"],
        updated_by=row.get("updated_by"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("", response_model=CredentialResponse, status_code=201)
async def create_credential(body: CredentialCreate, user: AuthenticatedUser) -> CredentialResponse:
    svc = await _service()
    row = await svc.create_credential(body, user)
    return _to_response(row)


@router.get("", response_model=CredentialListResponse)
async def list_credentials(
    user: AuthenticatedUser,
    scope_type: str | None = None,
    owner_user_id: UUID | None = None,
    owner_agent_id: UUID | None = None,
    integration_type: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> CredentialListResponse:
    svc = await _service()
    rows = await svc.list_credentials(
        user=user,
        scope_type=scope_type,
        owner_user_id=owner_user_id,
        owner_agent_id=owner_agent_id,
        integration_type=integration_type,
        status=status,
        limit=limit,
    )
    return CredentialListResponse(
        credentials=[_to_response(r) for r in rows], total_count=len(rows)
    )


@router.get("/{credential_id}", response_model=CredentialResponse)
async def get_credential(credential_id: UUID, user: AuthenticatedUser) -> CredentialResponse:
    svc = await _service()
    row = await svc.get_credential(credential_id, user)
    return _to_response(row)


@router.patch("/{credential_id}", response_model=CredentialResponse)
async def update_credential(
    credential_id: UUID,
    body: CredentialUpdate,
    user: AuthenticatedUser,
) -> CredentialResponse:
    svc = await _service()
    row = await svc.update_credential(
        credential_id,
        user,
        display_name=body.display_name,
        scopes=body.scopes,
        access_token=body.access_token,
        refresh_token=body.refresh_token,
        access_token_expires_at=body.access_token_expires_at,
        refresh_token_expires_at=body.refresh_token_expires_at,
        metadata=body.metadata,
        status=(body.status.value if body.status else None),
    )
    return _to_response(row)


@router.delete("/{credential_id}")
async def delete_credential(credential_id: UUID, user: AuthenticatedUser) -> dict[str, bool]:
    svc = await _service()
    return await svc.delete_credential(credential_id, user)


@router.post("/{credential_id}/refresh", response_model=CredentialRefreshResponse)
async def refresh_credential(
    credential_id: UUID,
    user: AuthenticatedUser,
) -> CredentialRefreshResponse:
    svc = await _service()
    return await svc.refresh_credential(credential_id, user)


@router.post("/oauth/start", response_model=OAuthStartResponse)
async def start_oauth(body: OAuthStartRequest, user: AuthenticatedUser) -> OAuthStartResponse:
    svc = await _service()
    return await svc.start_oauth(body, user)


@router.post("/oauth/callback", response_model=CredentialResponse)
async def oauth_callback(
    body: OAuthCallbackRequest,
    user: AuthenticatedUser,
    display_name: str,
) -> CredentialResponse:
    svc = await _service()
    row = await svc.complete_oauth(body, display_name=display_name, user=user)
    return _to_response(row)
