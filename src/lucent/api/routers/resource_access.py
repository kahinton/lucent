"""REST API for managing resource access grants (the many-to-many access plane).

This is the REST counterpart to the web ``/access/...`` routes and the MCP
``*_resource_access`` tools. All three surfaces delegate to the same
``AccessControlService`` methods, so the access model is identical everywhere:

* Only the resource's managing owner or an org admin/owner may view or edit
  grants (``can_modify``). Grants control *use*, not *management*.
* Scoped API-key contexts (daemon sub-agents) may not manage grants — they
  cannot self-escalate runtime access.
* Grant principals must belong to the caller's organization (no cross-tenant
  or dangling grants).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lucent.access_control import AccessControlService, canonical_resource_type
from lucent.api.deps import AuthenticatedUser
from lucent.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/access-grants", tags=["Access Grants"])


class GrantPrincipal(BaseModel):
    """A principal to grant or revoke: a user, a group, or the whole org."""

    principal_type: str  # "user" | "group" | "org"
    principal_id: str | None = None  # required for user/group; ignored for org


def _validate_principal_type(principal_type: str) -> str:
    ptype = (principal_type or "").strip().lower()
    if ptype not in ("user", "group", "org"):
        raise HTTPException(400, "principal_type must be 'user', 'group', or 'org'")
    return ptype


async def _require_manage(
    acl: AccessControlService, user: AuthenticatedUser, resource_type: str, resource_id: str
) -> str:
    """Validate type, block scoped keys, enforce manage permission; return canonical token."""
    if user.is_memory_scoped:
        raise HTTPException(
            403,
            "Scoped agent contexts cannot manage resource access grants",
        )
    try:
        rtype = canonical_resource_type(resource_type)
    except ValueError:
        raise HTTPException(404, "Unknown resource type")
    if not await acl.can_modify(
        str(user.id), rtype, resource_id, str(user.organization_id)
    ):
        raise HTTPException(403, "Permission denied")
    return rtype


@router.get("/{resource_type}/{resource_id}")
async def list_resource_grants(
    resource_type: str, resource_id: str, user: AuthenticatedUser
):
    """List the access grants on a resource. Visible only to managers."""
    pool = await get_pool()
    acl = AccessControlService(pool)
    rtype = await _require_manage(acl, user, resource_type, resource_id)
    grants = await acl.list_access_grants(rtype, resource_id, str(user.organization_id))
    return {
        "resource_type": rtype,
        "resource_id": str(resource_id),
        "grants": grants,
    }


@router.post("/{resource_type}/{resource_id}/grant", status_code=201)
async def grant_resource_access(
    resource_type: str, resource_id: str, body: GrantPrincipal, user: AuthenticatedUser
):
    """Grant a user, group, or the whole org access to use a resource."""
    pool = await get_pool()
    acl = AccessControlService(pool)
    rtype = await _require_manage(acl, user, resource_type, resource_id)
    ptype = _validate_principal_type(body.principal_type)
    if ptype in ("user", "group") and not body.principal_id:
        raise HTTPException(400, "principal_id is required for user/group grants")
    if not await acl.principal_exists_in_org(
        principal_type=ptype,
        principal_id=body.principal_id,
        org_id=str(user.organization_id),
    ):
        raise HTTPException(400, "Principal not found in this organization")
    await acl.grant_access(
        resource_type=rtype,
        resource_id=resource_id,
        org_id=str(user.organization_id),
        principal_type=ptype,
        principal_id=body.principal_id,
        granted_by=str(user.id),
    )
    return {"status": "granted", "principal_type": ptype, "principal_id": body.principal_id}


@router.post("/{resource_type}/{resource_id}/revoke")
async def revoke_resource_access(
    resource_type: str, resource_id: str, body: GrantPrincipal, user: AuthenticatedUser
):
    """Revoke a user, group, or org-wide grant from a resource."""
    pool = await get_pool()
    acl = AccessControlService(pool)
    rtype = await _require_manage(acl, user, resource_type, resource_id)
    ptype = _validate_principal_type(body.principal_type)
    if ptype in ("user", "group") and not body.principal_id:
        raise HTTPException(400, "principal_id is required for user/group grants")
    await acl.revoke_access(
        resource_type=rtype,
        resource_id=resource_id,
        org_id=str(user.organization_id),
        principal_type=ptype,
        principal_id=body.principal_id,
    )
    return {"status": "revoked", "principal_type": ptype, "principal_id": body.principal_id}
