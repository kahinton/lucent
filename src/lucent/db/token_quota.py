"""Token quota policies: configuration and per-request consumption reads.

This module owns the *storage* half of token limits — the policy rows, their
principal assignments, and the aggregations over the ``llm_token_usage``
ledger that say how much budget a principal has already spent. The decision
logic lives in :mod:`lucent.llm.token_quota`, which reads through this
repository.

Design notes
------------
*The ledger is the source of truth for consumption.* ``llm_token_usage`` (108)
already records provider-reported usage per model call, stamped with
``organization_id``, ``user_id``, ``model`` and ``created_at``. Nothing in this
module writes usage; it only aggregates. That keeps one accounting path and
means a quota can never disagree with the Usage report.

*Aggregation is grouped by user, not summed per policy.* A single request can
fall under several policies whose pools cover different sets of people (org
wide, one group, one person). Rather than run one SUM per policy, the caller
fetches per-user totals once per distinct ``(window, model scope)`` pair and
partitions them in Python. In practice that is one or two queries per request.

*Nothing here is cached.* Policy rows are cached briefly by the enforcement
module (they change rarely); consumption is always read live so a check can
never approve against stale numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from asyncpg import Pool
from lucent.db.pool import scoped_acquire

logger = logging.getLogger(__name__)

# Sentinel model id meaning "every model". A sentinel rather than NULL keeps the
# policy row explicit and lets the unique index treat it like any other value.
ALL_MODELS = "*"

PRINCIPAL_ORG = "org"
PRINCIPAL_GROUP = "group"
PRINCIPAL_USER = "user"
PRINCIPAL_TYPES = (PRINCIPAL_ORG, PRINCIPAL_GROUP, PRINCIPAL_USER)

SCOPE_GLOBAL = "global"
SCOPE_INDIVIDUAL = "individual"
SCOPE_MODES = (SCOPE_GLOBAL, SCOPE_INDIVIDUAL)

# Token dimensions a policy can cap. Each maps to a column on the ledger.
DIMENSION_RAW_INPUT = "raw_input"
DIMENSION_CACHED_INPUT = "cached_input"
DIMENSION_OUTPUT = "output"
DIMENSIONS = (DIMENSION_RAW_INPUT, DIMENSION_CACHED_INPUT, DIMENSION_OUTPUT)

_DIMENSION_LIMIT_COLUMNS = {
    DIMENSION_RAW_INPUT: "raw_input_tokens_limit",
    DIMENSION_CACHED_INPUT: "cached_input_tokens_limit",
    DIMENSION_OUTPUT: "output_tokens_limit",
}


def _uuid(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    return UUID(str(value))


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """One budget definition with the shape the enforcement code needs."""

    id: str
    organization_id: str
    model_ids: tuple[str, ...]
    name: str
    description: str
    window_seconds: int
    scope_mode: str
    raw_input_tokens_limit: int | None
    cached_input_tokens_limit: int | None
    output_tokens_limit: int | None
    is_enabled: bool

    def limit_for(self, dimension: str) -> int | None:
        return getattr(self, _DIMENSION_LIMIT_COLUMNS[dimension])

    def limits(self) -> dict[str, int | None]:
        return {dim: self.limit_for(dim) for dim in DIMENSIONS if self.limit_for(dim) is not None}

    @property
    def applies_to_all_models(self) -> bool:
        return ALL_MODELS in self.model_ids

    @property
    def covers_all_models(self) -> bool:
        """Whether this policy's pool spans every model."""
        return self.applies_to_all_models

    def covers(self, model_id: str) -> bool:
        return self.applies_to_all_models or str(model_id) in self.model_ids

    def label(self) -> str:
        """Human summary of the model scope, for messages and reporting."""
        if self.applies_to_all_models:
            return "all models"
        if len(self.model_ids) == 1:
            return f"model '{self.model_ids[0]}'"
        return f"{len(self.model_ids)} models"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "model_ids": list(self.model_ids),
            "name": self.name,
            "description": self.description,
            "window_seconds": self.window_seconds,
            "scope_mode": self.scope_mode,
            "raw_input_tokens_limit": self.raw_input_tokens_limit,
            "cached_input_tokens_limit": self.cached_input_tokens_limit,
            "output_tokens_limit": self.output_tokens_limit,
            "is_enabled": self.is_enabled,
        }


@dataclass(frozen=True, slots=True)
class QuotaAssignment:
    """A policy bound to one principal (org, group, or user)."""

    id: str
    policy_id: str
    principal_type: str
    principal_id: str | None


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    """A policy that applies to the requesting principal, with why."""

    policy: QuotaPolicy
    matched_assignments: tuple[QuotaAssignment, ...] = ()


@dataclass(frozen=True, slots=True)
class PoolUsage:
    """Consumption totals for one (window, model scope) query."""

    window_seconds: int
    model_ids: tuple[str, ...]
    raw_input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    by_user: dict[str, dict[str, int]] = field(default_factory=dict)

    def for_dimension(self, dimension: str, user_ids: Iterable[str] | None) -> int:
        """Sum one dimension, optionally restricted to a set of users."""
        if user_ids is None:
            return int(getattr(self, f"{_USAGE_COLUMN[dimension]}"))
        wanted = {str(uid) for uid in user_ids}
        total = 0
        for user_id, totals in self.by_user.items():
            if user_id in wanted:
                total += int(totals.get(dimension, 0))
        return total


_USAGE_COLUMN = {
    DIMENSION_RAW_INPUT: "raw_input_tokens",
    DIMENSION_CACHED_INPUT: "cached_input_tokens",
    DIMENSION_OUTPUT: "output_tokens",
}


def _policy_from_row(row: Any) -> QuotaPolicy:
    return QuotaPolicy(
        id=str(row["id"]),
        organization_id=str(row["organization_id"]),
        model_ids=tuple(str(model) for model in (row["model_ids"] or [])),
        name=str(row["name"]),
        description=str(row["description"] or ""),
        window_seconds=int(row["window_seconds"]),
        scope_mode=str(row["scope_mode"]),
        raw_input_tokens_limit=(
            int(row["raw_input_tokens_limit"])
            if row["raw_input_tokens_limit"] is not None
            else None
        ),
        cached_input_tokens_limit=(
            int(row["cached_input_tokens_limit"])
            if row["cached_input_tokens_limit"] is not None
            else None
        ),
        output_tokens_limit=(
            int(row["output_tokens_limit"])
            if row["output_tokens_limit"] is not None
            else None
        ),
        is_enabled=bool(row["is_enabled"]),
    )


class TokenQuotaRepository:
    """CRUD for quota policies/assignments plus consumption aggregation."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── reads ──────────────────────────────────────────────────────────────

    async def list_policies(
        self,
        organization_id: str | UUID,
        *,
        include_disabled: bool = True,
    ) -> list[dict[str, Any]]:
        """Every policy in the organization, each with its assignments."""
        clauses = ["organization_id = $1"]
        if not include_disabled:
            clauses.append("is_enabled")
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM token_quota_policies
                WHERE {' AND '.join(clauses)}
                ORDER BY name
                """,
                _uuid(organization_id),
            )
            assignments = await conn.fetch(
                """
                SELECT a.id, a.policy_id, a.principal_type, a.principal_id,
                       COALESCE(u.display_name, u.email) AS user_name,
                       g.name AS group_name
                FROM token_quota_assignments a
                LEFT JOIN users u
                  ON u.id = a.principal_id AND a.principal_type = 'user'
                LEFT JOIN groups g
                  ON g.id = a.principal_id AND a.principal_type = 'group'
                WHERE a.organization_id = $1
                ORDER BY a.principal_type, a.created_at
                """,
                _uuid(organization_id),
            )
        by_policy: dict[str, list[dict[str, Any]]] = {}
        for row in assignments:
            by_policy.setdefault(str(row["policy_id"]), []).append(
                {
                    "id": str(row["id"]),
                    "policy_id": str(row["policy_id"]),
                    "principal_type": str(row["principal_type"]),
                    "principal_id": (
                        str(row["principal_id"]) if row["principal_id"] else None
                    ),
                    "label": self._principal_label(row),
                }
            )
        items: list[dict[str, Any]] = []
        for row in rows:
            policy = _policy_from_row(row)
            items.append(
                {
                    **policy.as_dict(),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "assignments": by_policy.get(policy.id, []),
                }
            )
        return items

    @staticmethod
    def _principal_label(row: Any) -> str:
        principal_type = str(row["principal_type"])
        if principal_type == PRINCIPAL_ORG:
            return "Everyone in the organization"
        if principal_type == PRINCIPAL_USER:
            return str(row["user_name"] or row["principal_id"])
        return str(row["group_name"] or row["principal_id"])

    async def get_policy(
        self, policy_id: str | UUID, *, organization_id: str | UUID
    ) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM token_quota_policies
                WHERE id = $1 AND organization_id = $2
                """,
                _uuid(policy_id),
                _uuid(organization_id),
            )
        if not row:
            return None
        return {**_policy_from_row(row).as_dict(), "created_at": row["created_at"]}

    async def resolve_policies(
        self,
        organization_id: str | UUID,
        user_id: str | UUID,
        model_id: str,
    ) -> list[ResolvedPolicy]:
        """Enabled policies that apply to this user for this model.

        A policy applies when it covers the model (it lists it, or lists ``*``)
        *and* it is assigned to the whole organization, to a group this user
        belongs to, or to this user directly. All matching assignments are
        returned so the UI and audit trail can say *why* a limit applies.
        """
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                """
                SELECT p.*,
                       a.id            AS assignment_id,
                       a.principal_type,
                       a.principal_id
                FROM token_quota_policies p
                JOIN token_quota_assignments a ON a.policy_id = p.id
                WHERE p.organization_id = $1
                  AND p.is_enabled
                  AND (p.model_ids && ARRAY[$3::text, $4::text])
                  AND (
                        a.principal_type = 'org'
                     OR (a.principal_type = 'user' AND a.principal_id = $2)
                     OR (a.principal_type = 'group' AND a.principal_id IN (
                            SELECT group_id FROM user_groups WHERE user_id = $2
                        ))
                  )
                ORDER BY p.id
                """,
                _uuid(organization_id),
                _uuid(user_id),
                str(model_id),
                ALL_MODELS,
            )
        resolved: dict[str, ResolvedPolicy] = {}
        for row in rows:
            policy = _policy_from_row(row)
            assignment = QuotaAssignment(
                id=str(row["assignment_id"]),
                policy_id=policy.id,
                principal_type=str(row["principal_type"]),
                principal_id=str(row["principal_id"]) if row["principal_id"] else None,
            )
            existing = resolved.get(policy.id)
            if existing is None:
                resolved[policy.id] = ResolvedPolicy(
                    policy=policy, matched_assignments=(assignment,)
                )
            else:
                resolved[policy.id] = ResolvedPolicy(
                    policy=policy,
                    matched_assignments=(*existing.matched_assignments, assignment),
                )
        return list(resolved.values())

    async def group_members(
        self, organization_id: str | UUID, group_ids: Iterable[str | UUID]
    ) -> dict[str, set[str]]:
        """Members of the given groups, keyed by group id.

        Used for ``global`` policies assigned to a group: the pool is shared by
        every member, so their usage is summed together.
        """
        ids = [_uuid(gid) for gid in group_ids if gid]
        if not ids:
            return {}
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                """
                SELECT ug.group_id, ug.user_id
                FROM user_groups ug
                JOIN groups g ON g.id = ug.group_id
                WHERE ug.group_id = ANY($1::uuid[]) AND g.organization_id = $2
                """,
                ids,
                _uuid(organization_id),
            )
        members: dict[str, set[str]] = {str(gid): set() for gid in ids}
        for row in rows:
            members.setdefault(str(row["group_id"]), set()).add(str(row["user_id"]))
        return members

    async def organization_members(self, organization_id: str | UUID) -> set[str]:
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                "SELECT id FROM users WHERE organization_id = $1",
                _uuid(organization_id),
            )
        return {str(row["id"]) for row in rows}

    async def usage_for_window(
        self,
        organization_id: str | UUID,
        *,
        window_seconds: int,
        model_ids: Iterable[str] | str,
        now: datetime | None = None,
    ) -> PoolUsage:
        """Consumption totals per user inside a rolling window.

        ``model_ids`` may contain :data:`ALL_MODELS`, in which case every model
        counts toward the same pool — which is the natural reading of an
        org-wide "all models" budget. Otherwise only the listed models are
        summed, so a policy covering three models has one shared pool across
        exactly those three.

        Cached prompt tokens are reported separately from uncached ones because
        the two are priced differently and a policy may cap only one of them.
        ``raw_input`` is therefore ``input_tokens - cache_read_tokens`` clamped
        at zero, matching the Usage report's ``uncached_input_tokens``.
        """
        clamped = max(int(window_seconds), 60)
        scope = [str(model_ids)] if isinstance(model_ids, str) else list(model_ids)
        params: list[Any] = [_uuid(organization_id), clamped]
        model_clause = ""
        if ALL_MODELS not in scope:
            params.append(scope)
            model_clause = f"AND model = ANY(${len(params)}::text[])"
        time_anchor = "NOW()" if now is None else "$" + str(len(params) + 1)
        if now is not None:
            params.append(now)
        sql = f"""
            SELECT user_id,
                   COALESCE(SUM(GREATEST(input_tokens - cache_read_tokens, 0)), 0)
                       AS raw_input_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cached_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM llm_token_usage
            WHERE organization_id = $1
              AND created_at >= {time_anchor} - make_interval(secs => $2)
              {model_clause}
            GROUP BY user_id
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(sql, *params)
        by_user: dict[str, dict[str, int]] = {}
        totals = {dim: 0 for dim in DIMENSIONS}
        for row in rows:
            user_key = str(row["user_id"]) if row["user_id"] else ""
            entry = {
                DIMENSION_RAW_INPUT: int(row["raw_input_tokens"] or 0),
                DIMENSION_CACHED_INPUT: int(row["cached_input_tokens"] or 0),
                DIMENSION_OUTPUT: int(row["output_tokens"] or 0),
            }
            by_user[user_key] = entry
            for dim, value in entry.items():
                totals[dim] += value
        return PoolUsage(
            window_seconds=clamped,
            model_ids=tuple(scope),
            raw_input_tokens=totals[DIMENSION_RAW_INPUT],
            cached_input_tokens=totals[DIMENSION_CACHED_INPUT],
            output_tokens=totals[DIMENSION_OUTPUT],
            by_user=by_user,
        )

    # ── writes ─────────────────────────────────────────────────────────────

    async def create_policy(
        self,
        organization_id: str | UUID,
        *,
        model_ids: Iterable[str],
        name: str,
        window_seconds: int,
        scope_mode: str,
        raw_input_tokens_limit: int | None = None,
        cached_input_tokens_limit: int | None = None,
        output_tokens_limit: int | None = None,
        description: str = "",
        created_by: str | UUID | None = None,
    ) -> dict[str, Any]:
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO token_quota_policies (
                    organization_id, model_ids, name, description, window_seconds,
                    scope_mode, raw_input_tokens_limit,
                    cached_input_tokens_limit, output_tokens_limit, created_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                _uuid(organization_id),
                [str(model) for model in model_ids],
                name,
                description,
                int(window_seconds),
                scope_mode,
                raw_input_tokens_limit,
                cached_input_tokens_limit,
                output_tokens_limit,
                _uuid(created_by),
            )
        return {**_policy_from_row(row).as_dict(), "created_at": row["created_at"]}

    async def update_policy(
        self,
        policy_id: str | UUID,
        *,
        organization_id: str | UUID,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Update the supplied policy columns; unknown keys are rejected.

        Deliberately an explicit allowlist: this runs from a form handler, and a
        blanket ``**kwargs`` → SQL mapping would let a request body set columns
        the UI never offers.
        """
        allowed = {
            "name",
            "description",
            "model_ids",
            "window_seconds",
            "scope_mode",
            "raw_input_tokens_limit",
            "cached_input_tokens_limit",
            "output_tokens_limit",
            "is_enabled",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return await self.get_policy(policy_id, organization_id=organization_id)
        assignments = []
        params: list[Any] = [_uuid(policy_id), _uuid(organization_id)]
        for index, (column, value) in enumerate(updates.items(), start=3):
            assignments.append(f"{column} = ${index}")
            params.append(
                [str(model) for model in value]
                if column == "model_ids"
                else value
            )
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE token_quota_policies
                SET {', '.join(assignments)}, updated_at = NOW()
                WHERE id = $1 AND organization_id = $2
                RETURNING *
                """,
                *params,
            )
        if not row:
            return None
        return {**_policy_from_row(row).as_dict(), "created_at": row["created_at"]}

    async def delete_policy(
        self, policy_id: str | UUID, *, organization_id: str | UUID
    ) -> bool:
        async with scoped_acquire(organization_id=organization_id) as conn:
            result = await conn.execute(
                "DELETE FROM token_quota_policies WHERE id = $1 AND organization_id = $2",
                _uuid(policy_id),
                _uuid(organization_id),
            )
        return result.endswith(" 1")

    async def assign(
        self,
        policy_id: str | UUID,
        *,
        organization_id: str | UUID,
        principal_type: str,
        principal_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        """Idempotently bind a policy to a principal.

        ``ON CONFLICT DO NOTHING`` makes re-assigning the same principal a
        no-op rather than an error, because the UI renders one "apply" button
        per principal and a double submit must not fail.
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO token_quota_assignments (
                    policy_id, organization_id, principal_type, principal_id
                ) VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                RETURNING id, policy_id, principal_type, principal_id
                """,
                _uuid(policy_id),
                _uuid(organization_id),
                principal_type,
                _uuid(principal_id) if principal_type != PRINCIPAL_ORG else None,
            )
        if row:
            return dict(row)
        # Already assigned — return the existing row so callers can report it.
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, policy_id, principal_type, principal_id
                FROM token_quota_assignments
                WHERE policy_id = $1 AND principal_type = $2
                  AND COALESCE(principal_id, '00000000-0000-0000-0000-000000000000'::uuid)
                      = COALESCE($3::uuid, '00000000-0000-0000-0000-000000000000'::uuid)
                """,
                _uuid(policy_id),
                principal_type,
                _uuid(principal_id) if principal_type != PRINCIPAL_ORG else None,
            )
        return dict(row) if row else {}

    async def unassign(
        self, assignment_id: str | UUID, *, organization_id: str | UUID
    ) -> bool:
        async with scoped_acquire(organization_id=organization_id) as conn:
            result = await conn.execute(
                """
                DELETE FROM token_quota_assignments a
                USING token_quota_policies p
                WHERE a.id = $1 AND a.policy_id = p.id AND p.organization_id = $2
                """,
                _uuid(assignment_id),
                _uuid(organization_id),
            )
        return result.endswith(" 1")

    # ── reporting ──────────────────────────────────────────────────────────

    async def member_labels(
        self, organization_id: str | UUID, user_ids: Iterable[str | UUID]
    ) -> dict[str, str]:
        """Display names for user ids, for the consumption report.

        Read separately rather than joined into the usage aggregate because the
        aggregate groups by user in SQL and the label lookup is only needed for
        the handful of people who actually appear in the report.
        """
        ids = [_uuid(uid) for uid in user_ids if uid]
        if not ids:
            return {}
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                """
                SELECT id, COALESCE(NULLIF(display_name, ''), email, id::text) AS label
                FROM users WHERE id = ANY($1::uuid[]) AND organization_id = $2
                """,
                ids,
                _uuid(organization_id),
            )
        return {str(row["id"]): str(row["label"]) for row in rows}

    async def consumption_report(
        self,
        organization_id: str | UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Per (policy, subject) consumption, for the admin page.

        Computed on demand rather than maintained as a counter: the ledger is
        the only source of truth, and a denormalized counter would drift the
        moment a row was deleted or a window rolled over.
        """
        policies = await self.list_policies(organization_id, include_disabled=False)
        if not policies:
            return []
        rows: list[dict[str, Any]] = []
        # Resolve display names once for every user that can appear.
        label_ids: set[str] = set()
        for policy in policies:
            if policy["scope_mode"] != SCOPE_INDIVIDUAL:
                continue
            for assignment in policy["assignments"]:
                if assignment["principal_type"] == PRINCIPAL_ORG:
                    label_ids |= await self.organization_members(organization_id)
                elif assignment["principal_type"] == PRINCIPAL_USER and assignment["principal_id"]:
                    label_ids.add(str(assignment["principal_id"]))
        labels = await self.member_labels(organization_id, label_ids)
        for policy in policies:
            window = int(policy["window_seconds"])
            usage = await self.usage_for_window(
                organization_id,
                window_seconds=window,
                model_ids=policy["model_ids"],
            )
            subjects: list[tuple[str, str | None]] = []
            for assignment in policy["assignments"]:
                subjects.append((assignment["principal_type"], assignment["principal_id"]))
            if not subjects:
                continue
            for principal_type, principal_id in subjects:
                if principal_type == PRINCIPAL_ORG:
                    subject_users = None
                    label = "Organization"
                elif principal_type == PRINCIPAL_GROUP:
                    members = await self.group_members(
                        organization_id, [principal_id] if principal_id else []
                    )
                    subject_users = members.get(str(principal_id), set())
                    label = next(
                        (
                            a["label"]
                            for a in policy["assignments"]
                            if a["principal_type"] == principal_type
                            and str(a["principal_id"]) == str(principal_id)
                        ),
                        str(principal_id),
                    )
                else:
                    subject_users = {str(principal_id)}
                    label = next(
                        (
                            a["label"]
                            for a in policy["assignments"]
                            if a["principal_type"] == principal_type
                            and str(a["principal_id"]) == str(principal_id)
                        ),
                        str(principal_id),
                    )
                if policy["scope_mode"] == SCOPE_INDIVIDUAL and principal_type == PRINCIPAL_ORG:
                    # One row per member for per-person pools. Labels come from
                    # the same helper used elsewhere so the table never shows a
                    # bare UUID where a person's name belongs.
                    for user_id, totals in usage.by_user.items():
                        rows.append(
                            {
                                "policy_id": policy["id"],
                                "policy_name": policy["name"],
                                "model_ids": policy["model_ids"],
                                "window_seconds": window,
                                "scope_mode": policy["scope_mode"],
                                "subject_type": "user",
                                "subject_id": user_id,
                                "subject_label": labels.get(user_id, user_id),
                                **totals,
                            }
                        )
                    continue
                rows.append(
                    {
                        "policy_id": policy["id"],
                        "policy_name": policy["name"],
                        "model_ids": policy["model_ids"],
                        "window_seconds": window,
                        "scope_mode": policy["scope_mode"],
                        "subject_type": principal_type,
                        "subject_id": principal_id,
                        "subject_label": label,
                        "raw_input": usage.for_dimension(
                            DIMENSION_RAW_INPUT, subject_users
                        ),
                        "cached_input": usage.for_dimension(
                            DIMENSION_CACHED_INPUT, subject_users
                        ),
                        "output": usage.for_dimension(DIMENSION_OUTPUT, subject_users),
                    }
                )
        rows.sort(key=lambda item: item.get("raw_input", 0) + item.get("output", 0), reverse=True)
        return rows[:limit]


__all__ = [
    "ALL_MODELS",
    "DIMENSIONS",
    "DIMENSION_CACHED_INPUT",
    "DIMENSION_OUTPUT",
    "DIMENSION_RAW_INPUT",
    "PRINCIPAL_GROUP",
    "PRINCIPAL_ORG",
    "PRINCIPAL_TYPES",
    "PRINCIPAL_USER",
    "PoolUsage",
    "QuotaAssignment",
    "QuotaPolicy",
    "ResolvedPolicy",
    "SCOPE_GLOBAL",
    "SCOPE_INDIVIDUAL",
    "SCOPE_MODES",
    "TokenQuotaRepository",
]
