"""API router for remote instance management (Agent Control Protocol).

Management endpoints (CRUD, rotation, requeue) are owner-only: a remote can
execute arbitrary work descriptions, so registering one is an owner-level
trust decision. Admins get read access.

Protocol endpoints (claim/heartbeat) are used by the remote agent client with
a scoped ``lr_`` API key; the allowlist that restricts those keys to exactly
these paths lives in ``lucent.api.deps``.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from lucent.api.deps import AuthenticatedUser, get_pool
from lucent.api.deps import get_current_user
from lucent.db.pool import scoped_acquire
from lucent.rbac import Role

router = APIRouter(tags=["Remotes"])
remote_protocol_router = APIRouter(tags=["Remotes"])

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────


class RemoteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    capabilities: dict | None = None
    expires_at: str | None = Field(
        default=None,
        max_length=64,
        description="ISO timestamp for the initial agent token lifetime.",
    )


class RemoteUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    capabilities: dict | None = None
    status: str | None = Field(default=None, pattern=r"^(active|disabled|revoked)$")


class RemoteTokenRotate(BaseModel):
    expires_at: str | None = Field(default=None, max_length=64)


def _require_owner(user: AuthenticatedUser) -> None:
    if user.role < Role.OWNER:
        raise HTTPException(
            status_code=403,
            detail="Only organization owners can manage remote instances",
        )


def _require_bound_agent(user: AuthenticatedUser, remote_id: UUID) -> None:
    """Defense in depth: the calling key must be bound to ``remote_id``.

    ``lucent.api.deps`` already rejects a mismatch in the path-vs-key binding
    before routing, but the protocol endpoints are the security boundary for
    an external peer — they must not depend on a dependency-layer check alone
    remaining in place.
    """
    if "remote-agent" not in (user.api_key_scopes or []):
        raise HTTPException(
            status_code=403,
            detail="API key does not have required scope: remote-agent",
        )
    bound = getattr(user, "remote_agent_id", None)
    if not bound:
        raise HTTPException(
            status_code=403,
            detail="Remote agent key is not bound to a remote instance",
        )
    if str(bound) != str(remote_id):
        raise HTTPException(
            status_code=403,
            detail="Remote agent key is scoped to a different remote instance",
        )


def _remote_to_dict(row: dict, key_info: dict | None = None) -> dict:
    data = dict(row)
    if data.get("capabilities") and isinstance(data["capabilities"], str):
        try:
            import json as _json

            data["capabilities"] = _json.loads(data["capabilities"])
        except (ValueError, TypeError):
            pass
    if key_info:
        data["token"] = key_info
    return data


def _iso_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None
    return None


# ── Management (owner/admin) ──────────────────────────────────────────────


@router.post("")
async def create_remote(
    body: RemoteCreate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Register a remote instance and issue its agent token (owner-only).

    The plaintext token is returned exactly once.
    """
    from lucent.db.remotes import RemoteRepository

    _require_owner(user)
    repo = RemoteRepository(pool)
    try:
        remote, plain_key = await repo.create(
            org_id=str(user.organization_id),
            name=body.name,
            created_by=str(user.id),
            description=body.description,
            capabilities=body.capabilities,
            expires_at=body.expires_at,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    key_info = await repo.get_key_expiry_info(str(remote["id"]), str(user.organization_id))
    logger.info(
        "Remote created: remote_id=%s by_user=%s org=%s",
        remote["id"],
        user.id,
        user.organization_id,
    )
    return _remote_to_dict(remote, key_info) | {"token": plain_key}


@router.get("")
async def list_remotes(
    user: AuthenticatedUser,
    include_revoked: bool = False,
    pool=Depends(get_pool),
):
    from lucent.db.remotes import RemoteRepository

    repo = RemoteRepository(pool)
    items = await repo.list(str(user.organization_id), include_revoked=include_revoked)
    result = []
    for remote in items:
        key_info = await repo.get_key_expiry_info(str(remote["id"]), str(user.organization_id))
        result.append(_remote_to_dict(remote, key_info))
    return {"items": result, "total_count": len(result)}


@router.get("/{remote_id}")
async def get_remote(
    remote_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.remotes import RemoteRepository

    repo = RemoteRepository(pool)
    remote = await repo.get(str(remote_id), str(user.organization_id))
    if not remote:
        raise HTTPException(404, "Remote not found")
    key_info = await repo.get_key_expiry_info(str(remote_id), str(user.organization_id))
    return _remote_to_dict(remote, key_info)


@router.patch("/{remote_id}")
async def update_remote(
    remote_id: UUID,
    body: RemoteUpdate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    from lucent.db.remotes import RemoteRepository

    _require_owner(user)
    repo = RemoteRepository(pool)
    remote = await repo.update(
        str(remote_id),
        str(user.organization_id),
        description=body.description,
        capabilities=body.capabilities,
        status=body.status,
    )
    if not remote:
        raise HTTPException(404, "Remote not found")
    logger.info(
        "Remote updated: remote_id=%s status=%s by_user=%s",
        remote_id,
        body.status,
        user.id,
    )
    return _remote_to_dict(remote)


@router.post("/{remote_id}/rotate-token")
async def rotate_remote_token(
    remote_id: UUID,
    user: AuthenticatedUser,
    body: RemoteTokenRotate = Body(default=RemoteTokenRotate()),
    pool=Depends(get_pool),
):
    """Issue a fresh agent token; the previous token is revoked immediately."""
    from lucent.db.remotes import RemoteRepository

    _require_owner(user)
    repo = RemoteRepository(pool)
    plain_key = await repo.rotate_token(
        str(remote_id),
        str(user.organization_id),
        expires_at=body.expires_at if body else None,
    )
    if plain_key is None:
        raise HTTPException(404, "Remote not found or revoked")
    logger.info("Remote token rotated: remote_id=%s by_user=%s", remote_id, user.id)
    return {"token": plain_key}


@router.post("/{remote_id}/requeue-tasks")
async def requeue_remote_tasks(
    remote_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Release all pending/planned tasks targeted at this remote back to the
    local dispatch pool (clears ``remote_target_id``). Owner-only."""
    from lucent.db.requests import RequestRepository

    _require_owner(user)
    repo = RemoteRepository(pool)
    remote = await repo.get(str(remote_id), str(user.organization_id))
    if not remote:
        raise HTTPException(404, "Remote not found")

    request_repo = RequestRepository(pool)
    released = await request_repo.clear_remote_targeting(
        str(remote_id), str(user.organization_id)
    )
    logger.info(
        "Remote tasks requeued: remote_id=%s released=%s by_user=%s",
        remote_id,
        released,
        user.id,
    )
    # Also release anything the remote currently holds claimed, so work does
    # not dangle mid-flight on a remote being wound down.
    stale = await request_repo.release_stale_tasks(org_id=str(user.organization_id))
    return {"requeued": released, "released_claims": stale}


# ── Protocol (remote agent client, lr_ scoped key) ────────────────────────


@remote_protocol_router.post("/remotes/{remote_id}/claim")
async def remote_claim(
    remote_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Atomically claim the oldest dispatchable task targeted at this remote.

    Returns the full task payload, or 204 when nothing is waiting. The remote
    must be active; disabled remotes cannot claim.
    """
    from lucent.db.remotes import RemoteRepository
    from lucent.db.requests import RequestRepository

    _require_bound_agent(user, remote_id)
    remote_repo = RemoteRepository(pool)
    remote = await remote_repo.require_active(str(remote_id), str(user.organization_id))
    if not remote:
        raise HTTPException(403, "Remote is not active")

    request_repo = RequestRepository(pool)
    task = await request_repo.claim_remote_task(
        remote_id=str(remote_id),
        org_id=str(user.organization_id),
    )
    await remote_repo.touch_last_seen(str(remote_id), str(user.organization_id))
    if not task:
        from fastapi import Response

        return Response(status_code=204)
    return _task_payload_for_remote(task, remote, str(user.organization_id))


@remote_protocol_router.post("/remotes/{remote_id}/heartbeat")
async def remote_heartbeat(
    remote_id: UUID,
    user: AuthenticatedUser,
    body: dict | None = Body(default=None),
    pool=Depends(get_pool),
):
    """Refresh remote liveness AND renew leases on its claimed tasks.

    The remote also registers as a ``daemon_instances`` row (``remote:{id}``)
    so the Stale Task Reaper covers remote claims without special-casing.
    """
    from lucent.db.remotes import RemoteRepository
    from lucent.db.requests import RequestRepository

    _require_bound_agent(user, remote_id)
    remote_repo = RemoteRepository(pool)
    remote = await remote_repo.get(str(remote_id), str(user.organization_id))
    if not remote or remote["status"] == "revoked":
        raise HTTPException(403, "Remote is not active")

    instance_id = f"remote:{remote_id}"
    request_repo = RequestRepository(pool)

    # Register idempotently (upsert) so lease renewal has a row to renew.
    existing = await request_repo.get_daemon_instance(
        instance_id, org_id=str(user.organization_id)
    )
    if not existing:
        await request_repo.register_instance(
            org_id=str(user.organization_id),
            instance_id=instance_id,
            hostname=None,
            pid=None,
            roles=["remote"],
            metadata={"remote_name": remote["name"], "kind": "remote"},
            status="active",
        )
    else:
        await request_repo.heartbeat_instance(
            org_id=str(user.organization_id),
            instance_id=instance_id,
        )
    await remote_repo.require_active(str(remote_id), str(user.organization_id))
    await remote_repo.touch_last_seen(str(remote_id), str(user.organization_id))
    return {
        "ok": True,
        "status": remote["status"],
        "claimed_active": await request_repo.count_active_remote_tasks(
            remote_id=str(remote_id), org_id=str(user.organization_id)
        ),
    }


def _task_payload_for_remote(task: dict, remote: dict, org_id: str) -> dict:
    """Shape the claim response and stamp the claim as remote-attributed."""
    payload = dict(task)
    payload.pop("sandbox_config", None)  # remote contract: plain execution
    payload["remote_instance_id"] = f"remote:{remote['id']}"
    return payload