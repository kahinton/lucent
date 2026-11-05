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
* A tenant is only usable when it has a human member, so this router has no
  bare create endpoint. ``POST /provision`` creates the org, its first admin,
  and their API key together, and rolls the org back if the member cannot be
  created; any signup or console flow calls it. A shell-creating ``POST`` here
  would only offer a fast way to mint an unusable tenant.
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

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent import settings as runtime_settings
from lucent.api.deps import AuthenticatedUser, HyperadminUser, OwnerUser
from lucent.api.models import (
    AccountLookupMatch,
    AccountLookupResponse,
    CredentialFieldSpec,
    EngineCatalogEntry,
    EngineProviderOption,
    ErrorResponse,
    InstanceAuditEntry,
    InstanceAuditResponse,
    InstanceSettingLockEntry,
    InstanceSettingLockResponse,
    OrganizationAdminCreate,
    OrganizationAdminCreated,
    OrganizationApiKeyCreate,
    OrganizationApiKeyCreated,
    OrganizationBackendCreate,
    OrganizationCredentialUpdate,
    OrganizationInvitationCreate,
    OrganizationInvitationCreated,
    OrganizationInvitationList,
    OrganizationInvitationSummary,
    OrganizationListResponse,
    OrganizationModelCreate,
    OrganizationModelToggle,
    OrganizationProvisionRequest,
    OrganizationProvisionResponse,
    OrganizationResponse,
    OrganizationSettingLockBulkRequest,
    OrganizationSettingLockBulkResult,
    OrganizationSettingLockReleaseRequest,
    OrganizationSettingLockUpdate,
    OrganizationSettingLockValue,
    OrganizationSettingsResponse,
    OrganizationSettingUpdate,
    OrganizationSettingValue,
    OrganizationUpdate,
    ProvisionedUserResponse,
    SuccessResponse,
    UsageBackfillResult,
    UsageOrganizationSummary,
    UsageReport,
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
from lucent.db.resource_usage import ResourceUsageRepository
from lucent.logging import get_logger
from lucent.rbac import Permission, can_assign_role, can_manage_user
from lucent.services import identity as identity_service
from lucent.services import org_providers as instance_providers
from lucent.services import org_provisioning, usage
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
    "/provision",
    response_model=OrganizationProvisionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {
            "description": (
                "Organization name or owner email already exists. The detail is "
                '{"field": "<collided field>", "message": "..."} so a control plane '
                "knows which input to change."
            )
        }
    },
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

    Errors:
        409 — the organization name is taken, or an account already exists for
        the first admin's identity: ``detail`` is
        ``{"field": "name" | "owner_email" | "external_id", "message": ...}``
        naming the input that collided.

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
            model_provider=data.model_provider,
            model_backend=data.model_backend,
            providers=[
                {"provider": p.provider, "api_key": p.api_key} for p in data.providers
            ],
            backends=[b.model_dump() for b in data.backends],
            settings=data.settings or None,
            operator_org_id=user.organization_id,
            operator_user_id=user.id,
        )
    except org_provisioning.OrganizationNameTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"field": "name", "message": str(exc)},
        ) from None
    except org_provisioning.FirstUserConflictError as exc:
        # The service conflicts on the identity it was told to use: an
        # explicit external_id, or the email when that is the identity. The
        # field in the body has to match, so the control plane knows which
        # input to change.
        field = "external_id" if first and first.external_id else "owner_email"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"field": field, "message": str(exc)},
        ) from None
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
        providers_configured=result.providers_configured,
        backends_created=result.backends_created,
        models_discovered=result.models_discovered,
        settings_applied=result.settings_applied,
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
    "/instance/usage/resources",
    response_model=UsageReport,
)
async def get_instance_usage_report(
    user: HyperadminUser,
    starts_at: datetime | None = Query(
        default=None, description="Only intervals starting at or after this time"
    ),
    ends_at: datetime | None = Query(
        default=None, description="Only intervals starting before this time"
    ),
    include_seats: bool = Query(
        default=True, description="Include live seat counts per tenant"
    ),
) -> UsageReport:
    """Every tenant's usage quantities for a period.

    Requires hyperadmin role. This is the call a usage run makes: one request
    returns seats and resource consumption for every tenant, so a charge report cannot
    be built from a partially-read view.

    Two different time semantics apply, on purpose:

    * **Resource usage is period-scoped.** Rows come from the append-only ledger
      filtered by ``starts_at``/``ends_at``, and they survive tenant deletion —
      an offboarded tenant still appears with ``status="deleted"`` and
      ``seats=null`` because its final period is still chargeable.
    * **Seats are a live count.** Seat count means "who is on the account right
      now", not "who was here in March", so ``starts_at``/``ends_at`` do not
      filter it.

    Pass ``include_seats=false`` for a resources-only query, which avoids the
    per-tenant count when only consumption is needed.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    try:
        payload = await usage.usage_report(
            pool,
            starts_at=starts_at,
            ends_at=ends_at,
            include_seats=include_seats,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=str(exc)
        ) from None

    return UsageReport(**payload)


@router.get(
    "/instance/usage/backfill-preview",
    response_model=UsageBackfillResult,
)
async def preview_usage_backfill(
    user: HyperadminUser,
    organization_id: UUID | None = Query(default=None),
) -> UsageBackfillResult:
    """Report what historical usage a backfill would reconstruct, writing nothing.

    Requires hyperadmin role. Usage recorded before the ledger existed survives
    only as ``sandboxes.config`` plus timestamps; this shows how many finished
    sandboxes would be recoverable before committing to the write.

    Declared above ``/{organization_id}/...`` routes because the literal
    ``instance`` segment would otherwise be parsed as an organization id.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    pool = await get_pool()
    result = await usage.backfill_from_sandboxes(
        pool, organization_id=organization_id, dry_run=True
    )
    return UsageBackfillResult(**result)


@router.post(
    "/instance/usage/backfill",
    response_model=UsageBackfillResult,
)
async def run_usage_backfill(
    user: HyperadminUser,
    organization_id: UUID | None = Query(default=None),
) -> UsageBackfillResult:
    """Reconstruct ledger rows for sandboxes that finished before the ledger existed.

    Requires hyperadmin role. Idempotent: the ledger's unique key plus
    first-close-wins means a re-run cannot double-count, and sandboxes already
    closed by live teardown are skipped.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    pool = await get_pool()
    result = await usage.backfill_from_sandboxes(
        pool, organization_id=organization_id, dry_run=False
    )
    logger.info(
        "Usage backfill by %s: recorded=%s skipped=%s",
        user.id,
        result.get("recorded"),
        result.get("skipped"),
    )
    return UsageBackfillResult(**result)


@router.get(
    "/instance/setting-locks",
    response_model=InstanceSettingLockResponse,
)
async def monitor_instance_setting_locks(
    user: HyperadminUser,
    key: str | None = Query(
        default=None, description="Only locks on this setting key"
    ),
) -> InstanceSettingLockResponse:
    """Every setting lock across every tenant.

    Requires hyperadmin role. This is the monitoring half of the feature: an
    outside application that assigns tiers needs to answer "which tenants are
    pinned to what", and doing that by iterating tenants over HTTP would be
    O(tenants) round trips for a single dashboard.

    ``db_value`` is the tenant's stored override and is null when the tenant
    inherits the environment or built-in default — the pure "frozen fallback"
    case, which is a fact a monitoring caller needs to see rather than infer.

    Declared here, above every ``/{organization_id}`` route, because a literal
    ``instance`` segment would otherwise be captured by that UUID parameter and
    rejected with a 422.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    payload = await instance_settings.monitor_locks(pool, key=key)

    def to_entry(row: dict[str, Any]) -> InstanceSettingLockEntry:
        definition = runtime_settings.get_runtime_setting_definition(row["key"])
        return InstanceSettingLockEntry(
            organization_id=row["organization_id"],
            organization_name=row.get("organization_name"),
            key=row["key"],
            title=definition.title if definition else None,
            section=definition.section if definition else None,
            reason=row.get("reason") or "",
            db_value=row.get("db_value"),
            locked_by=row.get("locked_by"),
            locked_by_name=row.get("locked_by_name"),
            locked_at=row.get("locked_at"),
        )

    return InstanceSettingLockResponse(
        total=payload["total"],
        locks=[to_entry(row) for row in payload["locks"]],
        by_setting={
            group: [to_entry(row) for row in rows]
            for group, rows in payload["by_setting"].items()
        },
        by_organization={
            group: [to_entry(row) for row in rows]
            for group, rows in payload["by_organization"].items()
        },
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
    Cross-organization reads are gated on the instance-operator permission —
    never on a literal role string, which ``rbac.py`` is the contract for.
    Members probing someone else's organization still get the same 404.
    """
    if organization_id != user.organization_id:
        if not user.has_permission(Permission.ORG_MANAGE_INSTANCE):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )
        is_instance_operator = True
    else:
        is_instance_operator = user.has_permission(Permission.ORG_MANAGE_INSTANCE)

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


@router.put(
    "/{organization_id}/setting-locks/{setting_key:path}",
    response_model=OrganizationSettingLockValue,
)
async def lock_organization_setting(
    organization_id: UUID,
    setting_key: str,
    user: HyperadminUser,
    data: OrganizationSettingLockUpdate | None = None,
) -> OrganizationSettingLockValue:
    """Pin one runtime setting so the tenant can no longer change it.

    Requires hyperadmin role. Locking is the feature, not a modifier on a write:
    the value stays wherever it already lives (a DB row, or the env/default
    fallback) and this only removes the tenant's ability to edit **or reset** it.
    Resetting matters — without it, a tenant could delete the row and take the
    setting back to an instance default that the operator was overriding.

    Re-locking an already locked setting updates the reason and the operator, so
    a pin can be re-justified after a plan change without an unlock in between.

    The path deliberately does **not** live under ``/settings/``: that collection
    already declares ``PUT /settings/{setting_key:path}``, whose greedy path
    parameter would swallow ``/settings/<key>/lock`` whole.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    reason = (data.reason if data else "") or ""
    try:
        result = await instance_settings.lock_setting(
            pool,
            organization_id,
            setting_key,
            reason=reason,
            actor_user_id=user.id,
        )
    except instance_settings.UnknownSettingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    entry = result["entry"]
    await _audit(
        user,
        action=audit_actions.ORG_SETTING_LOCK,
        entity_type="settings",
        entity_id=organization_id,
        entity_label=result["title"],
        target_org_id=organization_id,
        changed_fields=[setting_key],
        old_values={"locked": result["already_locked"], "reason": result["previous_reason"]},
        new_values={"locked": True, "reason": result["reason"]},
        notes=(
            f"Setting {setting_key} re-locked by an instance operator."
            if result["already_locked"]
            else f"Setting {setting_key} locked against tenant changes by an instance operator."
        ),
    )

    return OrganizationSettingLockValue(
        key=entry["key"],
        title=entry["title"],
        section=entry["section"],
        reason=result["reason"],
        locked_by=result["lock"].get("locked_by"),
        locked_by_name=entry.get("locked_by_name"),
        locked_at=result["lock"].get("locked_at"),
        value=entry["value"],
        source=entry["source"],
        display_value=entry["display_value"],
    )


@router.delete(
    "/{organization_id}/setting-locks/{setting_key:path}",
    response_model=OrganizationSettingLockValue,
)
async def unlock_organization_setting(
    organization_id: UUID,
    setting_key: str,
    user: HyperadminUser,
) -> OrganizationSettingLockValue:
    """Release a pinned setting back to the tenant's admins and owners.

    Requires hyperadmin role. The stored value is deliberately left alone:
    unlocking changes *who may edit*, never what the tenant experiences.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    try:
        result = await instance_settings.unlock_setting(
            pool, organization_id, setting_key, actor_user_id=user.id
        )
    except instance_settings.UnknownSettingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except instance_settings.SettingNotLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    entry = result["entry"]
    await _audit(
        user,
        action=audit_actions.ORG_SETTING_UNLOCK,
        entity_type="settings",
        entity_id=organization_id,
        entity_label=result["title"],
        target_org_id=organization_id,
        changed_fields=[setting_key],
        old_values={"locked": True, "reason": result["previous_reason"]},
        new_values={"locked": False},
        notes=f"Setting {setting_key} unlocked by an instance operator.",
    )

    return OrganizationSettingLockValue(
        key=entry["key"],
        title=entry["title"],
        section=entry["section"],
        reason=result["previous_reason"],
        value=entry["value"],
        source=entry["source"],
        display_value=entry["display_value"],
    )


@router.get(
    "/{organization_id}/setting-locks",
    response_model=list[OrganizationSettingLockValue],
)
async def list_organization_setting_locks(
    organization_id: UUID,
    user: HyperadminUser,
) -> list[OrganizationSettingLockValue]:
    """List the settings this tenant's admins cannot change.

    Requires hyperadmin role. Kept separate from ``GET /{id}/settings`` so a
    caller that only cares about "what is pinned here" does not have to load the
    whole catalog, and so the console can render the locked set even when the
    full settings read fails.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    await runtime_settings.load_runtime_setting_locks_from_db(
        pool, organization_id=organization_id
    )
    locks = runtime_settings.runtime_setting_locks(organization_id)

    items: list[OrganizationSettingLockValue] = []
    for key, lock in sorted(locks.items()):
        definition = runtime_settings.get_runtime_setting_definition(key)
        value = runtime_settings.get_runtime_setting(key, organization_id=organization_id)
        items.append(
            OrganizationSettingLockValue(
                key=key,
                title=definition.title if definition else None,
                section=definition.section if definition else None,
                reason=lock.get("reason") or "",
                locked_by=lock.get("locked_by"),
                locked_by_name=lock.get("locked_by_name"),
                locked_at=lock.get("locked_at"),
                value=value,
                source=runtime_settings.get_runtime_setting_source(
                    key, organization_id=organization_id
                ),
                display_value=(
                    runtime_settings._display_value(definition, value)  # noqa: SLF001
                    if definition
                    else str(value)
                ),
            )
        )
    return items


@router.put(
    "/{organization_id}/setting-locks",
    response_model=OrganizationSettingLockBulkResult,
)
async def apply_organization_setting_locks(
    organization_id: UUID,
    data: OrganizationSettingLockBulkRequest,
    user: HyperadminUser,
) -> OrganizationSettingLockBulkResult:
    """Declare this tenant's complete lock set and converge to it.

    Requires hyperadmin role. This is the endpoint a tier application drives:
    post the settings a plan pins and the server locks what is missing,
    re-reasons what changed, and (unless ``release_unlisted`` is false) releases
    what the plan no longer includes. Re-posting the same body is a no-op, which
    is what makes it safe to retry from a webhook.

    Partial failure is reported per setting rather than aborting: a tier naming
    one retired setting still applies the rest, so a catalog change cannot brick
    the caller's whole flow. ``dry_run`` returns the same diff shape without
    writing, for pre-flighting a plan change.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    try:
        result = await instance_settings.apply_lock_set(
            pool,
            organization_id,
            entries=[entry.model_dump() for entry in data.locks],
            actor_user_id=user.id,
            release_unlisted=data.release_unlisted,
            dry_run=data.dry_run,
        )
    except instance_settings.UnknownOrganizationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    if data.dry_run:
        # A dry run changed nothing, so it must not write an audit row.
        return OrganizationSettingLockBulkResult(**result)

    changed = (
        result["locked"]
        + result["updated"]
        + result["values_written"]
        + result["released"]
    )
    if changed or result["failed"]:
        await _audit(
            user,
            action=audit_actions.ORG_SETTING_LOCK,
            entity_type="settings",
            entity_id=organization_id,
            entity_label="setting locks (bulk)",
            target_org_id=organization_id,
            changed_fields=sorted(set(changed)),
            old_values=None,
            new_values={
                "locked": result["locked"],
                "updated": result["updated"],
                "values_written": result["values_written"],
                "released": result["released"],
                "reasons": {
                    entry.key: entry.reason
                    for entry in data.locks
                    if entry.key in result["locked"] + result["updated"]
                },
            },
            notes=(
                f"{len(result['locked'])} lock(s) applied, "
                f"{len(result['updated'])} re-reasoned, "
                f"{len(result['values_written'])} value(s) written, "
                f"{len(result['released'])} released by an instance operator."
                + (
                    f" {len(result['failed'])} key(s) rejected: "
                    + ", ".join(str(item['key']) for item in result["failed"])
                    if result["failed"]
                    else ""
                )
            ),
        )

    return OrganizationSettingLockBulkResult(**result)


@router.post(
    "/{organization_id}/setting-locks/release",
    response_model=OrganizationSettingLockBulkResult,
)
async def release_organization_setting_locks(
    organization_id: UUID,
    data: OrganizationSettingLockReleaseRequest,
    user: HyperadminUser,
) -> OrganizationSettingLockBulkResult:
    """Release a named set of locks.

    Requires hyperadmin role. Unlike ``PUT /{id}/setting-locks`` this does not
    reconcile: exactly these keys are released, so a pin an operator placed by
    hand survives. That is the right shape for "undo this tier".

    Idempotent — a key that was not locked is reported in ``not_locked`` rather
    than treated as an error, so releasing the same set twice is safe.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    try:
        result = await instance_settings.release_lock_set(
            pool, organization_id, keys=data.keys
        )
    except instance_settings.UnknownSettingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    if result["released"]:
        await _audit(
            user,
            action=audit_actions.ORG_SETTING_UNLOCK,
            entity_type="settings",
            entity_id=organization_id,
            entity_label="setting locks (bulk release)",
            target_org_id=organization_id,
            changed_fields=result["released"],
            old_values={"locked": result["released"]},
            new_values={"locked": []},
            notes=(
                f"{len(result['released'])} lock(s) released by an instance operator."
            ),
        )

    return OrganizationSettingLockBulkResult(
        organization_id=organization_id,
        released=result["released"],
        not_locked=result["not_locked"],
    )


@router.get(
    "/{organization_id}/usage/resources",
    response_model=UsageOrganizationSummary,
)
async def get_organization_usage(
    organization_id: UUID,
    user: HyperadminUser,
    starts_at: datetime | None = Query(default=None),
    ends_at: datetime | None = Query(default=None),
    include_seats: bool = Query(default=True),
) -> UsageOrganizationSummary:
    """One tenant's usage quantities: seats and resource consumption.

    Requires hyperadmin role. Deliberately one request rather than "count users,
    then page through sandboxes", because a usage run against a stale or partial
    view is how charges end up disagreeing with the usage report.

    ``resources_by_source`` splits consumption into ``task``, ``managed_tool``,
    and ``interactive`` so a provider that prices custom tooling differently can
    rate them separately.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    try:
        payload = await usage.organization_usage(
            pool,
            organization_id,
            starts_at=starts_at,
            ends_at=ends_at,
            include_seats=include_seats,
        )
    except usage.UnknownOrganizationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=str(exc)
        ) from None

    return UsageOrganizationSummary(**payload)


@router.get(
    "/{organization_id}/usage/ledger",
    response_model=list[dict[str, Any]],
)
async def list_organization_usage_ledger(
    organization_id: UUID,
    user: HyperadminUser,
    starts_at: datetime | None = Query(default=None),
    ends_at: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """The raw ledger rows behind a tenant's totals.

    Requires hyperadmin role. This exists so a disputed charge can be traced to
    specific intervals — which sandbox, what it was for, and how long it held
    its CPU and memory — rather than being reconciled against a summary number.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    pool = await get_pool()
    rows = await ResourceUsageRepository(pool).list_events(
        organization_id,
        starts_at=starts_at,
        ends_at=ends_at,
        limit=limit,
        offset=offset,
    )
    return rows


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
    "/{organization_id}/invitations",
    response_model=OrganizationInvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_invitation(
    organization_id: UUID,
    data: OrganizationInvitationCreate,
    user: HyperadminUser,
) -> OrganizationInvitationCreated:
    """Mint a shareable invitation link for any organization.

    Requires hyperadmin role. This is the operator-side counterpart to the
    members-page invite button: it produces the same single-use, expiring link
    without requiring the operator to sign in to the tenant.

    The plaintext link is returned exactly once. Only its hash is stored, so
    there is no way to retrieve it later — revoke and reissue instead.
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
            detail=f"You cannot invite someone with the '{data.role}' role",
        )

    pool = await get_pool()
    try:
        record, token = await identity_service.create_invitation(
            pool,
            organization_id=organization_id,
            email=data.email,
            role=data.role,
            invited_by=user.id,
            # The hyperadmin's own role is the ceiling. `can_assign_role`
            # already refuses anything above it, so passing the inviter's role
            # here keeps the check honest rather than bypassable.
            inviter_role=user.role,
            ttl_hours=data.ttl_hours,
            note=data.note,
        )
    except identity_service.InvitationError as exc:
        message = str(exc)
        status_code = (
            status.HTTP_409_CONFLICT
            if "already" in message.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=message) from None

    await _audit(
        user,
        action=audit_actions.INVITATION_CREATE,
        entity_type="invitation",
        entity_id=record["id"],
        entity_label=data.email,
        target_org_id=organization_id,
        changed_fields=["invitation"],
        new_values={
            "email": data.email,
            "role": data.role,
            "expires_at": str(record["expires_at"]),
            "organization": org["name"],
        },
        notes="Invitation link created by an instance operator.",
    )

    # The base URL is taken from configuration rather than the request, because
    # this endpoint is called by a server (an external signup site), where the
    # request's own host is not the URL a browser should open.
    from lucent.public_url import public_base_url

    base_url = public_base_url()

    return OrganizationInvitationCreated(
        id=record["id"],
        email=record["email"],
        role=record["role"],
        expires_at=record["expires_at"],
        invite_url=f"{base_url}/invite/{token}",
    )


@router.get(
    "/{organization_id}/invitations",
    response_model=OrganizationInvitationList,
)
async def list_organization_invitations(
    organization_id: UUID,
    user: HyperadminUser,
) -> OrganizationInvitationList:
    """List pending (unaccepted, unrevoked, unexpired) invitations."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    from lucent.db import InvitationRepository

    pool = await get_pool()
    records = await InvitationRepository(pool).list_pending(organization_id)
    return OrganizationInvitationList(
        invitations=[
            OrganizationInvitationSummary(
                id=record["id"],
                email=record["email"],
                role=record["role"],
                expires_at=record["expires_at"],
                created_at=record["created_at"],
                note=record.get("note"),
            )
            for record in records
        ],
        total_count=len(records),
    )


@router.delete(
    "/{organization_id}/invitations/{invitation_id}",
    response_model=SuccessResponse,
)
async def revoke_organization_invitation(
    organization_id: UUID,
    invitation_id: UUID,
    user: HyperadminUser,
) -> SuccessResponse:
    """Revoke a pending invitation so its link stops working immediately."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)

    from lucent.db import InvitationRepository

    pool = await get_pool()
    revoked = await InvitationRepository(pool).revoke(
        invitation_id, organization_id=organization_id
    )
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No pending invitation with that id in this organization",
        )

    await _audit(
        user,
        action=audit_actions.INVITATION_REVOKE,
        entity_type="invitation",
        entity_id=invitation_id,
        target_org_id=organization_id,
        notes="Invitation revoked by an instance operator.",
    )
    return SuccessResponse(success=True, message="Invitation revoked")


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


# ===========================================================================
# Per-organization LLM providers and models
#
# The tenant-facing admin API (/api/admin/backends, /api/admin/models) resolves
# the organization from the *caller's* session. An instance operator configuring
# someone else's workspace needs to name the target explicitly, which is what
# these routes do. Everything is written with the target organization_id, so a
# credential or model can never leak into another tenant.
#
# Two separate concepts live here, deliberately:
#   * provider CREDENTIALS — the API key the engines actually read, stored at the
#     canonical secret key for that provider;
#   * engine BACKENDS — optional routing overrides (base URL, gateway, pinned
#     model). A backend alone does not make a provider usable.
# ===========================================================================


# --- Instance account lookup -----------------------------------------------


@router.get(
    "/instance/users/by-email",
    response_model=AccountLookupResponse,
)
async def find_accounts_by_email(
    user: HyperadminUser,
    email: str = Query(..., min_length=3, max_length=320),
) -> AccountLookupResponse:
    """Does this email already have an account, on any tenant?

    Requires hyperadmin role. The external signup plane asks this before
    provisioning: the same email may hold an account in several organizations,
    and each one is returned with its tenant context so the control plane can
    point the person at an existing tenant instead of silently creating a
    duplicate identity. Matching is case-insensitive, like the login lookup,
    and daemon service identities are excluded.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    pool = await get_pool()
    normalized = email.strip().lower()
    matches = await UserRepository(pool).find_accounts_by_email(normalized)

    return AccountLookupResponse(
        email=normalized,
        accounts=[
            AccountLookupMatch(
                user_id=match["id"],
                email=match["email"],
                display_name=match["display_name"],
                role=match["role"],
                is_active=match["is_active"],
                email_verified=match["email_verified"],
                created_at=match["created_at"],
                organization_id=match["organization_id"],
                organization_name=match["organization_name"],
                organization_status=match["organization_status"],
            )
            for match in matches
        ],
    )


@router.get("/instance/providers")
async def list_available_providers(user: HyperadminUser) -> dict[str, Any]:
    """Providers an operator can configure a credential for."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    return {"items": instance_providers.available_providers()}


@router.get("/instance/runtime")
async def get_instance_runtime(user: HyperadminUser) -> dict[str, Any]:
    """The instance's LLM engine and the providers it can actually run.

    A new tenant has no models of its own, and a *shared* model row cannot be
    granted to one: ``models`` is keyed by ``id`` alone, so "make this row
    usable by one tenant" is not expressible without moving the row, which
    would take it away from every other tenant. That is why a create-time model
    picker over the shared catalogue is wrong, and why this endpoint answers
    the question that can honestly be answered instead: **which providers will
    this instance's engine run**.

    Given that, the tenant's model list comes from its own credentials plus a
    discovery run — i.e. from what it actually has, which is exactly what its
    owner can see and change from Settings → Models.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    from lucent.llm.engine_injection import compatible_provider_options
    from lucent.llm.engine_registry import default_engine_for_provider
    from lucent.settings import llm_engine_name

    configured_engine = llm_engine_name()

    providers: list[dict[str, Any]] = []
    for option in compatible_provider_options(configured_engine):
        name = option.get("name") or ""
        providers.append(
            {
                "provider": name,
                "label": option.get("label") or name,
                "engine": default_engine_for_provider(
                    name, configured_default=configured_engine
                ),
            }
        )

    return {
        "configured_engine": configured_engine,
        "providers": providers,
        "provider_names": [p["provider"] for p in providers],
    }


@router.get("/instance/engines", response_model=list[EngineCatalogEntry])
async def list_engines(user: HyperadminUser) -> list[EngineCatalogEntry]:
    """Engines, the providers each accepts, and their config fields."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    return [
        EngineCatalogEntry(
            engine=item["engine"],
            providers=[
                EngineProviderOption(name=p["name"], label=p["label"])
                for p in item["providers"]
            ],
            fields=[CredentialFieldSpec(**f) for f in item["fields"]],
            availability=item["availability"],
        )
        for item in instance_providers.available_engines()
    ]


@router.get("/instance/settings-schema")
async def get_instance_settings_schema(user: HyperadminUser) -> dict[str, Any]:
    """Every operator-editable setting, for provisioning a tenant that has none.

    The tenant settings endpoint reads an organization's *values*, so it cannot
    describe a workspace that does not exist yet. This returns the shape only —
    key, type, default, bounds, choices — which is all the create form needs.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)

    sections: dict[str, list[dict[str, Any]]] = {}
    for definition in runtime_settings.runtime_setting_definitions():
        if not definition.editable:
            continue
        sections.setdefault(definition.section, []).append(
            {
                "key": definition.key,
                "title": definition.title,
                "value_type": definition.value_type,
                "default": definition.default,
                "choices": list(definition.choices),
                "min_value": definition.min_value,
                "max_value": definition.max_value,
                "description": definition.description,
                # A model-backed setting can be filled in now, but a brand new
                # workspace has no models installed yet, so the console renders
                # it as free text rather than an empty dropdown.
                "option_source": definition.option_source,
            }
        )

    return {
        "sections": [
            {"section": section, "settings": entries}
            for section, entries in sections.items()
        ],
        "count": sum(len(entries) for entries in sections.values()),
    }


@router.get("/{organization_id}/credentials")
async def list_organization_credentials(
    organization_id: UUID,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Which provider credentials a tenant has. Values are never returned."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()
    return {
        "items": await instance_providers.list_credentials(pool, organization_id)
    }


@router.put("/{organization_id}/credentials/{provider}")
async def set_organization_credential(
    organization_id: UUID,
    provider: str,
    data: OrganizationCredentialUpdate,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Store a provider API key for a tenant.

    This writes the canonical secret the engines read, which is what makes the
    provider usable by that tenant.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    org = await _require_org(organization_id)
    pool = await get_pool()

    try:
        result = await instance_providers.set_credential(
            pool, organization_id, provider=provider, value=data.value
        )
    except instance_providers.ProviderConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    await _audit(
        user,
        action="org.provider_credential_set",
        entity_type="provider_credential",
        entity_id=None,
        entity_label=provider,
        target_org_id=organization_id,
        changed_fields=[f"{provider}.api_key"],
        new_values={"provider": provider, "organization": org["name"], "configured": True},
        notes="Provider credential stored by an instance operator (value not logged).",
    )
    return result


@router.delete("/{organization_id}/credentials/{provider}")
async def clear_organization_credential(
    organization_id: UUID,
    provider: str,
    user: HyperadminUser,
) -> SuccessResponse:
    """Remove a tenant's provider API key so an env fallback can apply."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()

    try:
        await instance_providers.clear_credential(
            pool, organization_id, provider=provider
        )
    except instance_providers.ProviderConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    await _audit(
        user,
        action="org.provider_credential_clear",
        entity_type="provider_credential",
        entity_label=provider,
        target_org_id=organization_id,
        changed_fields=[f"{provider}.api_key"],
        new_values={"provider": provider, "configured": False},
        notes="Provider credential cleared by an instance operator.",
    )
    return SuccessResponse(success=True, message=f"{provider} credential cleared")


@router.post("/{organization_id}/providers/discover")
async def discover_organization_models(
    organization_id: UUID,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Register the models a tenant's configured providers expose.

    This is the step that turns a stored credential into selectable models.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    org = await _require_org(organization_id)
    pool = await get_pool()

    try:
        result = await instance_providers.discover_models(pool, organization_id)
    except Exception as exc:  # noqa: BLE001 — provider-side failure
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Model discovery failed: {exc}",
        ) from None

    await _audit(
        user,
        action="org.model_discover",
        entity_type="model",
        entity_label=org["name"],
        target_org_id=organization_id,
        changed_fields=["models"],
        new_values={"discovered": result if isinstance(result, dict) else None},
        notes="Model discovery run by an instance operator.",
    )
    return result


@router.get("/{organization_id}/backends")
async def list_organization_backends(
    organization_id: UUID,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Engine backends owned by a tenant (routing overrides)."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()
    return {"items": await instance_providers.list_backends(pool, organization_id)}


@router.post(
    "/{organization_id}/backends",
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_backend(
    organization_id: UUID,
    data: OrganizationBackendCreate,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Create an engine backend (base URL / gateway / pinned model) for a tenant."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    org = await _require_org(organization_id)
    pool = await get_pool()

    try:
        backend = await instance_providers.create_backend(
            pool,
            organization_id,
            name=data.name,
            engine=data.engine,
            provider=data.provider,
            config=data.config,
            secrets=data.secrets,
            is_enabled=data.is_enabled,
        )
    except instance_providers.ProviderConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None

    await _audit(
        user,
        action="org.backend_create",
        entity_type="engine_backend",
        entity_id=backend["id"],
        entity_label=backend["name"],
        target_org_id=organization_id,
        changed_fields=["backend"],
        new_values={
            "engine": backend["engine"],
            "provider": backend["provider"],
            "organization": org["name"],
        },
        notes="Engine backend created by an instance operator.",
    )
    return backend


@router.delete("/{organization_id}/backends/{backend_id}")
async def delete_organization_backend(
    organization_id: UUID,
    backend_id: str,
    user: HyperadminUser,
) -> SuccessResponse:
    """Delete a tenant-owned engine backend."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()

    if not await instance_providers.delete_backend(pool, organization_id, backend_id):
        raise HTTPException(status_code=404, detail="Backend not found for this organization")

    await _audit(
        user,
        action="org.backend_delete",
        entity_type="engine_backend",
        entity_id=backend_id,
        target_org_id=organization_id,
        changed_fields=["backend"],
        notes="Engine backend removed by an instance operator.",
    )
    return SuccessResponse(success=True, message="Backend removed")


@router.get("/{organization_id}/models")
async def list_organization_models(
    organization_id: UUID,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Models defined for or granted to a tenant."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()
    items = await instance_providers.list_models(pool, organization_id)
    return {"items": items, "total_count": len(items)}


@router.post("/{organization_id}/models/{model_id:path}/grant")
async def grant_organization_model(
    organization_id: UUID,
    model_id: str,
    user: HyperadminUser,
) -> SuccessResponse:
    """Make a model usable by one tenant, copying it if it is instance-shared.

    Enabling a shared model is not enough: the clearance that actually permits
    use has to point at an org-owned row. Copying the shared row into the
    organization achieves that without removing it from the catalogue every
    other tenant draws from.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()

    try:
        result = await instance_providers.grant_model_to_org(
            pool, organization_id, model_id, granted_by=user.id
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator verbatim
        raise HTTPException(
            status_code=400, detail=f"{model_id} could not be granted: {exc}"
        ) from exc

    await _audit(
        user,
        action="org.model_grant",
        entity_type="model",
        entity_id=None,
        entity_label=model_id,
        target_org_id=organization_id,
        changed_fields=["granted"],
        new_values={"granted": True, "cloned": result.get("cloned", False)},
        notes="Model granted to the tenant by an instance operator.",
    )
    return SuccessResponse(
        success=True,
        message=(
            f"{model_id} granted to this tenant"
            + (
                f" as {result['model_id']} (copied from the shared registry)"
                if result.get("cloned")
                else ""
            )
        ),
    )


@router.post(
    "/{organization_id}/models",
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_model(
    organization_id: UUID,
    data: OrganizationModelCreate,
    user: HyperadminUser,
) -> dict[str, Any]:
    """Define a model for a tenant and (by default) grant the org read access.

    An enabled model without an org grant is not usable, so the grant defaults on.
    """
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    org = await _require_org(organization_id)
    pool = await get_pool()

    try:
        model = await instance_providers.create_model(
            pool,
            organization_id,
            model_id=data.model_id,
            provider=data.provider,
            name=data.name or data.model_id,
            engine=data.engine,
            category=data.category,
            model_class=data.model_class,
            context_window=data.context_window,
            supports_tools=data.supports_tools,
            supports_vision=data.supports_vision,
            notes=data.notes,
            is_enabled=data.is_enabled,
            grant_to_org=data.grant_to_org,
            granted_by=user.id,
        )
    except instance_providers.ModelConfigError as exc:
        detail = str(exc)
        code = (
            status.HTTP_409_CONFLICT
            if "already exists" in detail
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=detail) from None

    await _audit(
        user,
        action="org.model_create",
        entity_type="model",
        # ``admin_audit_log.entity_id`` is a UUID column, but model ids are
        # free-form strings ("gpt-4.1"), so the id belongs in the label and
        # entity_id stays null.
        entity_id=None,
        entity_label=model["id"],
        target_org_id=organization_id,
        changed_fields=["model"],
        new_values={
            "provider": model.get("provider"),
            "engine": model.get("engine"),
            "organization": org["name"],
            "org_granted": model.get("org_granted"),
        },
        notes="Model defined by an instance operator.",
    )
    return model


@router.patch("/{organization_id}/models/{model_id:path}")
async def toggle_organization_model(
    organization_id: UUID,
    model_id: str,
    data: OrganizationModelToggle,
    user: HyperadminUser,
) -> SuccessResponse:
    """Enable or disable one model for a tenant."""
    user.require_permission(Permission.ORG_MANAGE_INSTANCE)
    await _require_org(organization_id)
    pool = await get_pool()

    updated = await instance_providers.set_model_enabled(
        pool, organization_id, model_id, enabled=data.enabled
    )
    if not updated:
        # Distinguish "this is a shared registry row" from "no such model", since
        # the operator's next step differs: the former needs granting (which
        # adopts it), the latter needs discovery run first.
        visible = await instance_providers.list_models(pool, organization_id)
        shared = any(
            item["id"] == model_id and not item.get("owned", True)
            for item in visible
        )
        if shared:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{model_id} is shared across the instance, not owned by this "
                    "organization. Grant it to this tenant instead — that adopts "
                    "it into the organization and leaves other tenants alone."
                ),
            )
        raise HTTPException(
            status_code=404, detail="Model not found for this organization"
        )

    await _audit(
        user,
        action="org.model_update",
        entity_type="model",
        entity_id=None,
        entity_label=model_id,
        target_org_id=organization_id,
        changed_fields=["is_enabled"],
        new_values={"is_enabled": data.enabled},
        notes="Model enabled/disabled by an instance operator.",
    )
    return SuccessResponse(
        success=True,
        message=f"Model {model_id} {'enabled' if data.enabled else 'disabled'}",
    )
