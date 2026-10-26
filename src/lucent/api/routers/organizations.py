"""Instance-operator (hyperadmin) organization management API.

Two audiences share one router:

* **Tenant endpoints** (``/current``, ``/{id}``) — a member's own organization.
  These predate hyperadmin and keep working exactly as they did.
* **Instance endpoints** — everything a hyperadmin needs to run Lucent for other
  people: provision a complete tenant, add its first admin, mint its API keys,
  read and change its settings, suspend/resume it, and destroy it.

Design notes worth keeping in mind when changing this file:

* Every instance route requires :class:`HyperadminUser`. A tenant owner is
  intentionally *not* enough: these routes write into other tenants' rows.
* A tenant is only usable when it has a human member. ``POST /`` alone creates a
  headless organization; ``POST /provision`` creates the org, its first admin,
  and their API key together, and rolls the org back if the member cannot be
  created. Prefer ``/provision`` for any signup flow.
* Suspension is a soft stop. It blocks sessions and API keys
  (``organization_allows_access``) while retaining every row, and hyperadmins
  keep access so they can inspect and reactivate.
* Mutating routes write an instance-level audit row into both the operator's own
  organization (durable — it survives tenant deletion) and the target
  organization (visible to that tenant's admins).
* Route order matters. Static paths are declared before ``/{organization_id}``
  so a literal segment (``current``) is never swallowed by the UUID parameter
  and rejected with a 422.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent.api.deps import AuthenticatedUser, HyperadminUser, OwnerUser
from lucent.api.models import (
    ErrorResponse,
    InstanceAuditEntry,
    InstanceAuditResponse,
    OrganizationAdminCreate,
    OrganizationAdminCreated,
    OrganizationApiKeyCreate,
    OrganizationApiKeyCreated,
    OrganizationCreate,
    OrganizationListResponse,
    OrganizationProvisionRequest,
    OrganizationProvisionResponse,
    OrganizationResponse,
    OrganizationSettingsResponse,
    OrganizationSettingUpdate,
    OrganizationSettingValue,
    OrganizationUpdate,
    ProvisionedUserResponse,
    SuccessResponse,
    UserResponse,
    UserRoleUpdate,
)
from lucent.db import (
    AdminAuditRepository,
    OrganizationRepository,
    UserRepository,
    get_pool,
)
from lucent.db import admin_audit as audit_actions
from lucent.db.organization import OrganizationNameConflictError
from lucent.logging import get_logger
from lucent.rbac import Permission, can_assign_role, can_manage_user
from lucent.services import org_provisioning
from lucent.services import org_settings as instance_settings

logger = get_logger(__name__)


router = APIRouter()

#: Rows that are infrastructure rather than tenants. Hidden from the operator
#: list so nobody suspends or deletes the organization that holds instance
#: secrets.
HIDDEN_ORGANIZATION_NAMES = ["__lucent_system__"]

#: Roles a hyperadmin may grant to a tenant member through these endpoints.
#: ``hyperadmin`` is deliberately excluded: instance-wide access is granted by
#: operator tooling (``python -m lucent.hyperadmin``), never by adding someone to
#: a customer's organization.
TENANT_ROLES = ("member", "admin", "owner")


def _org_to_response(org: dict[str, Any]) -> OrganizationResponse:
    """Convert an organization dict to a response model."""
    return OrganizationResponse(
        id=org["id"],
        name=org["name"],
        status=org["status"],
        suspended_at=org["suspended_at"],
        created_at=org["created_at"],
        updated_at=org["updated_at"],
        user_count=org.get("user_count"),
        admin_count=org.get("admin_count"),
        owner_names=org.get("owner_names"),
        api_key_count=org.get("api_key_count"),
    )


def _user_to_response(user: dict[str, Any]) -> UserResponse:
    """Convert a user dict to a response model."""
    return UserResponse(
        id=user["id"],
        external_id=user["external_id"],
        provider=user["provider"],
        organization_id=user.get("organization_id"),
        email=user.get("email"),
        display_name=user.get("display_name"),
        avatar_url=user.get("avatar_url"),
        role=user.get("role", "member"),
        is_active=user.get("is_active", True),
        created_at=user["created_at"],
        updated_at=user["updated_at"],
        last_login_at=user.get("last_login_at"),
    )


async def _require_org(org_id: UUID) -> dict[str, Any]:
    """Load an organization or raise 404."""
    pool = await get_pool()
    org = await OrganizationRepository(pool).get_by_id(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


async def _require_member(org_id: UUID, user_id: UUID) -> dict[str, Any]:
    """Load a user and confirm they belong to ``org_id``."""
    pool = await get_pool()
    member = await UserRepository(pool).get_by_id(user_id)
    if member is None or str(member.get("organization_id")) != str(org_id):
        raise HTTPException(status_code=404, detail="User not found in this organization")
    return member


async def _audit(
    user: Any,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID | str | None = None,
    entity_label: str | None = None,
    target_org_id: UUID | str | None = None,
    changed_fields: list[str] | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    notes: str | None = None,
) -> None:
    """Write an instance-level audit row (never raises)."""
    operator_org = user.organization_id or target_org_id
    if operator_org is None:
        logger.warning("No organization to attribute instance audit row %s to", action)
        return
    pool = await get_pool()
    await org_provisioning.log_instance_event(
        pool,
        operator_org_id=operator_org,
        operator_user_id=user.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        target_org_id=target_org_id,
        changed_fields=changed_fields,
        old_values=old_values,
        new_values=new_values,
        notes=notes,
    )


# ===========================================================================
# Static paths first
# ===========================================================================


@router.get(
    "/current",
    response_model=OrganizationResponse,
)
async def get_current_organization(
    user: AuthenticatedUser,
) -> OrganizationResponse:
    """Get the current user's organization."""
    user.require_permission(Permission.ORG_VIEW)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    org = await org_repo.get_by_id(user.organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return _org_to_response(org)


@router.patch(
    "/current",
    response_model=OrganizationResponse,
)
async def update_current_organization(
    data: OrganizationUpdate,
    user: OwnerUser,  # Requires owner role
) -> OrganizationResponse:
    """Update the current organization's settings.

    Requires owner role.
    """
    user.require_permission(Permission.ORG_UPDATE)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    try:
        result = await org_repo.update(
            org_id=user.organization_id,
            name=data.name,
        )
    except OrganizationNameConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization with this name already exists",
        ) from None

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return _org_to_response(result)


@router.delete(
    "/current",
    response_model=SuccessResponse,
)
async def delete_current_organization(
    user: OwnerUser,  # Requires owner role
) -> SuccessResponse:
    """Delete the current organization.

    Requires owner role. This will delete:
    - All organization memories
    - All organization users
    - The organization itself

    This action cannot be undone.
    """
    user.require_permission(Permission.ORG_DELETE)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    success = await org_repo.delete(user.organization_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    logger.info("Organization deleted: id=%s, by=%s", user.organization_id, user.id)
    return SuccessResponse(
        success=True,
        message=f"Organization {user.organization_id} deleted",
    )


@router.post(
    "/current/transfer",
    response_model=SuccessResponse,
)
async def transfer_ownership(
    new_owner_id: UUID,
    user: OwnerUser,  # Requires owner role
) -> SuccessResponse:
    """Transfer organization ownership to another user.

    Requires owner role. The new owner must be a member of the organization.
    The current owner will be demoted to admin.
    """
    user.require_permission(Permission.ORG_TRANSFER)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )

    if new_owner_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You are already the owner",
        )

    pool = await get_pool()
    user_repo = UserRepository(pool)

    # Get new owner
    new_owner = await user_repo.get_by_id(new_owner_id)
    if new_owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Check same organization
    if new_owner.get("organization_id") != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of your organization",
        )

    # Demote-then-promote in one transaction: idx_one_owner_per_org allows a
    # single owner row, so promoting first always fails.
    await user_repo.transfer_ownership(
        organization_id=user.organization_id,
        from_user_id=user.id,
        to_user_id=new_owner_id,
        demote_to="admin",
    )

    logger.info(
        "Ownership transferred: org=%s, from=%s, to=%s",
        user.organization_id,
        user.id,
        new_owner_id,
    )
    return SuccessResponse(
        success=True,
        message=f"Ownership transferred to user {new_owner_id}",
    )


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    data: OrganizationCreate,
    user: HyperadminUser,
) -> OrganizationResponse:
    """Create an organization shell and seed its built-in definitions.

    Requires hyperadmin role.

    The organization this creates has **no human member**, so nobody can log
    into it yet. Use ``POST /provision`` for anything user-facing — it creates
    the organization, its first admin, and an API key in one step.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    try:
        org = await org_repo.create(name=data.name)
    except OrganizationNameConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization with this name already exists",
        ) from None

    # Seed the new org with built-in skills, agents, hooks, and sandbox templates.
    try:
        from lucent.builtin_definitions import sync_built_in_definitions_for_org

        await sync_built_in_definitions_for_org(pool, str(org["id"]))
    except Exception:
        logger.warning(
            "Failed to seed built-in definitions for new org %s", org["id"], exc_info=True
        )

    await _audit(
        user,
        action=audit_actions.ORG_PROVISION,
        entity_type="organization",
        entity_id=org["id"],
        entity_label=data.name,
        target_org_id=org["id"],
        changed_fields=["organization"],
        new_values={"name": data.name, "first_user": None},
        notes="Organization shell created without a member (POST /api/organizations).",
    )

    logger.info("Organization created: id=%s, name=%s, by=%s", org["id"], data.name, user.id)
    return _org_to_response(org)


@router.post(
    "/provision",
    response_model=OrganizationProvisionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def provision_organization(
    data: OrganizationProvisionRequest,
    user: HyperadminUser,
) -> OrganizationProvisionResponse:
    """Create a complete, immediately usable organization.

    Requires hyperadmin role.

    This is the endpoint an external signup site should call: it creates the
    tenant, its first admin (with a generated password unless one is supplied),
    a default API key, seeds the built-in agent/skill definitions, and
    optionally enables a model so the tenant can chat on first login.

    Secrets are returned **once** in this response and are never retrievable
    afterwards.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    pool = await get_pool()

    first = data.first_admin
    try:
        result = await org_provisioning.provision_organization(
            pool,
            name=data.name,
            admin_email=first.email if first else None,
            admin_display_name=first.display_name if first else None,
            admin_password=first.password if first else None,
            admin_role=first.role if first else "owner",
            admin_external_id=first.external_id if first else None,
            api_key_name=data.api_key_name,
            model_id=data.model_id,
            operator_org_id=user.organization_id,
            operator_user_id=user.id,
        )
    except org_provisioning.OrganizationNameTakenError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except org_provisioning.FirstUserConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except org_provisioning.InvalidProvisioningRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    return OrganizationProvisionResponse(
        organization=_org_to_response(result.organization),
        user=ProvisionedUserResponse(
            id=result.user["id"],
            email=result.user.get("email"),
            display_name=result.user.get("display_name"),
            role=result.user.get("role", "owner"),
            must_change_password=result.temporary_password is not None,
        ),
        api_key=result.api_key,
        temporary_password=result.temporary_password,
        seeded=result.seeded,
        model_id=result.model_id,
        warnings=result.warnings,
    )


@router.get(
    "/instance/audit",
    response_model=InstanceAuditResponse,
)
async def list_instance_audit(
    user: HyperadminUser,
    organization_id: UUID | None = Query(
        default=None, description="Only events that touched this tenant"
    ),
    action: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> InstanceAuditResponse:
    """Browse the instance-operator audit trail.

    Requires hyperadmin role. The trail lives in the operator's own
    organization, so this is a single-tenant read.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Instance operators must belong to an organization to hold an audit trail",
        )

    pool = await get_pool()
    result = await AdminAuditRepository(pool).list_instance_events(
        operator_org_id=user.organization_id,
        target_organization_id=organization_id,
        action=action,
        limit=limit,
        offset=offset,
    )

    return InstanceAuditResponse(
        entries=[
            InstanceAuditEntry(
                id=entry["id"],
                organization_id=entry["organization_id"],
                actor_user_id=entry.get("actor_user_id"),
                actor_display_name=entry.get("actor_display_name"),
                action=entry["action"],
                entity_type=entry["entity_type"],
                entity_id=entry.get("entity_id"),
                entity_label=entry.get("entity_label"),
                target_organization_id=entry.get("target_organization_id"),
                changed_fields=entry.get("changed_fields"),
                old_values=entry.get("old_values"),
                new_values=entry.get("new_values"),
                notes=entry.get("notes"),
                created_at=entry["created_at"],
            )
            for entry in result["entries"]
        ],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["limit"],
        has_more=result["has_more"],
    )


@router.get(
    "",
    response_model=OrganizationListResponse,
)
async def list_organizations(
    user: HyperadminUser,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(
        default=None, alias="status", description="active or suspended"
    ),
    search: str | None = Query(default=None, description="Substring match on the name"),
    include_stats: bool = Query(
        default=True, description="Include member and API-key counts"
    ),
) -> OrganizationListResponse:
    """List all organizations. Requires hyperadmin role.

    The internal ``__lucent_system__`` organization is never listed: it holds
    instance secrets and is not a tenant an operator manages.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    result = await org_repo.list(
        offset=offset,
        limit=limit,
        status=status_filter,
        search=search,
        exclude_names=HIDDEN_ORGANIZATION_NAMES,
        include_stats=include_stats,
    )
    return OrganizationListResponse(
        organizations=[_org_to_response(org) for org in result["organizations"]],
        total_count=result["total_count"],
        offset=offset,
        limit=limit,
        has_more=result["has_more"],
    )


# ===========================================================================
# Dynamic organization paths
# ===========================================================================


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_organization(
    organization_id: UUID,
    user: AuthenticatedUser,
    include_stats: bool = Query(default=False),
) -> OrganizationResponse:
    """Get an organization by ID.

    Hyperadmins can view any organization. Other users can only view their own.
    """
    is_instance_operator = user.role.value == "hyperadmin"
    if organization_id != user.organization_id and not is_instance_operator:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)

    org = await org_repo.get_by_id(organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    if include_stats and is_instance_operator:
        org = {**org, **(await org_repo.get_stats(organization_id))}

    return _org_to_response(org)


@router.patch(
    "/{organization_id}",
    response_model=OrganizationResponse,
    responses={404: {"model": ErrorResponse}},
)
async def update_organization(
    organization_id: UUID,
    data: OrganizationUpdate,
    user: HyperadminUser,
) -> OrganizationResponse:
    """Update any organization's name. Requires hyperadmin role."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    before = await org_repo.get_by_id(organization_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        result = await org_repo.update(organization_id, data.name)
    except OrganizationNameConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization with this name already exists",
        ) from None
    if result is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    if before["name"] != result["name"]:
        await _audit(
            user,
            action=audit_actions.ORG_UPDATE,
            entity_type="organization",
            entity_id=organization_id,
            entity_label=result["name"],
            target_org_id=organization_id,
            changed_fields=["name"],
            old_values={"name": before["name"]},
            new_values={"name": result["name"]},
            notes="Organization renamed by an instance operator.",
        )

    logger.info("Organization updated: id=%s, by=%s", organization_id, user.id)
    return _org_to_response(result)


@router.delete(
    "/{organization_id}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def delete_organization(
    organization_id: UUID,
    user: HyperadminUser,
    confirm_name: str = Query(
        ...,
        description=(
            "Must match the organization's current name exactly. Guards against "
            "deleting the wrong tenant from a stale console."
        ),
    ),
) -> SuccessResponse:
    """Permanently delete an organization and all of its data.

    Requires hyperadmin role. The internal system organization cannot be
    deleted, and the audit row is written to the *operator's* organization
    because the target's own rows cascade away with it.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Instance operators must belong to an organization to hold an audit trail",
        )

    pool = await get_pool()
    try:
        result = await org_provisioning.delete_organization(
            pool,
            organization_id=organization_id,
            operator_org_id=user.organization_id,
            operator_user_id=user.id,
            confirm_name=confirm_name,
        )
    except org_provisioning.InvalidProvisioningRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="Organization not found")

    return SuccessResponse(
        success=True,
        message=f"Organization '{result['name']}' deleted",
    )


@router.post(
    "/{organization_id}/suspend",
    response_model=OrganizationResponse,
    responses={404: {"model": ErrorResponse}},
)
async def suspend_organization(
    organization_id: UUID,
    user: HyperadminUser,
) -> OrganizationResponse:
    """Suspend an organization without deleting its data.

    Suspension blocks session and API-key authentication for the tenant;
    hyperadmins retain access so they can inspect and resume it.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    before = await org_repo.get_by_id(organization_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    result = await org_repo.set_suspended(organization_id, suspended=True)
    if result is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    if before["status"] != "suspended":
        await _audit(
            user,
            action=audit_actions.ORG_SUSPEND,
            entity_type="organization",
            entity_id=organization_id,
            entity_label=result["name"],
            target_org_id=organization_id,
            changed_fields=["status"],
            old_values={"status": before["status"]},
            new_values={"status": result["status"]},
            notes="Organization suspended by an instance operator.",
        )

    logger.info("Organization suspended: id=%s, by=%s", organization_id, user.id)
    return _org_to_response(result)


@router.post(
    "/{organization_id}/resume",
    response_model=OrganizationResponse,
    responses={404: {"model": ErrorResponse}},
)
async def resume_organization(
    organization_id: UUID,
    user: HyperadminUser,
) -> OrganizationResponse:
    """Reactivate a suspended organization. Requires hyperadmin role."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    before = await org_repo.get_by_id(organization_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    result = await org_repo.set_suspended(organization_id, suspended=False)
    if result is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    if before["status"] != "active":
        await _audit(
            user,
            action=audit_actions.ORG_RESUME,
            entity_type="organization",
            entity_id=organization_id,
            entity_label=result["name"],
            target_org_id=organization_id,
            changed_fields=["status"],
            old_values={"status": before["status"]},
            new_values={"status": result["status"]},
            notes="Organization resumed by an instance operator.",
        )

    logger.info("Organization resumed: id=%s, by=%s", organization_id, user.id)
    return _org_to_response(result)


# --- Organization settings ------------------------------------------------


@router.get(
    "/{organization_id}/settings",
    response_model=OrganizationSettingsResponse,
)
async def get_organization_settings(
    organization_id: UUID,
    user: HyperadminUser,
) -> OrganizationSettingsResponse:
    """Read a target organization's admin-editable runtime settings.

    Requires hyperadmin role. Only settings an operator can change are
    returned; read-only entries (database credentials and the like) are omitted
    entirely rather than redacted.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    payload = await instance_settings.load_settings(pool, organization_id)

    return OrganizationSettingsResponse(
        organization_id=payload["organization_id"],
        settings=[OrganizationSettingValue(**entry) for entry in payload["settings"]],
        sections={
            section: [OrganizationSettingValue(**entry) for entry in entries]
            for section, entries in payload["sections"].items()
        },
    )


@router.put(
    "/{organization_id}/settings/{setting_key:path}",
    response_model=OrganizationSettingValue,
)
async def update_organization_setting(
    organization_id: UUID,
    setting_key: str,
    data: OrganizationSettingUpdate,
    user: HyperadminUser,
) -> OrganizationSettingValue:
    """Set or reset one runtime setting on any organization.

    Requires hyperadmin role. Values are validated by the same rules the
    settings UI uses, so an operator cannot store something a tenant admin
    could not. Every change is audit-logged in both organizations.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    try:
        result = await instance_settings.update_setting(
            pool,
            organization_id,
            setting_key,
            value=data.value,
            reset=data.reset,
            actor_user_id=user.id,
        )
    except instance_settings.UnknownSettingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    await _audit(
        user,
        action=audit_actions.ORG_SETTING_UPDATE,
        entity_type="settings",
        entity_id=organization_id,
        entity_label=result["title"],
        target_org_id=organization_id,
        changed_fields=[setting_key],
        old_values={"value": result["previous_value"], "source": result["previous_source"]},
        new_values={"value": result["value"], "source": result["source"]},
        notes=(
            f"Setting {setting_key} reset to fallback by an instance operator."
            if data.reset
            else f"Setting {setting_key} changed by an instance operator."
        ),
    )

    return OrganizationSettingValue(**result["entry"])


@router.get(
    "/{organization_id}/audit",
    response_model=InstanceAuditResponse,
)
async def get_organization_audit(
    organization_id: UUID,
    user: HyperadminUser,
    action: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> InstanceAuditResponse:
    """Read a target organization's admin audit log.

    Requires hyperadmin role. Instance-operator actions are mirrored into the
    tenant's own log, so this contains both tenant administration and operator
    changes.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    result = await AdminAuditRepository(pool).list_for_org(
        organization_id=organization_id,
        action=action,
        limit=limit,
        offset=offset,
    )

    return InstanceAuditResponse(
        entries=[
            InstanceAuditEntry(
                id=entry["id"],
                organization_id=entry["organization_id"],
                actor_user_id=entry.get("actor_user_id"),
                actor_display_name=entry.get("actor_display_name"),
                action=entry["action"],
                entity_type=entry["entity_type"],
                entity_id=entry.get("entity_id"),
                entity_label=entry.get("entity_label"),
                target_organization_id=str(organization_id),
                changed_fields=entry.get("changed_fields"),
                old_values=entry.get("old_values"),
                new_values=entry.get("new_values"),
                notes=entry.get("notes"),
                created_at=entry["created_at"],
            )
            for entry in result["entries"]
        ],
        total_count=result["total_count"],
        offset=result["offset"],
        limit=result["page_size"],
        has_more=result["offset"] + len(result["entries"]) < result["total_count"],
    )


# --- Organization members -------------------------------------------------


@router.get(
    "/{organization_id}/users",
    response_model=list[UserResponse],
)
async def list_organization_users(
    organization_id: UUID,
    user: HyperadminUser,
    role: str | None = Query(default=None),
    include_daemon: bool = Query(
        default=False, description="Include the per-org daemon service identity"
    ),
) -> list[UserResponse]:
    """List the members of any organization. Requires hyperadmin role."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    members = await UserRepository(pool).get_by_organization(
        organization_id=organization_id, role=role
    )
    if not include_daemon:
        members = [m for m in members if m.get("role") != "daemon"]

    return [_user_to_response(m) for m in members]


@router.post(
    "/{organization_id}/users",
    response_model=OrganizationAdminCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_user(
    organization_id: UUID,
    data: OrganizationAdminCreate,
    user: HyperadminUser,
    mint_api_key: bool = Query(
        default=False, description="Also mint an API key for the new member"
    ),
) -> OrganizationAdminCreated:
    """Add a member to any organization. Requires hyperadmin role.

    If no password is supplied a temporary one is generated, returned once, and
    the member must change it at first login.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    org = await _require_org(organization_id)

    if data.role not in TENANT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role must be one of: {', '.join(TENANT_ROLES)}",
        )
    if not can_assign_role(user.role, data.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign the '{data.role}' role",
        )

    pool = await get_pool()
    try:
        member, temporary_password = await org_provisioning.add_member(
            pool,
            organization_id=organization_id,
            role=data.role,
            email=data.email,
            display_name=data.display_name,
            external_id=data.external_id,
            password=data.password,
        )
    except org_provisioning.FirstUserConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except org_provisioning.OrganizationAlreadyHasOwnerError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except org_provisioning.InvalidProvisioningRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    plaintext_key = None
    if mint_api_key:
        minted = await org_provisioning.mint_api_key(
            pool,
            user_id=member["id"],
            organization_id=organization_id,
        )
        plaintext_key = minted["plaintext"]

    await _audit(
        user,
        action=audit_actions.ORG_USER_CREATE,
        entity_type="user",
        entity_id=member["id"],
        entity_label=member.get("display_name") or member.get("email"),
        target_org_id=organization_id,
        changed_fields=["user"],
        new_values={
            "email": member.get("email"),
            "role": member.get("role"),
            "organization": org["name"],
            "api_key_minted": bool(plaintext_key),
        },
        notes="Member added by an instance operator.",
    )

    return OrganizationAdminCreated(
        user=_user_to_response(member),
        temporary_password=temporary_password,
        api_key=plaintext_key,
    )


@router.patch(
    "/{organization_id}/users/{user_id}/role",
    response_model=UserResponse,
)
async def update_organization_user_role(
    organization_id: UUID,
    user_id: UUID,
    data: UserRoleUpdate,
    user: HyperadminUser,
) -> UserResponse:
    """Change a member's role in any organization. Requires hyperadmin role."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    target = await _require_member(organization_id, user_id)

    if data.role not in TENANT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role must be one of: {', '.join(TENANT_ROLES)}",
        )
    if not can_manage_user(user.role, target.get("role", "member")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot manage this user",
        )
    if not can_assign_role(user.role, data.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign the '{data.role}' role",
        )

    pool = await get_pool()
    if data.role == "owner" and target.get("role") != "owner":
        stats = await OrganizationRepository(pool).get_stats(organization_id)
        if stats["owner_count"] > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This organization already has an owner. Ownership must be "
                    "transferred by the current owner."
                ),
            )

    result = await UserRepository(pool).update_role(user_id, data.role)
    if result is None:
        raise HTTPException(status_code=404, detail="User not found")

    if target.get("role") != data.role:
        await _audit(
            user,
            action=audit_actions.ORG_USER_ROLE_CHANGE,
            entity_type="user",
            entity_id=user_id,
            entity_label=target.get("display_name") or target.get("email"),
            target_org_id=organization_id,
            changed_fields=["role"],
            old_values={"role": target.get("role")},
            new_values={"role": data.role},
            notes="Member role changed by an instance operator.",
        )

    return _user_to_response(result)


@router.post(
    "/{organization_id}/users/{user_id}/reset-password",
    response_model=SuccessResponse,
)
async def reset_organization_user_password(
    organization_id: UUID,
    user_id: UUID,
    user: HyperadminUser,
) -> SuccessResponse:
    """Reset a member's password to a temporary value. Requires hyperadmin role.

    The temporary password is returned in the message — it is generated here and
    stored nowhere, so this response is the only copy.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    target = await _require_member(organization_id, user_id)

    from lucent.auth_providers import admin_reset_password

    pool = await get_pool()
    temporary_password = await admin_reset_password(pool, user_id)

    await _audit(
        user,
        action=audit_actions.USER_PASSWORD_RESET,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
        target_org_id=organization_id,
        notes="Password reset by an instance operator.",
    )

    return SuccessResponse(
        success=True,
        message=(
            f"Temporary password for {target.get('email') or user_id}: "
            f"{temporary_password} (must be changed at next login)"
        ),
    )


@router.post(
    "/{organization_id}/users/{user_id}/api-keys",
    response_model=OrganizationApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_user_api_key(
    organization_id: UUID,
    user_id: UUID,
    data: OrganizationApiKeyCreate,
    user: HyperadminUser,
) -> OrganizationApiKeyCreated:
    """Mint an API key for any member of any organization.

    Requires hyperadmin role. The plaintext key is returned exactly once.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    target = await _require_member(organization_id, user_id)

    pool = await get_pool()
    try:
        minted = await org_provisioning.mint_api_key(
            pool,
            user_id=user_id,
            organization_id=organization_id,
            name=data.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    await _audit(
        user,
        action=audit_actions.ORG_API_KEY_CREATE,
        entity_type="api_key",
        entity_id=minted["record"]["id"],
        entity_label=data.name,
        target_org_id=organization_id,
        changed_fields=["api_key"],
        new_values={"name": data.name, "user": str(user_id)},
        notes=(
            "API key minted by an instance operator for "
            f"{target.get('email') or user_id}."
        ),
    )

    return OrganizationApiKeyCreated(
        id=minted["record"]["id"],
        name=minted["record"]["name"],
        key_prefix=minted["record"]["key_prefix"],
        api_key=minted["plaintext"],
    )


@router.delete(
    "/{organization_id}/users/{user_id}",
    response_model=SuccessResponse,
)
async def delete_organization_user(
    organization_id: UUID,
    user_id: UUID,
    user: HyperadminUser,
) -> SuccessResponse:
    """Remove a member from any organization. Requires hyperadmin role.

    This deletes the user and (by cascade) their memories and API keys. The last
    remaining owner is refused so a tenant can never be left headless.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    target = await _require_member(organization_id, user_id)

    if not can_manage_user(user.role, target.get("role", "member")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot manage this user",
        )

    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    stats = await org_repo.get_stats(organization_id)
    if target.get("role") == "owner" and stats["owner_count"] <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This is the organization's only owner. Promote another member first "
                "so the tenant is not left unmanageable."
            ),
        )

    deleted = await UserRepository(pool).delete(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")

    await _audit(
        user,
        action=audit_actions.USER_DELETE,
        entity_type="user",
        entity_id=user_id,
        entity_label=target.get("display_name") or target.get("email"),
        target_org_id=organization_id,
        changed_fields=["user"],
        old_values={"email": target.get("email"), "role": target.get("role")},
        notes="Member removed by an instance operator.",
    )

    return SuccessResponse(success=True, message=f"User {user_id} removed")
