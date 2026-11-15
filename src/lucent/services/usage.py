"""Usage reporting for instance operators.

Two distinct things live here, and the distinction matters:

* **Counting tenants** — how many *metered people* an organization has. This is
  a live count, not history: seat count is "who is on the account right now".
* **Resource consumption** — how much sandbox CPU and memory was reserved, and
  for how long. This is history read from the append-only ledger
  (``resource_usage_events``), because a usage figure must not move when the
  operational rows that produced it are cleaned up.

Everything here is hyperadmin-scoped and cross-tenant by nature, so each function
takes or implies an explicit tenant rather than falling back to the caller's
context.

A note on what is deliberately **not** here: no pricing, no plan, no charges, no
tier. Lucent records *quantities*; the application that sells the product decides
what they cost. Anything that looked like a rate would go stale the moment a
price changed and would have to be re-derived from history anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.organization import OrganizationRepository
from lucent.db.pool import runner_guc_preamble, tenant_guc_scrub
from lucent.db.resource_usage import ResourceUsageRepository, parse_memory_limit_mb
from lucent.logging import get_logger

logger = get_logger(__name__)

#: Roles that are never metered seats. `daemon` is Lucent's own service
#: identity (one per organization, created with the org); `hyperadmin` is
#: instance-operator staff, not a customer. Counting either would inflate every
#: charge report and, worse, make the seat count depend on internal machinery.
NON_METERED_ROLES = ("daemon", "hyperadmin")


class UnknownOrganizationError(ValueError):
    """The target organization does not exist."""


async def count_metered_users(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    active_only: bool = True,
) -> dict[str, Any]:
    """Count the people on an organization for seat-based usage.

    Args:
        active_only: Count only active members. Seats are normally billed for
            people who can actually sign in; pass False for "everyone ever
            created here".

    Returns:
        ``{"total", "active", "metered", "by_role"}``. ``metered`` is the
        number to count: active human members, excluding service identities
        and instance-operator staff.
    """
    org_id = UUID(str(organization_id))
    query = """
        SELECT role, is_active, COUNT(*) AS count
        FROM users
        WHERE organization_id = $1
          AND role <> ALL($2::text[])
        GROUP BY role, is_active
    """
    async with pool.acquire() as conn:
        await conn.execute(runner_guc_preamble())
        try:
            rows = await conn.fetch(query, org_id, list(NON_METERED_ROLES))
        finally:
            await conn.execute(tenant_guc_scrub())

    by_role: dict[str, int] = {}
    active_total = 0
    all_total = 0
    for row in rows:
        count = int(row["count"])
        all_total += count
        by_role[row["role"]] = by_role.get(row["role"], 0) + count
        if row["is_active"]:
            active_total += count

    return {
        "organization_id": org_id,
        "total": all_total,
        "active": active_total,
        "metered": active_total if active_only else all_total,
        "active_only": active_only,
        "excluded_roles": list(NON_METERED_ROLES),
        "by_role": by_role,
    }


async def organization_usage(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    include_seats: bool = True,
) -> dict[str, Any]:
    """One tenant's usage quantities: seats and resource consumption.

    This is the call a usage run makes per tenant. It is deliberately one
    request rather than "count users, then page through sandboxes", because a
    usage run against a stale or partially-read view is how charges end up
    disagreeing with the usage report.

    Raises:
        UnknownOrganizationError: If the organization does not exist.
    """
    org_id = UUID(str(organization_id))
    org = await OrganizationRepository(pool).get_by_id(org_id)
    if org is None:
        raise UnknownOrganizationError(f"Organization {org_id} not found.")

    repo = ResourceUsageRepository(pool)
    totals = await repo.totals_for_organization(
        org_id, starts_at=starts_at, ends_at=ends_at
    )

    payload: dict[str, Any] = {
        "organization_id": org_id,
        "organization_name": org["name"],
        "status": org["status"],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "resources": totals["summary"],
        "resources_by_source": totals["by_source"],
        # Per-tool invocation counts. Compute is excluded from these rows on
        # purpose: a tool run's CPU and memory live on the sandbox it created,
        # so summing both would bill custom-tool usage twice in one call.
        "tool_usage": totals["by_tool"],
    }
    if include_seats:
        payload["seats"] = await count_metered_users(pool, org_id)
    return payload


async def usage_report(
    pool: asyncpg.Pool,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    include_seats: bool = True,
    limit: int = 500,
) -> dict[str, Any]:
    """Every tenant's usage quantities for a period, plus instance totals.

    The per-tenant rows come from the ledger grouped by organization, so a
    deleted tenant still appears (with its snapshotted name) as long as it has
    usage in the period. Seat counts are live, so a deleted tenant has none —
    which is why ``include_seats`` is separable and the instance totals state
    which period they cover.
    """
    if starts_at and ends_at and ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")

    resource_rows = await ResourceUsageRepository(pool).totals_by_organization(
        starts_at=starts_at, ends_at=ends_at
    )

    tenants: list[dict[str, Any]] = []
    seats_by_org: dict[str, dict[str, Any]] = {}
    if include_seats:
        org_repo = OrganizationRepository(pool)
        result = await org_repo.list(
            limit=limit,
            exclude_names=["__lucent_system__"],
            include_stats=True,
        )
        for org in result["organizations"]:
            counts = await count_metered_users(pool, org["id"])
            seats_by_org[str(org["id"])] = counts
            tenants.append(
                {
                    "organization_id": org["id"],
                    "organization_name": org["name"],
                    "status": org["status"],
                    "seats": counts,
                    "resources": None,
                }
            )

    # Live tenants are keyed by id; offboarded ones have a NULL id (the ledger's
    # FK is ON DELETE SET NULL) and are keyed by the snapshotted name instead,
    # which the totals query groups them by. Keying everything on
    # `str(organization_id)` would put every offboarded tenant under the single
    # key "None" and silently drop all but one of them.
    by_org: dict[str, dict[str, Any]] = {}
    offboarded: list[dict[str, Any]] = []
    for row in resource_rows:
        if row.get("organization_id") is None:
            offboarded.append(row)
        else:
            by_org[str(row["organization_id"])] = row

    known = {t["organization_id"] and str(t["organization_id"]) for t in tenants}

    for tenant in tenants:
        usage = by_org.get(str(tenant["organization_id"]))
        if usage:
            tenant["resources"] = {
                key: usage[key]
                for key in (
                    "intervals",
                    "duration_seconds",
                    "cpu_seconds",
                    "cpu_hours",
                    "memory_mb_seconds",
                    "tool_invocations",
                )
            }

    def _resources(usage: dict[str, Any]) -> dict[str, Any]:
        return {
            field: usage[field]
            for field in (
                "intervals",
                "duration_seconds",
                "cpu_seconds",
                "cpu_hours",
                "memory_mb_seconds",
                "tool_invocations",
            )
        }

    # Tenants with usage but no live organization row (offboarded, or scoped out
    # of the seat listing) still need to appear: their period is metered.
    for key, usage in by_org.items():
        if key in known:
            continue
        tenants.append(
            {
                "organization_id": usage["organization_id"],
                "organization_name": usage.get("organization_name"),
                "status": "deleted",
                "seats": None,
                "resources": _resources(usage),
            }
        )

    for usage in offboarded:
        tenants.append(
            {
                # The tenant is gone, so there is no id left to report; the name
                # is the identity the ledger kept.
                "organization_id": None,
                "organization_name": usage.get("organization_name"),
                "status": "deleted",
                "seats": None,
                "resources": _resources(usage),
            }
        )

    instance_totals = {
        "seats": sum(
            int(seats["metered"]) for seats in seats_by_org.values()
        ),
        "intervals": sum(int(row["intervals"]) for row in resource_rows),
        "duration_seconds": sum(int(row["duration_seconds"]) for row in resource_rows),
        "cpu_seconds": round(
            sum(float(row["cpu_seconds"] or 0) for row in resource_rows), 3
        ),
        "cpu_hours": round(sum(float(row["cpu_hours"] or 0) for row in resource_rows), 3),
        "memory_mb_seconds": sum(
            int(row["memory_mb_seconds"] or 0) for row in resource_rows
        ),
        "tool_invocations": sum(
            int(row["tool_invocations"] or 0) for row in resource_rows
        ),
    }

    return {
        "starts_at": starts_at,
        "ends_at": ends_at,
        "instance_totals": instance_totals,
        "tenants": sorted(
            tenants,
            key=lambda t: (
                -(float((t.get("resources") or {}).get("cpu_seconds") or 0)),
                t.get("organization_name") or "",
            ),
        ),
    }


async def record_managed_tool_usage(
    pool: asyncpg.Pool,
    *,
    managed_tool_run_id: str,
    tool_id: str | None,
    organization_id: str | None,
    started_at: datetime | None,
    ended_at: datetime | None = None,
    outcome: str = "unknown",
    task_id: str | None = None,
    request_id: str | None = None,
    organization_name: str | None = None,
) -> dict[str, Any] | None:
    """Close one tool invocation in the ledger.

    This records the **invocation**, not the compute. A managed-tool execution
    already produces a sandbox carrying ``source='managed_tool'``, and that row
    holds the CPU, memory, and duration. Writing a second row with compute here
    would make a naive ``SUM`` bill every custom-tool call twice; the ledger's
    aggregation therefore takes compute only from ``sandbox`` rows and counts
    ``managed_tool_run`` rows as invocations.

    What this row adds is the thing ``managed_tool_runs`` cannot provide: a
    durable per-invocation record that outlives the tenant (that table cascades on
    ``organizations``). A provider metering per call can count these.

    Never raises. Calling paths include the executor's failure and blocked
    branches, and a usage write must not be able to change how a tool error is
    reported to its caller.

    Returns the ledger row, or ``None`` when nothing was recorded.
    """
    if not organization_id or started_at is None:
        logger.debug(
            "Skipping tool usage record for run %s (org=%s, started_at=%s)",
            str(managed_tool_run_id)[:12],
            organization_id,
            started_at,
        )
        return None

    ended = ended_at or datetime.now(UTC)
    if ended < started_at:
        # Clock skew; clamp rather than record a negative interval that the
        # table's CHECK constraint would reject.
        ended = started_at

    try:
        name = organization_name
        if not name:
            org = await OrganizationRepository(pool).get_by_id(organization_id)
            name = (org or {}).get("name") or ""

        return await ResourceUsageRepository(pool).record_interval(
            organization_id=organization_id,
            organization_name=name,
            managed_tool_run_id=managed_tool_run_id,
            tool_id=tool_id,
            started_at=started_at,
            ended_at=ended,
            source="managed_tool",
            outcome=outcome if outcome in {
                "completed", "failed", "blocked", "unknown"
            } else "unknown",
            task_id=task_id,
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 — a usage write must not change tool behaviour
        logger.warning(
            "Failed to record usage for tool run %s",
            str(managed_tool_run_id)[:12],
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Ledger emission
# ---------------------------------------------------------------------------


def _interval_source(config: Any) -> tuple[str, str | None, str | None]:
    """Classify what caused a sandbox to exist.

    Returns ``(source, task_id, request_id)``. A managed-tool sandbox is a task
    sandbox in the schema's eyes (it carries ``task_id`` for the bridge), so it
    must be classified *first* or its usage would be attributed to task work and
    a customer querying "my tool usage" would see nothing.
    """
    task_id = getattr(config, "task_id", None)
    request_id = getattr(config, "request_id", None)
    name = str(getattr(config, "name", "") or "")
    if name.startswith("managed-tool-"):
        return "managed_tool", task_id, request_id
    if task_id:
        return "task", task_id, request_id
    if request_id:
        return "task", task_id, request_id
    return "interactive", task_id, request_id


async def record_sandbox_usage(
    pool: asyncpg.Pool,
    *,
    sandbox_id: str,
    config: Any,
    started_at: datetime | None,
    ended_at: datetime | None = None,
    outcome: str = "unknown",
    organization_name: str | None = None,
) -> dict[str, Any] | None:
    """Close a sandbox's usage interval in the ledger.

    Never raises. This is called from teardown paths — destroy, stop, the idle
    sweeper, the auto-destroy timer — and a usage write must never be able to
    fail a teardown and leak a running container.

    ``started_at`` comes from the sandbox row's ``created_at`` (or
    ``ready_at``), not from process memory, so a teardown in a *different*
    process than the create still produces a correct interval.

    Returns the ledger row, or ``None`` when nothing was recorded (no org, no
    start time, or a write error).
    """
    organization_id = getattr(config, "organization_id", None)
    if not organization_id or started_at is None:
        # A sandbox with no tenant cannot be billed to anyone, and with no start
        # time the interval would be a guess. Both are logged rather than
        # silently billed as zero.
        logger.debug(
            "Skipping usage record for sandbox %s (org=%s, started_at=%s)",
            str(sandbox_id)[:12],
            organization_id,
            started_at,
        )
        return None

    ended = ended_at or datetime.now(UTC)
    if ended < started_at:
        # Clock skew or a stale row; clamp rather than record a negative
        # interval that would fail the CHECK constraint.
        ended = started_at

    source, task_id, request_id = _interval_source(config)
    try:
        name = organization_name
        if not name:
            org = await OrganizationRepository(pool).get_by_id(organization_id)
            name = (org or {}).get("name") or ""

        return await ResourceUsageRepository(pool).record_interval(
            organization_id=organization_id,
            organization_name=name,
            sandbox_id=str(sandbox_id),
            started_at=started_at,
            ended_at=ended,
            cpu_limit=float(getattr(config, "cpu_limit", 0) or 0),
            memory_limit_mb=parse_memory_limit_mb(
                getattr(config, "memory_limit", None)
            ),
            disk_limit=getattr(config, "disk_limit", None),
            source=source,
            outcome=outcome if outcome in {
                "stopped", "destroyed", "failed", "expired", "unknown"
            } else "unknown",
            task_id=task_id,
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 — a usage write must not fail a teardown
        logger.warning(
            "Failed to record resource usage for sandbox %s",
            str(sandbox_id)[:12],
            exc_info=True,
        )
        return None


async def backfill_from_sandboxes(
    pool: asyncpg.Pool,
    *,
    organization_id: UUID | str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Close intervals for sandboxes that already stopped, from existing rows.

    Historical runs predate the ledger, so their usage exists only as
    ``sandboxes.config`` plus timestamps. This reconstructs those intervals,
    which is what makes the feature useful for a tenant that has been running
    for months rather than only from today.

    Idempotent: the ledger's unique key means a re-run cannot double-count, and
    ``record_interval``'s first-close-wins rule means a sandbox already closed
    by live teardown is left alone.

    Only *finished* sandboxes are considered. A live sandbox has no end yet and
    will be closed by its own teardown, so including it here would write an
    interval that grows every time the backfill runs.

    Args:
        organization_id: Limit to one tenant; omit for the whole instance.
        dry_run: Report what would be written without writing.
    """
    where = ["status IN ('stopped', 'destroyed', 'failed')"]
    params: list[Any] = []
    if organization_id:
        params.append(UUID(str(organization_id)))
        where.append(f"organization_id = ${len(params)}")
    query = f"""
        SELECT id, organization_id, created_at, ready_at, stopped_at,
               destroyed_at, status, config
        FROM sandboxes s
        WHERE {' AND '.join(where)}
          AND NOT EXISTS (
              SELECT 1 FROM resource_usage_events e WHERE e.sandbox_id = s.id::text
          )
        ORDER BY created_at
    """

    async with pool.acquire() as conn:
        await conn.execute(runner_guc_preamble())
        try:
            rows = await conn.fetch(query, *params)
        finally:
            await conn.execute(tenant_guc_scrub())

    reconstructed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        config = row["config"] or {}
        if isinstance(config, str):
            import json

            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        started = row["ready_at"] or row["created_at"]
        ended = row["destroyed_at"] or row["stopped_at"]
        if ended is None:
            skipped.append({"sandbox_id": str(row["id"]), "reason": "no end timestamp"})
            continue
        if row["organization_id"] is None:
            skipped.append({"sandbox_id": str(row["id"]), "reason": "no organization"})
            continue
        reconstructed.append(
            {
                "sandbox_id": str(row["id"]),
                "organization_id": row["organization_id"],
                "started_at": started,
                "ended_at": ended,
                "cpu_limit": config.get("cpu_limit"),
                "memory_limit_mb": parse_memory_limit_mb(config.get("memory_limit")),
                "disk_limit": config.get("disk_limit"),
                "source": (
                    "managed_tool"
                    if str(config.get("name") or "").startswith("managed-tool-")
                    else "task" if config.get("task_id") else "interactive"
                ),
                "outcome": {
                    "stopped": "stopped",
                    "destroyed": "destroyed",
                    "failed": "failed",
                }.get(row["status"], "unknown"),
                "task_id": config.get("task_id"),
                "request_id": config.get("request_id"),
            }
        )

    if dry_run:
        return {
            "dry_run": True,
            "would_record": len(reconstructed),
            "skipped": len(skipped),
            "skipped_detail": skipped[:50],
            "sample": reconstructed[:5],
        }

    repo = ResourceUsageRepository(pool)
    recorded = 0
    for item in reconstructed:
        try:
            name = ""
            org = await OrganizationRepository(pool).get_by_id(item["organization_id"])
            name = (org or {}).get("name") or ""
            await repo.record_interval(
                organization_id=item["organization_id"],
                organization_name=name,
                sandbox_id=item["sandbox_id"],
                started_at=item["started_at"],
                ended_at=item["ended_at"],
                cpu_limit=item["cpu_limit"],
                memory_limit_mb=item["memory_limit_mb"],
                disk_limit=item["disk_limit"],
                source=item["source"],
                outcome=item["outcome"],
                task_id=item["task_id"],
                request_id=item["request_id"],
            )
            recorded += 1
        except Exception:  # noqa: BLE001 — report, keep going
            logger.warning(
                "Backfill failed for sandbox %s", item["sandbox_id"], exc_info=True
            )
            skipped.append({"sandbox_id": item["sandbox_id"], "reason": "write error"})

    return {
        "dry_run": False,
        "recorded": recorded,
        "skipped": len(skipped),
        "skipped_detail": skipped[:50],
    }
