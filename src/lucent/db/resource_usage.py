"""Database repository for the resource-usage ledger.

The ledger records **closed** usage intervals: one row per sandbox, written when
the resource stops consuming. It is intentionally separate from ``sandboxes``
(which is operational, mutable, and deleted with the tenant) so a usage figure
never moves because an operational row changed or was reaped.

Chargeable totals are *derived* from the primitives here — ``cpu_limit``,
``memory_limit_mb``, ``duration_seconds`` — rather than stored. Storing them
would bake a pricing model into the schema; an outside metering system can apply
whatever rating it wants to the same rows.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg

from lucent.db.pool import runner_guc_preamble, scoped_acquire, tenant_guc_scrub

ResourceSource = Literal["task", "managed_tool", "interactive", "unknown"]
#: How a row ended. Two vocabularies, because the two row types answer different
#: questions: a sandbox interval says how a *reservation* ended, while a tool run
#: says how a *job* turned out. ``completed`` and ``blocked`` are tool-only;
#: ``stopped``/``destroyed``/``expired`` are sandbox-only. ``failed`` and
#: ``unknown`` are shared.
ResourceOutcome = Literal[
    "stopped", "destroyed", "failed", "expired", "unknown",
    "completed", "blocked",
]

_MEMORY_UNITS: dict[str, int] = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}

_MEMORY_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?b?)\s*", re.IGNORECASE)


def parse_memory_limit_mb(value: Any) -> int | None:
    """Convert a Docker/Kubernetes memory limit string to whole mebibytes.

    Accepts the forms Lucent actually stores in ``SandboxConfig.memory_limit``
    and in managed-tool ``resource_limits`` — ``512m``, ``2g``, ``1024``,
    ``1.5g``, ``256MB`` — plus a bare integer, which Docker and Kubernetes both
    read as bytes.

    Returns ``None`` when the value is absent or unrecognized. That is
    deliberately distinct from ``0``: a NULL limit means "we do not know what
    this reservation was", which a usage caller must not silently count as
    free. Guessing a default here would corrupt the one number the ledger
    exists to get right.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Bare numbers are bytes in both backends' dialects.
        return max(0, int(value) // (1024**2))
    text = str(value).strip()
    if not text:
        return None
    match = _MEMORY_RE.fullmatch(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multiplier = _MEMORY_UNITS.get(unit)
    if multiplier is None:
        return None
    return max(0, int(amount * multiplier) // (1024**2))


def _mb_to_bytes(mb: int) -> int:
    return int(mb) * 1024**2


class ResourceUsageRepository:
    """Append-only writes and usage aggregations over the ledger."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # -- writes ------------------------------------------------------------

    async def record_interval(
        self,
        *,
        organization_id: UUID | str | None,
        organization_name: str,
        sandbox_id: str | None = None,
        managed_tool_run_id: UUID | str | None = None,
        tool_id: UUID | str | None = None,
        started_at: datetime,
        ended_at: datetime,
        cpu_limit: float | None = None,
        memory_limit_mb: int | None = None,
        disk_limit: str | None = None,
        source: ResourceSource = "unknown",
        outcome: ResourceOutcome = "unknown",
        task_id: UUID | str | None = None,
        request_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Close one sandbox interval, idempotently.

        Re-recording the same ``sandbox_id`` **updates** the existing row
        instead of inserting a second one. Teardown is reachable more than once
        — ``stop`` followed by ``destroy``, a retried destroy, the auto-destroy
        sweeper racing an operator — and each of those must close the same
        interval rather than bill it twice.

        **The first close wins on the interval.** A sandbox that is stopped at
        10:00 and destroyed at 14:00 consumed resources only until 10:00, so the
        later teardown updates ``outcome`` and fills in missing limits without
        extending ``ended_at``. Extending instead would bill four idle hours.

        The update is deliberately narrow: a repeated teardown that knows *less*
        (a sweeper with no tenant context, a destroy with no config in hand)
        cannot blank out attribution or limits an earlier close already
        recorded.

        Two row types share this table. Pass ``sandbox_id`` for a compute
        reservation; pass ``managed_tool_run_id`` (and ``tool_id``) for one tool
        invocation. A tool row carries **no** cpu/memory — its compute lives on
        its own sandbox row, and duplicating it here would let a careless SUM bill
        the same resources twice.
        """
        if managed_tool_run_id is not None:
            return await self._record_tool_run(
                organization_id=organization_id,
                organization_name=organization_name,
                managed_tool_run_id=managed_tool_run_id,
                tool_id=tool_id,
                started_at=started_at,
                ended_at=ended_at,
                source=source,
                outcome=outcome,
                task_id=task_id,
                request_id=request_id,
            )
        if not sandbox_id:
            raise ValueError(
                "record_interval requires either sandbox_id or managed_tool_run_id"
            )

        duration = max(0, int((ended_at - started_at).total_seconds()))
        query = """
            INSERT INTO resource_usage_events
                (organization_id, organization_name, resource_type, source,
                 sandbox_id, task_id, request_id, managed_tool_run_id,
                 cpu_limit, memory_limit_mb, disk_limit,
                 started_at, ended_at, duration_seconds, outcome)
            VALUES ($1, $2, 'sandbox', $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (sandbox_id) DO UPDATE SET
                organization_id = COALESCE(
                    EXCLUDED.organization_id, resource_usage_events.organization_id
                ),
                -- Keep a known name over an empty one, so a sweeper without
                -- tenant context cannot erase attribution.
                organization_name = CASE
                    WHEN EXCLUDED.organization_name <> ''
                    THEN EXCLUDED.organization_name
                    ELSE resource_usage_events.organization_name
                END,
                source = CASE
                    WHEN EXCLUDED.source = 'unknown'
                    THEN resource_usage_events.source
                    ELSE EXCLUDED.source
                END,
                task_id = COALESCE(EXCLUDED.task_id, resource_usage_events.task_id),
                request_id = COALESCE(EXCLUDED.request_id, resource_usage_events.request_id),
                managed_tool_run_id = COALESCE(
                    EXCLUDED.managed_tool_run_id, resource_usage_events.managed_tool_run_id
                ),
                cpu_limit = COALESCE(EXCLUDED.cpu_limit, resource_usage_events.cpu_limit),
                memory_limit_mb = COALESCE(
                    EXCLUDED.memory_limit_mb, resource_usage_events.memory_limit_mb
                ),
                disk_limit = COALESCE(EXCLUDED.disk_limit, resource_usage_events.disk_limit),
                -- Earliest end wins (see the docstring). Duration is recomputed
                -- from that same expression so the two can never disagree.
                ended_at = LEAST(EXCLUDED.ended_at, resource_usage_events.ended_at),
                duration_seconds = GREATEST(
                    0,
                    EXTRACT(
                        EPOCH FROM (
                            LEAST(EXCLUDED.ended_at, resource_usage_events.ended_at)
                            - resource_usage_events.started_at
                        )
                    )::bigint
                ),
                outcome = EXCLUDED.outcome
            RETURNING id, organization_id, organization_name, resource_type, source,
                      sandbox_id, task_id, request_id, managed_tool_run_id, tool_id,
                      cpu_limit, memory_limit_mb, disk_limit,
                      started_at, ended_at, duration_seconds, outcome, recorded_at
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                query,
                UUID(str(organization_id)) if organization_id else None,
                organization_name or "",
                source,
                str(sandbox_id),
                UUID(str(task_id)) if task_id else None,
                UUID(str(request_id)) if request_id else None,
                UUID(str(managed_tool_run_id)) if managed_tool_run_id else None,
                cpu_limit,
                memory_limit_mb,
                disk_limit,
                started_at,
                ended_at,
                duration,
                outcome,
            )
        return dict(row)

    async def _record_tool_run(
        self,
        *,
        organization_id: UUID | str | None,
        organization_name: str,
        managed_tool_run_id: UUID | str,
        tool_id: UUID | str | None,
        started_at: datetime,
        ended_at: datetime,
        source: ResourceSource,
        outcome: ResourceOutcome,
        task_id: UUID | str | None,
        request_id: UUID | str | None,
    ) -> dict[str, Any]:
        """Close one tool invocation, keyed on its run id.

        Idempotent on ``managed_tool_run_id`` for the same reason sandbox closes
        are: the executor completes a run on the success path, the failure path,
        and the blocked path, and a retried call must not create a second
        invocable unit.

        The compute columns are forced to NULL. ``record_interval`` already
        refuses to pass them here, but writing NULL explicitly means a future
        caller cannot smuggle a reservation onto an invocation row and inflate the
        cpu/memory totals of its own sandbox.
        """
        duration = max(0, int((ended_at - started_at).total_seconds()))
        query = """
            INSERT INTO resource_usage_events
                (organization_id, organization_name, resource_type, source,
                 sandbox_id, task_id, request_id, managed_tool_run_id, tool_id,
                 cpu_limit, memory_limit_mb, disk_limit,
                 started_at, ended_at, duration_seconds, outcome)
            VALUES ($1, $2, 'managed_tool_run', $3, NULL, $4, $5, $6, $7,
                    NULL, NULL, NULL, $8, $9, $10, $11)
            ON CONFLICT (managed_tool_run_id) WHERE managed_tool_run_id IS NOT NULL
            DO UPDATE SET
                organization_id = COALESCE(
                    EXCLUDED.organization_id, resource_usage_events.organization_id
                ),
                organization_name = CASE
                    WHEN EXCLUDED.organization_name <> ''
                    THEN EXCLUDED.organization_name
                    ELSE resource_usage_events.organization_name
                END,
                tool_id = COALESCE(EXCLUDED.tool_id, resource_usage_events.tool_id),
                task_id = COALESCE(EXCLUDED.task_id, resource_usage_events.task_id),
                request_id = COALESCE(EXCLUDED.request_id, resource_usage_events.request_id),
                -- The final outcome replaces any provisional one, unlike the
                -- sandbox path: a run is completed once, and its last status is
                -- the authoritative one (a "running" placeholder becoming
                -- "completed" is the normal case).
                ended_at = EXCLUDED.ended_at,
                duration_seconds = GREATEST(
                    EXCLUDED.duration_seconds, resource_usage_events.duration_seconds
                ),
                outcome = EXCLUDED.outcome
            RETURNING id, organization_id, organization_name, resource_type, source,
                      sandbox_id, task_id, request_id, managed_tool_run_id, tool_id,
                      cpu_limit, memory_limit_mb, disk_limit,
                      started_at, ended_at, duration_seconds, outcome, recorded_at
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                query,
                UUID(str(organization_id)) if organization_id else None,
                organization_name or "",
                source,
                UUID(str(task_id)) if task_id else None,
                UUID(str(request_id)) if request_id else None,
                UUID(str(managed_tool_run_id)),
                UUID(str(tool_id)) if tool_id else None,
                started_at,
                ended_at,
                duration,
                outcome,
            )
        return dict(row)

    # -- reads -------------------------------------------------------------

    async def list_events(
        self,
        organization_id: UUID | str | None = None,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List ledger rows, optionally scoped to a tenant and period."""
        where: list[str] = []
        params: list[Any] = []
        if organization_id:
            params.append(UUID(str(organization_id)))
            where.append(f"organization_id = ${len(params)}")
        if starts_at:
            params.append(starts_at)
            where.append(f"started_at >= ${len(params)}")
        if ends_at:
            params.append(ends_at)
            where.append(f"started_at < ${len(params)}")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        query = f"""
            SELECT id, organization_id, organization_name, resource_type, source,
                   sandbox_id, task_id, request_id, managed_tool_run_id, tool_id,
                   cpu_limit, memory_limit_mb, disk_limit,
                   started_at, ended_at, duration_seconds, outcome, recorded_at
            FROM resource_usage_events
            {where_sql}
            ORDER BY started_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        if not organization_id:
            # Cross-tenant read: the tenant guard exists to stop tenant work from
            # running unscoped, and this row set spans every tenant by
            # definition (that is how an offboarded tenant's usage is still
            # found). Same preamble the other instance-wide reads use.
            async with self.pool.acquire() as conn:
                await conn.execute(runner_guc_preamble())
                try:
                    rows = await conn.fetch(query, *params, limit, offset)
                finally:
                    await conn.execute(tenant_guc_scrub())
            return [dict(row) for row in rows]

        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(query, *params, limit, offset)
        return [dict(row) for row in rows]

    async def totals_by_organization(
        self,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Usage totals per tenant for a period.

        Returns primitives *and* the two derived quantities a metering system
        actually rates, because every consumer would otherwise recompute them
        and disagree on rounding:

        * ``cpu_seconds``    = SUM(cpu_limit * duration_seconds)
        * ``memory_mb_seconds`` = SUM(memory_limit_mb * duration_seconds)

        ``NULL`` limits contribute nothing to a sum, which is correct: an
        unknown reservation is not chargeable, and the row stays visible in
        ``intervals`` so it can be corrected.

        **Only ``resource_type = 'sandbox'`` rows contribute to compute.** Tool
        invocations describe the same resources as their own sandbox row, so
        including them here would bill every custom-tool execution twice. Their
        count is reported separately as ``tool_invocations``.

        **Grouping survives offboarding.** ``organization_id`` is
        ``ON DELETE SET NULL`` so the ledger outlives the tenant, which means
        every offboarded tenant has a NULL id. Grouping on the id alone would
        collapse all of them into one NULL row and merge several companies'
        final periods into a single billable line. The tenant identity the
        schema deliberately snapshots is ``organization_name``, so a NULL id
        groups by that name instead: a live tenant still gets one row per id,
        and each offboarded tenant keeps its own.
        """
        params: list[Any] = []
        period: list[str] = []
        if starts_at:
            params.append(starts_at)
            period.append(f"started_at >= ${len(params)}")
        if ends_at:
            params.append(ends_at)
            period.append(f"started_at < ${len(params)}")
        period_sql = f" AND {' AND '.join(period)}" if period else ""
        query = f"""
            SELECT organization_id,
                   MAX(organization_name) AS organization_name,
                   COUNT(*) FILTER (
                       WHERE resource_type = 'sandbox'
                   ) AS intervals,
                   COALESCE(SUM(duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ), 0) AS duration_seconds,
                   COALESCE(ROUND(SUM(cpu_limit * duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ), 3), 0) AS cpu_seconds,
                   COALESCE(SUM(memory_limit_mb * duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ), 0) AS memory_mb_seconds,
                   COALESCE(SUM(cpu_limit * duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ) / 3600.0, 0) AS cpu_hours,
                   COUNT(*) FILTER (
                       WHERE resource_type = 'managed_tool_run'
                   ) AS tool_invocations
            FROM resource_usage_events
            WHERE TRUE{period_sql}
            GROUP BY organization_id,
                     CASE WHEN organization_id IS NULL
                          THEN organization_name END
            ORDER BY cpu_seconds DESC, duration_seconds DESC
        """
        # Cross-tenant aggregation: spans every tenant by definition, so it
        # needs the explicit system context rather than the tenant guard.
        async with self.pool.acquire() as conn:
            await conn.execute(runner_guc_preamble())
            try:
                rows = await conn.fetch(query, *params)
            finally:
                await conn.execute(tenant_guc_scrub())
        return [dict(row) for row in rows]

    async def totals_for_organization(
        self,
        organization_id: UUID | str,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Usage totals for one tenant, broken down by source and by tool.

        Same double-counting rule as :meth:`totals_by_organization`: only
        ``sandbox`` rows feed the compute columns, and tool invocations are
        counted separately in ``tool_invocations`` (overall and per tool).
        """
        where = ["organization_id = $1"]
        params: list[Any] = [UUID(str(organization_id))]
        if starts_at:
            params.append(starts_at)
            where.append(f"started_at >= ${len(params)}")
        if ends_at:
            params.append(ends_at)
            where.append(f"started_at < ${len(params)}")
        where_sql = " AND ".join(where)

        summary_q = f"""
            SELECT COUNT(*) FILTER (WHERE resource_type = 'sandbox')
                       AS intervals,
                   COALESCE(SUM(duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ), 0) AS duration_seconds,
                   COALESCE(ROUND(SUM(cpu_limit * duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ), 3), 0) AS cpu_seconds,
                   COALESCE(SUM(memory_limit_mb * duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ), 0) AS memory_mb_seconds,
                   COALESCE(SUM(cpu_limit * duration_seconds) FILTER (
                       WHERE resource_type = 'sandbox'
                   ) / 3600.0, 0) AS cpu_hours,
                   COUNT(*) FILTER (WHERE resource_type = 'managed_tool_run')
                       AS tool_invocations,
                   COALESCE(SUM(duration_seconds) FILTER (
                       WHERE resource_type = 'managed_tool_run'
                   ), 0) AS tool_duration_seconds
            FROM resource_usage_events
            WHERE {where_sql}
        """
        by_source_q = f"""
            SELECT source,
                   COUNT(*) AS intervals,
                   COALESCE(SUM(duration_seconds), 0) AS duration_seconds,
                   COALESCE(ROUND(SUM(cpu_limit * duration_seconds), 3), 0)
                       AS cpu_seconds,
                   COALESCE(SUM(memory_limit_mb * duration_seconds), 0)
                       AS memory_mb_seconds
            FROM resource_usage_events
            WHERE {where_sql} AND resource_type = 'sandbox'
            GROUP BY source
            ORDER BY cpu_seconds DESC
        """
        # Per-tool invocation counts, for a provider that prices tool usage
        # individually rather than blending it into compute.
        #
        # "succeeded" uses the TOOL vocabulary (`completed`), not the sandbox one.
        # These rows only ever hold tool runs, so counting stopped/destroyed here
        # would report every successful execution as a failure — which is exactly
        # the bug this filter used to have.
        by_tool_q = f"""
            SELECT tool_id,
                   COUNT(*) AS invocations,
                   COUNT(*) FILTER (WHERE outcome = 'completed') AS succeeded,
                   COALESCE(SUM(duration_seconds), 0) AS duration_seconds
            FROM resource_usage_events
            WHERE {where_sql}
              AND resource_type = 'managed_tool_run'
              AND tool_id IS NOT NULL
            GROUP BY tool_id
            ORDER BY invocations DESC
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            summary = await conn.fetchrow(summary_q, *params)
            by_source = await conn.fetch(by_source_q, *params)
            by_tool = await conn.fetch(by_tool_q, *params)
        return {
            "summary": dict(summary) if summary else {},
            "by_source": [dict(row) for row in by_source],
            "by_tool": [dict(row) for row in by_tool],
        }
