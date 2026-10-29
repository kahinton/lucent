"""Instance catalog API: the operator's tool-provisioning surface.

## Why this is not part of ``/api/definitions``

Those routes answer *"what may I use in my organization?"* — they derive the
tenant from the caller's own membership, enforce per-user consent, and expose the
things a member authors. This router answers a different question: *"what am I
pushing into someone else's organization?"* Every route here writes into rows that
belong to another tenant, so it gets its own prefix, its own role gate, and its own
audit path. Merging the two would mean one code path deciding both "my org" and
"that org", which is exactly where cross-tenant mistakes come from.

## What provisioning produces

A provisioned definition is ``scope='built-in'`` and ``origin='operator'``:

* ``scope='built-in'`` — the semantics the operator asked for. It is
  consent-exempt, readable org-wide, protected from daemon mutation, and has no
  per-user owner. Tenants cannot edit or delete it, the same way they cannot edit
  a shipped skill.
* ``origin='operator'`` — the provenance that keeps it safe from the disk sync.
  Without it, a file on disk with the same name would overwrite the operator's row
  on the next restart, and a name the disk catalogue does not carry would be
  *archived* as retired.

One row is written per organization, deliberately. A single shared NULL-org row
would be cheaper, but it would put a NULL into every authorisation predicate that
resolves a tenant, and "which org does this row belong to" would become a runtime
question instead of a column. One row per tenant keeps the boundary explicit and
makes a per-tenant revoke a plain DELETE.

## Credentials

Nothing here stores a token. A provisioned connector declares
``credential_slots`` and stores ``secret://`` references in its headers, exactly
like a disk-shipped built-in. The value is resolved per **requesting user** through
the clearance-driven secret store, so a hosted tool can be offered to every tenant
while each person supplies their own credential — or an org admin supplies one for
the whole team.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from lucent.api.deps import HyperadminUser
from lucent.db import get_pool
from lucent.db.audit import AuditRepository
from lucent.db.definitions import BuiltInProtectionError, DefinitionRepository
from lucent.rbac import Permission

router = APIRouter()

#: Definition families an operator may provision into a tenant.
CatalogType = Literal["mcp_server", "managed_tool", "skill", "agent"]


class CredentialSlotSpec(BaseModel):
    """One credential the tenant's users must supply."""

    name: str = Field(max_length=128)
    header: str = Field(max_length=128)
    format: str = Field(default="{value}", max_length=256)
    label: str = Field(default="", max_length=200)
    help: str = Field(default="", max_length=2000)
    required: bool = True
    secret_key: str = Field(min_length=1, max_length=128)
    create_url: str = Field(default="", max_length=500)


class ProvisionCatalogMCPServer(BaseModel):
    """Upsert one operator-provisioned remote connector into one organization."""

    name: str = Field(max_length=64)
    description: str = ""
    server_type: Literal["http", "sse", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict | None = None
    env_vars: dict[str, str] | None = None
    credential_slots: list[CredentialSlotSpec] = Field(default_factory=list)
    status: Literal["active", "archived"] = "active"


def _require_catalog_permission(user: HyperadminUser) -> None:
    """Catalog provisioning writes across tenants, so it is instance-scoped.

    Deliberately *not* ``ORG_MANAGE``: this creates capability that other people's
    agents can invoke, which is a stronger thing than changing a tenant setting.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)


async def _require_org(org_id: UUID) -> None:
    pool = await get_pool()
    exists = await pool.fetchval("SELECT 1 FROM organizations WHERE id = $1", org_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")


async def _repository() -> DefinitionRepository:
    pool = await get_pool()
    return DefinitionRepository(pool, audit_repo=AuditRepository(pool))


@router.get("/{organization_id}/catalog/{definition_type}")
async def list_provisioned_definitions(
    organization_id: UUID,
    definition_type: CatalogType,
    user: HyperadminUser,
) -> dict[str, Any]:
    """What this operator has provisioned into one organization.

    Requires hyperadmin. Returns only ``origin='operator'`` rows: disk-shipped
    built-ins are not this API's business, and a tenant's own definitions are not
    either.
    """
    _require_catalog_permission(user)
    await _require_org(organization_id)
    repo = await _repository()
    items = await repo.list_catalog_definitions(str(organization_id), definition_type)
    return {
        "organization_id": str(organization_id),
        "definition_type": definition_type,
        "items": items,
        "total_count": len(items),
    }


@router.get("/catalog/{definition_type}/organizations")
async def list_catalog_fleet(
    definition_type: CatalogType,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Which organizations carry operator-provisioned definitions of this type.

    The fleet view: one query, grouped in SQL, so it does not degrade with tenant
    count.
    """
    _require_catalog_permission(user)
    repo = await _repository()
    orgs = await repo.list_catalog_organizations(definition_type)
    return {
        "definition_type": definition_type,
        "organizations": orgs,
        "total_organizations": len(orgs),
    }


@router.put("/{organization_id}/catalog/mcp-servers")
async def provision_mcp_server(
    organization_id: UUID,
    body: ProvisionCatalogMCPServer,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Create or replace an operator-provisioned connector in this organization.

    Idempotent: re-running with the same ``name`` updates that row. It refuses to
    touch a disk-shipped connector or a tenant-authored definition of the same
    name, rather than silently overwriting either.

    Provisioning does **not** grant the connector to any agent. A tenant admin
    still decides which of their agents may use it, and each user still consents —
    the operator controls availability, not reach.
    """
    _require_catalog_permission(user)
    await _require_org(organization_id)

    if body.server_type in ("http", "sse") and not body.url:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "url is required for an http or sse connector",
        )
    if body.server_type == "stdio" and not body.command:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "command is required for a stdio connector",
        )

    repo = await _repository()
    try:
        result = await repo.provision_catalog_mcp_server(
            org_id=str(organization_id),
            name=body.name,
            description=body.description,
            server_type=body.server_type,
            url=body.url,
            created_by=str(user.id),
            headers=body.headers,
            command=body.command,
            args=body.args,
            env_vars=body.env_vars,
            credential_slots=[slot.model_dump() for slot in body.credential_slots],
            status=body.status,
        )
    except BuiltInProtectionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return result


@router.delete("/{organization_id}/catalog/mcp-servers/{name}")
async def revoke_mcp_server(
    organization_id: UUID,
    name: str,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Remove one operator-provisioned connector from this organization.

    Refuses anything it did not create. Grants pointing at the row cascade, so a
    revoked connector stops being reachable by any agent in that tenant.
    """
    _require_catalog_permission(user)
    await _require_org(organization_id)
    repo = await _repository()
    removed = await repo.revoke_catalog_mcp_server(
        org_id=str(organization_id), name=name, actor_id=str(user.id)
    )
    if not removed:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No operator-provisioned connector named {name!r} in this organization",
        )
    return {"revoked": name, "organization_id": str(organization_id)}
