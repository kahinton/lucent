"""Entitlement gates: the enforcement half of organization entitlements.

Migration 164 added the rows (``organization_entitlements`` — one row per
(organization, capability)), and
:class:`lucent.db.entitlements.EntitlementRepository` owns reads and writes.
This module is what the rows are *for*: the check a gate site calls
immediately before the action the entitlement governs.

Why a shared helper instead of an inlined query at each site
------------------------------------------------------------
Every gate must ask the same question and refuse with the same error, or a
second caller path (an MCP tool next to an API router, the daemon next to a
route) silently redefines the policy. Routing-level decoration cannot provide
that guarantee — only a check in the code path that reaches the action can.
So each gate site calls one of the functions here *inside* its own flow, not
in a global middleware or router dependency that only one router happens to
mount.

Exemption
---------
Infrastructure organizations whose names are prefixed ``__lucent`` are never
gated. The migration backfill deliberately omitted them (it cannot grant the
platform's own organization entitlements — that would be circular), so
default-deny would otherwise shut the platform down under its own gate:
system agents provision their own sandboxes and run managed tool definitions.
This is also how "the daemon is not blocked" holds: the daemon's
infrastructure work runs under the ``__lucent_system__`` organization (see
:func:`lucent.db.organization.OrganizationRepository.list_real_organization_ids`
for the same exclusion), so the gate never fires for it. A daemon acting for
a real tenant organization is *that* organization's action and is subject to
its entitlements — exempting daemon callers by role would let a
non-entitled organization reach sandbox and managed-tool execution entirely
through the daemon, and the gate would never bite.

A missing row for a real organization is denied — the default-deny contract
the migration and repository already encode. Organizations created after
migration 164 need an administrator (or plan machinery) to grant the row.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lucent.db.entitlements import (
    CAPABILITY_MANAGED_TOOLS,
    CAPABILITY_SANDBOXES,
    EntitlementRepository,
)
from lucent.logging import get_logger

logger = get_logger("services.entitlements")

__all__ = [
    "CAPABILITY_MANAGED_TOOLS",
    "CAPABILITY_SANDBOXES",
    "CapabilityDisabledError",
    "capability_gate_message",
    "capability_is_granted",
    "ensure_capability",
    "grant_capability_for_testing",
    "is_infrastructure_organization",
]

# Prefix shared by infrastructure organizations: __lucent_system__,
# __lucent_aci__, and any future platform-scoped org. The migration backfill
# uses the same prefix, so the gate's exemption and the rows' provenance stay
# in step.
INFRASTRUCTURE_ORG_PREFIX = "__lucent"


class CapabilityDisabledError(Exception):
    """A gate refused an organization that lacks the capability entitlement.

    Raised *instead of* a generic authorization failure so the caller — a
    route, an MCP tool, or the sandbox executor — can map it to its own error
    envelope while the message still names the capability and says it is not
    enabled for the organization. Nothing here produces a bare 403.
    """

    def __init__(self, capability: str, organization_id: Any):
        self.capability = capability
        self.organization_id = organization_id
        super().__init__(
            f"The '{capability}' capability is not enabled for this organization"
        )


def _coerce_org_id(organization_id: Any) -> str:
    """Stripped string of the organization id, or "" when there is none."""
    if organization_id is None:
        return ""
    return str(organization_id).strip()


def _is_uuid(value: str) -> bool:
    """Cheap shape check so a non-UUID org id cannot reach the database.

    A malformed id can only come from stale test scaffolding or a bug: no
    organization row can be identified by it, so it is not entitled. Raising
    here would turn a policy question into a 500.
    """
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


async def is_infrastructure_organization(
    pool, organization_id: Any
) -> bool:
    """Whether the id names an ``__lucent%``-prefixed infrastructure org.

    Resolved by name rather than by a static id constant so the exemption
    never depends on one hard-coded UUID, and so the platform orgs stay
    ungovernable by a tenant-facing entitlement write (they are created from
    bootstrap code, not from the org creation route).
    """
    org_id = _coerce_org_id(organization_id)
    if not org_id or not _is_uuid(org_id):
        return False
    from lucent.db.pool import scoped_acquire_on

    try:
        async with scoped_acquire_on(pool, organization_id=org_id) as conn:
            return bool(
                conn and await conn.fetchval(
                    """
                    SELECT name FROM organizations
                    WHERE id = $1 AND name LIKE $2 || '%'
                    """,
                    UUID(org_id),
                    INFRASTRUCTURE_ORG_PREFIX,
                )
            )
    except Exception:  # noqa: BLE001 — an unreachable DB must not unlock gates
        logger.warning(
            "Entitlement infrastructure-org lookup failed for %s; "
            "treating the organization as a real tenant",
            org_id,
            exc_info=True,
        )
        return False


async def capability_is_granted(
    pool,
    organization_id: Any,
    capability: str,
) -> bool:
    """Whether the gate should pass for (organization, capability).

    One query in the common case: entitled organizations answer from the
    row itself and never touch the infrastructure-org lookup. Denied-by-
    default for a real organization with no enabled row; exempt for the
    platform organizations the backfill deliberately excluded.
    """
    org_id = _coerce_org_id(organization_id)
    if not org_id:
        # No organization to charge a capability against: ambient or
        # platform-level callers. Sites that require an org (execution,
        # dispatch) assert the org is present *before* the gate.
        return True
    if not _is_uuid(org_id):
        return False
    repository = EntitlementRepository(pool)
    try:
        if await repository.is_enabled(org_id, capability):
            return True
    except Exception:  # noqa: BLE001 — an unreadable row must not unlock gates
        logger.warning(
            "Entitlement lookup failed for organization %s capability %s; "
            "treating the capability as not granted",
            org_id,
            capability,
            exc_info=True,
        )
        return False
    return await is_infrastructure_organization(pool, org_id)


async def _resolve_pool(pool):
    """The gate pool: the given one, or the process pool at runtime."""
    if pool is not None:
        return pool
    from lucent.db import get_pool

    return await get_pool()


async def ensure_capability(
    pool,
    organization_id: Any,
    capability: str,
) -> None:
    """Raise :class:`CapabilityDisabledError` unless the gate passes.

    Call this immediately before the action it governs — in the executing
    code path itself, not in a router decorator — so a second caller path
    cannot bypass it.
    """
    if not await capability_is_granted(await _resolve_pool(pool), organization_id, capability):
        raise CapabilityDisabledError(capability, organization_id)


async def capability_gate_message(
    organization_id: Any,
    capability: str,
    *,
    pool=None,
) -> str | None:
    """The user-facing refusal message, or ``None`` when the gate passes.

    For MCP tool sites and other flows whose error envelope is a returned
    message rather than an exception: raise nothing, decide once, and return
    the identical text :class:`CapabilityDisabledError` would carry.
    """
    effective_pool = await _resolve_pool(pool)
    if not await capability_is_granted(effective_pool, organization_id, capability):
        return CapabilityDisabledError(capability, organization_id).args[0]
    return None


async def grant_capability_for_testing(  # pragma: no cover - test helper
    organization_id: Any,
    *capabilities: str,
) -> None:
    """Enable capabilities for an organization (test harness convenience).

    Used by integration tests that need an entitled organization; production
    code goes through :meth:`EntitlementRepository.set_entitlement` directly.
    """
    repository = EntitlementRepository(await _resolve_pool(None))
    for capability in capabilities or (CAPABILITY_SANDBOXES, CAPABILITY_MANAGED_TOOLS):
        await repository.set_entitlement(
            str(organization_id), capability, True, source="override"
        )
