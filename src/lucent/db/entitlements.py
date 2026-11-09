"""Organization entitlements: the storage half of the capability gate.

One row per (organization, capability) says whether the organization may use
that capability. Enforcement lives at the gate sites (sandbox-template and
managed-tool dispatch, the next change); this module owns the rows and the
reads/writes around them, mirroring how :mod:`lucent.db.token_quota` owns
quota policy storage while :mod:`lucent.llm.token_quota` makes decisions.

Design notes
------------
*Every method takes ``organization_id`` explicitly.* No method infers the org
from ambient state (request context, the tenant-scope contextvar on the pool).
The gate sites are exactly where the caller proves its own org, and a
repository that read ambient org state could let a gate site check a different
org's entitlements without either side noticing. The explicit parameter keeps
a single honest signature.

*A missing row means disabled.* ``is_enabled`` returns False when no row
exists — default-deny. The migration backfill (164) is the only code that
turns on entitlements for pre-existing organizations; organizations created
after the migration get rows when an administrator (or later machinery) calls
``set_entitlement``.

*``limits`` is opaque here.* The column is JSONB and its keys belong to the
capability that consumes them (concurrent-sandbox counts, tool-run quotas).
This module stores and returns whatever the caller passes; a consumer that
reads specific keys validates them in its own code.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from asyncpg import Pool

from lucent.db.pool import scoped_acquire_on

logger = logging.getLogger(__name__)

CAPABILITY_SANDBOXES = "custom_sandboxes"
CAPABILITY_MANAGED_TOOLS = "managed_tools"
CAPABILITIES = (CAPABILITY_SANDBOXES, CAPABILITY_MANAGED_TOOLS)

SOURCE_PLAN = "plan"
SOURCE_OVERRIDE = "override"
SOURCE_GRANDFATHERED = "grandfathered"
SOURCES = (SOURCE_PLAN, SOURCE_OVERRIDE, SOURCE_GRANDFATHERED)


class EntitlementRepository:
    """CRUD and reads for ``organization_entitlements``."""

    def __init__(self, pool: Pool):
        self.pool = pool

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """Normalize a database row for JSON transport (strings/JSON types)."""
        limits = row["limits"]
        if isinstance(limits, str):
            limits = json.loads(limits)
        return {
            "id": str(row["id"]),
            "organization_id": str(row["organization_id"]),
            "capability": row["capability"],
            "is_enabled": row["is_enabled"],
            "source": row["source"],
            "limits": limits or {},
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get_entitlements(
        self, organization_id: str | UUID
    ) -> list[dict[str, Any]]:
        """Every capability row for one organization.

        Returns ``[]`` rather than raising when the organization has no rows:
        a pre-backfill or newly-created organization legitimately has none,
        and enforcement on an empty list denies by policy, not by accident.
        """
        async with scoped_acquire_on(
                self.pool, organization_id=organization_id
            ) as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM organization_entitlements
                WHERE organization_id = $1
                ORDER BY capability
                """,
                _uuid(organization_id),
            )
        return [self._row_to_dict(row) for row in rows]

    async def get_entitlements_for_organizations(
        self, organization_ids: list[str | UUID]
    ) -> dict[str, list[dict[str, Any]]]:
        """Entitlement rows for several organizations in one query.

        Serves control-plane listings so an organization table never issues
        one entitlement query per row (N+1). Returns a mapping keyed by
        ``str(organization_id)``; organizations with no rows map to ``[]``
        rather than being absent, so callers never need a default fallback.

        The query spans organizations, so no single tenant scope applies: it
        rides the supplied pool directly, like the cross-org aggregations in
        :class:`lucent.db.resource_usage.ResourceUsageRepository`, and filters
        explicitly with ``organization_id = ANY($1::uuid[])``.
        """
        org_ids = _uuid_list(organization_ids)
        if not org_ids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM organization_entitlements
                WHERE organization_id = ANY($1::uuid[])
                ORDER BY organization_id, capability
                """,
                org_ids,
            )
        grouped: dict[str, list[dict[str, Any]]] = {
            str(org_id): [] for org_id in org_ids
        }
        for row in rows:
            grouped.setdefault(
                str(row["organization_id"]), []
            ).append(self._row_to_dict(row))
        return grouped

    async def is_enabled(
        self, organization_id: str | UUID, capability: str
    ) -> bool:
        """Whether the (org, capability) entitlement exists and is enabled.

        Default-deny: a missing row (or an unknown capability value — the
        database CHECK rejects inserting one, so a typo'd capability name in
        the caller fails loudly on write rather than silently here) is False.
        """
        async with scoped_acquire_on(
                self.pool, organization_id=organization_id
            ) as conn:
            return await conn.fetchval(
                """
                SELECT is_enabled FROM organization_entitlements
                WHERE organization_id = $1 AND capability = $2
                """,
                _uuid(organization_id),
                capability,
            ) is True

    async def set_entitlement(
        self,
        organization_id: str | UUID,
        capability: str,
        is_enabled: bool,
        *,
        source: str = SOURCE_PLAN,
        limits: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Insert or overwrite the (org, capability) row and return it.

        An upsert so flips through 'override' — or writes from the later
        plan machinery, which writes source 'plan' — are one statement and
        cannot be defeated by an insert/unique-constraint race.

        ``limits`` replaces the stored value wholesale, matching update
        semantics elsewhere in this codebase (``token_quota.update_policy``
        sets the columns it is given; nothing merges). Pass the caller's idea
        of the whole limits object, not a delta.
        """
        normalized_limits = limits if limits is not None else {}
        async with scoped_acquire_on(
                self.pool, organization_id=organization_id
            ) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO organization_entitlements
                    (organization_id, capability, is_enabled, source, limits, note)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                ON CONFLICT (organization_id, capability)
                DO UPDATE SET
                    is_enabled = EXCLUDED.is_enabled,
                    source = EXCLUDED.source,
                    limits = EXCLUDED.limits,
                    note = EXCLUDED.note
                RETURNING *
                """,
                _uuid(organization_id),
                capability,
                bool(is_enabled),
                source,
                json.dumps(normalized_limits),
                note,
            )
        if row is None:  # pragma: no cover - RETURNING an upsert always yields
            raise RuntimeError(
                f"Upsert of {capability} for {organization_id} returned no row"
            )
        return self._row_to_dict(row)


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _uuid_list(values: list[str | UUID] | tuple[str | UUID, ...]) -> list[UUID]:
    """UUID objects for ``ANY($1::uuid[])``; blanks are dropped, not errors."""
    result: list[UUID] = []
    for value in values:
        converted = _uuid(value)
        if converted is not None:
            result.append(converted)
    return result
