"""Access control service for ownership and group-based resource visibility.

This module is the single source of truth for authorization in Lucent. Every
surface (REST API, web UI, MCP tools, daemon) and every repository delegates the
"who may access / modify / grant this" decision here. See
``docs/access-control.md`` for the architecture contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from uuid import UUID

import asyncpg

RESOURCE_TABLE_MAP: dict[str, str] = {
    "agent": "agent_definitions",
    "agents": "agent_definitions",
    "skill": "skill_definitions",
    "skills": "skill_definitions",
    "mcp_server": "mcp_server_configs",
    "mcp_servers": "mcp_server_configs",
    "mcp": "mcp_server_configs",
    "hook": "hook_definitions",
    "hooks": "hook_definitions",
    "managed_tool": "managed_tool_definitions",
    "managed_tools": "managed_tool_definitions",
    "tool": "managed_tool_definitions",
    "tools": "managed_tool_definitions",
    "sandbox_template": "sandbox_templates",
    "sandbox_templates": "sandbox_templates",
    "secret": "secrets",
    "secrets": "secrets",
    "workflow": "schedules",
    "workflows": "schedules",
    "schedule": "schedules",
    "schedules": "schedules",
    "model": "models",
    "models": "models",
}

# Tables that have a 'scope' column (supports built-in detection)
_TABLES_WITH_SCOPE = {
    "agent_definitions", "skill_definitions", "mcp_server_configs",
    "hook_definitions", "managed_tool_definitions", "sandbox_templates",
    "schedules", "models",
}

# The single canonical token stored in ``resource_access_grants.resource_type``
# for each backing table. Every alias in RESOURCE_TABLE_MAP collapses to exactly
# one of these. The migration backfill writes the same literals, so the grant
# rows and the access clause always agree.
CANONICAL_RESOURCE_TYPE: dict[str, str] = {
    "agent_definitions": "agent",
    "skill_definitions": "skill",
    "mcp_server_configs": "mcp_server",
    "hook_definitions": "hook",
    "managed_tool_definitions": "managed_tool",
    "sandbox_templates": "sandbox_template",
    "schedules": "workflow",
    "models": "model",
    "secrets": "secret",
}
_CANONICAL_TYPES = frozenset(CANONICAL_RESOURCE_TYPE.values())


def normalize_resource_type(resource_type: str) -> str:
    key = (resource_type or "").strip().lower()
    if key not in RESOURCE_TABLE_MAP:
        raise ValueError(f"Unsupported resource_type: {resource_type}")
    return key


def canonical_resource_type(resource_type: str) -> str:
    """Return the single canonical token used in ``resource_access_grants``.

    Collapses any alias (``"agents"``, ``"agent"``) to the one token written by
    the migration backfill (``"agent"``). Raises on an unsupported type.
    """
    table = RESOURCE_TABLE_MAP[normalize_resource_type(resource_type)]
    return CANONICAL_RESOURCE_TYPE[table]


def build_access_clause(
    *,
    resource_type: str,
    uid_param: int,
    role_param: int,
    group_param: int | None = None,
    org_param: int | None = None,
    alias: str = "",
    has_scope: bool = True,
) -> str:
    """Return the canonical access ACL ``WHERE`` fragment.

    This is the SINGLE source of truth for the read-access predicate. Every
    repository and service that filters by access builds its clause from this
    function so the rule cannot drift between call sites. A guard test fails the
    build if the raw grant predicate appears anywhere else.

    Access resolves as: built-in (platform global) OR the requester is the
    managing owner (``owner_user_id``) OR the requester has an admin/owner role
    OR an explicit grant in ``resource_access_grants`` matches the requester
    (an ``org`` grant = everyone, a ``user`` grant = that user, a ``group``
    grant = any of the requester's groups).

    Args:
        resource_type: any alias of the resource type (e.g. ``"agent"``,
            ``"agents"``). Resolved to its canonical token and embedded as a
            safe SQL literal — it is always from the fixed allowlist, never user
            input.
        uid_param: positional parameter ($n) holding the requesting user's id.
        role_param: positional parameter holding the requesting user's role.
        group_param: optional positional parameter holding a pre-resolved
            ``uuid[]`` of the user's group ids. When omitted, group membership is
            resolved inline via a subselect on ``uid_param``.
        org_param: optional positional parameter holding the requester's
            organization id. When provided, the grant subquery is constrained to
            grants belonging to that organization. This is REQUIRED for resources
            whose rows are shared across organizations (e.g. the global ``models``
            catalog) so one org's grant cannot leak access to another org. For
            resources whose rows belong to a single org (everything else, already
            filtered by ``organization_id`` in the surrounding query) it is
            optional defense-in-depth.
        alias: optional table alias prefix (e.g. ``"a"`` → ``a.id``). When
            omitted, the canonical backing table name is used to qualify the
            outer columns so the correlated grant subquery cannot rebind them.
        has_scope: whether the table has a ``scope`` column. When False
            (e.g. secrets), the built-in branch is dropped.

    Returns:
        A parenthesized SQL boolean expression.
    """
    normalized = normalize_resource_type(resource_type)
    table = RESOURCE_TABLE_MAP[normalized]
    rtype = CANONICAL_RESOURCE_TYPE[table]
    # Qualify the outer-table column references. The grant subquery aliases
    # ``resource_access_grants`` as ``g`` (which also has an ``id`` column), so
    # an unqualified ``id`` in the correlated reference would bind to the wrong
    # table. Always qualify with the alias or the real table name.
    p = f"{alias}." if alias else f"{table}."
    if group_param is None:
        group_match = (
            "(g.principal_type = 'group' AND g.principal_id IN "
            f"(SELECT group_id FROM user_groups WHERE user_id = ${uid_param}))"
        )
    else:
        group_match = (
            "(g.principal_type = 'group' AND g.principal_id = "
            f"ANY(${group_param}::uuid[]))"
        )
    org_scope = f"g.organization_id = ${org_param} AND " if org_param is not None else ""
    grant_exists = (
        "EXISTS (SELECT 1 FROM resource_access_grants g "
        f"WHERE {org_scope}g.resource_type = '{rtype}' AND g.resource_id = {p}id::text AND ("
        "g.principal_type = 'org' "
        f"OR (g.principal_type = 'user' AND g.principal_id = ${uid_param}) "
        f"OR {group_match}))"
    )
    parts: list[str] = []
    if has_scope:
        parts.append(f"{p}scope = 'built-in'")
    parts.append(f"{p}owner_user_id = ${uid_param}")
    parts.append(grant_exists)
    parts.append(f"${role_param} IN ('admin', 'owner')")
    return "(" + " OR ".join(parts) + ")"


class Breadth(IntEnum):
    """Visibility breadth of an access-controlled resource (narrow → wide)."""

    PERSONAL = 1
    GROUP = 2
    ORG_SHARED = 3
    GLOBAL = 4  # built-in / platform-global


def resource_breadth(
    *,
    scope: str | None,
    owner_user_id: str | None,
    owner_group_id: str | None,
) -> Breadth:
    """Map an ownership triple to its visibility breadth rank."""
    if scope == "built-in":
        return Breadth.GLOBAL
    if owner_user_id is not None:
        return Breadth.PERSONAL
    if owner_group_id is not None:
        return Breadth.GROUP
    return Breadth.ORG_SHARED


# Grant compatibility verdicts.
COMPAT_OK = "ok"
COMPAT_MISMATCH = "mismatch"
COMPAT_CROSS_GROUP = "cross_group"


@dataclass
class Compatibility:
    """Verdict describing whether a capability may be granted to an actor."""

    status: str
    reason: str = ""
    actor_breadth: int = 0
    capability_breadth: int = 0
    suggested_fixes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == COMPAT_OK


@dataclass
class GrantDecision:
    """Result of authorizing a grant of a capability to an actor."""

    allowed: bool
    reason: str = ""
    compatibility: Compatibility | None = None
    requires_override: bool = False


@dataclass(frozen=True)
class AccessSet:
    """The set of principals who can access a resource.

    ``universal`` is True when the resource is built-in or carries an ``org``
    grant (everyone in the organization). Otherwise access is the explicit union
    of ``users`` and ``groups`` plus, when present, the managing owner.
    """

    universal: bool = False
    users: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()

    def covers(self, other: "AccessSet") -> bool:
        """True when everyone who can access ``other`` can also access self."""
        if self.universal:
            return True
        if other.universal:
            return False
        return other.users <= self.users and other.groups <= self.groups


def _set_breadth(s: AccessSet) -> int:
    if s.universal:
        return int(Breadth.ORG_SHARED)
    if s.groups:
        return int(Breadth.GROUP)
    return int(Breadth.PERSONAL)


def check_grant_compatibility(
    actor: AccessSet,
    capability: AccessSet,
) -> Compatibility:
    """Compare the access breadth of an actor and a capability (set-based).

    A grant is OK when the capability is visible to at least everyone who can
    use the actor (``capability`` covers ``actor``). Otherwise some users who can
    run the actor cannot see or maintain the capability — a MISMATCH that must be
    explicitly overridden. When the unmet principals are purely groups the
    verdict is CROSS_GROUP, to surface the group-containment nature of the gap.
    """
    a_breadth = _set_breadth(actor)
    c_breadth = _set_breadth(capability)
    if capability.covers(actor):
        return Compatibility(
            status=COMPAT_OK,
            actor_breadth=a_breadth,
            capability_breadth=c_breadth,
        )

    missing_users = (
        set() if capability.universal else (actor.users - capability.users)
    )
    missing_groups = (
        set() if capability.universal else (actor.groups - capability.groups)
    )
    if missing_groups and not missing_users and not actor.universal:
        return Compatibility(
            status=COMPAT_CROSS_GROUP,
            reason=(
                "The actor is shared with groups that cannot see the "
                "capability. Members of those groups may not be able to use it."
            ),
            actor_breadth=a_breadth,
            capability_breadth=c_breadth,
            suggested_fixes=[
                "promote_capability_to_org_shared",
                "grant_capability_to_same_groups",
                "revoke_grant",
                "override_with_reason",
            ],
        )
    return Compatibility(
        status=COMPAT_MISMATCH,
        reason=(
            "The capability is more narrowly scoped than the actor. The actor "
            "is broadly usable but depends on a capability only a subset of "
            "those users can see or maintain."
        ),
        actor_breadth=a_breadth,
        capability_breadth=c_breadth,
        suggested_fixes=[
            "promote_capability",
            "narrow_actor",
            "revoke_grant",
            "override_with_reason",
        ],
    )


def _norm_id(value: object) -> str | None:
    """Normalize a possibly-UUID id to a string, treating empty as None."""
    if value is None:
        return None
    text = str(value)
    return text or None


class AccessControlService:
    """Resolve resource access by built-in, ownership, group, then admin override."""

    _GROUP_CACHE_TTL = timedelta(seconds=5)
    _group_cache: dict[str, tuple[datetime, list[str]]] = {}

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    def invalidate_user_groups(cls, user_id: str) -> None:
        cls._group_cache.pop(user_id, None)

    async def _get_user_role(self, user_id: str, org_id: str) -> str | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                UUID(user_id),
                UUID(org_id),
            )
        return str(row["role"]) if row else None

    async def get_user_group_ids(self, user_id: str) -> list[str]:
        now = datetime.now(UTC)
        cached = self._group_cache.get(user_id)
        if cached and cached[0] > now:
            return list(cached[1])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1",
                UUID(user_id),
            )
        group_ids = [str(r["group_id"]) for r in rows]
        self._group_cache[user_id] = (now + self._GROUP_CACHE_TTL, group_ids)
        return group_ids

    async def can_access(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        """Resolve access: built-in → owner → group owner → admin/owner → deny."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False

        group_ids = [UUID(g) for g in await self.get_user_group_ids(user_id)]
        clause = build_access_clause(
            resource_type=normalized,
            uid_param=3,
            group_param=4,
            role_param=5,
            has_scope=table in _TABLES_WITH_SCOPE,
        )
        query = f"""
            SELECT EXISTS(
                SELECT 1
                FROM {table}
                WHERE id = $1
                  AND organization_id = $2
                  AND {clause}
            ) AS allowed
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                UUID(resource_id),
                UUID(org_id),
                UUID(user_id),
                group_ids,
                role,
            )
        return bool(row["allowed"]) if row else False

    async def can_access_model(
        self, user_id: str, model_id: str, org_id: str, *, role: str | None = None
    ) -> bool:
        """Whether a user may USE a model from the global catalog.

        Models cannot reuse :meth:`can_access` because their ids are free-form
        strings (e.g. ``'auto'``) rather than UUIDs. Resolution mirrors
        ``ModelRepository.list_models``: built-in/owner/admin/owner-role or an
        explicit grant. Grant matching is constrained to the requester's
        organization so one org's grant cannot expose a shared (org-NULL) model
        to another org. Admin/owner roles — including the daemon owner — pass via
        the role branch, preserving broad model selection for automated paths.
        """
        if role is None:
            role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False
        clause = build_access_clause(
            resource_type="model", uid_param=1, role_param=2, org_param=4
        )
        query = f"""
            SELECT EXISTS(
                SELECT 1
                FROM models
                WHERE id = $3
                  AND (organization_id IS NULL OR organization_id = $4)
                  AND {clause}
            ) AS allowed
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                UUID(str(user_id)),
                role,
                str(model_id),
                UUID(str(org_id)),
            )
        return bool(row["allowed"]) if row else False

    async def model_in_catalog(self, model_id: str, org_id: str) -> bool:
        """Whether ``model_id`` exists in the requester's catalog scope.

        A model is in scope when it is a global (org-``NULL``) entry or belongs
        to the requester's organization. Used to distinguish "exists but you have
        no grant" (enforce) from "absent from the catalog" (defer to
        ``validate_model``), so registry/DB desync in degraded mode does not
        spuriously block every selection.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT EXISTS(SELECT 1 FROM models WHERE id = $1 "
                "AND (organization_id IS NULL OR organization_id = $2)) AS e",
                str(model_id),
                UUID(str(org_id)),
            )
        return bool(row["e"]) if row else False

    async def accessible_model_ids(
        self, user_id: str, org_id: str, *, role: str | None = None
    ) -> set[str]:
        """Return the enabled model ids the requester may use.

        Grant-filtered identically to :meth:`can_access_model` and
        ``ModelRepository.list_models``: admin/owner roles (including the daemon
        owner) receive every enabled model via the role branch, while members are
        limited to models granted to them, their groups, or the org. Used to
        resolve a user-appropriate default when the configured default is not
        granted to the requester.
        """
        if role is None:
            role = await self._get_user_role(user_id, org_id)
        if role is None:
            return set()
        clause = build_access_clause(
            resource_type="model", uid_param=1, role_param=2, org_param=3
        )
        query = f"""
            SELECT id
            FROM models
            WHERE is_enabled = true
              AND (organization_id IS NULL OR organization_id = $3)
              AND {clause}
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                query,
                UUID(str(user_id)),
                role,
                UUID(str(org_id)),
            )
        return {r["id"] for r in rows}

    async def can_modify(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        """Check write access: only the managing owner or admin/owner can modify.

        Access grants control who can *use* a resource; they do not confer the
        right to *manage* it. Only the resource's ``owner_user_id`` (its creator/
        manager) and org admins/owners may modify it or edit its access grants.
        """
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False
        if role in ("admin", "owner"):
            # Admin/owner can modify any resource in their org (verify it exists).
            # Globally-shared rows (organization_id IS NULL, e.g. the models
            # catalog) are manageable by any org's admins.
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT EXISTS("
                    f"SELECT 1 FROM {table} WHERE id::text = $1 "
                    f"AND (organization_id = $2 OR organization_id IS NULL)"
                    f") AS e",
                    str(resource_id),
                    UUID(org_id),
                )
            return bool(row["e"]) if row else False
        # Members can only modify resources they manage (own).
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT EXISTS("
                f"SELECT 1 FROM {table}"
                f" WHERE id::text = $1 AND organization_id = $2"
                f" AND owner_user_id = $3"
                f") AS e",
                str(resource_id),
                UUID(org_id),
                UUID(user_id),
            )
        return bool(row["e"]) if row else False

    async def list_accessible(self, user_id: str, resource_type: str, org_id: str) -> list[str]:
        """Return IDs of all resources of this type the user can access."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return []

        group_ids = [UUID(g) for g in await self.get_user_group_ids(user_id)]
        clause = build_access_clause(
            resource_type=normalized,
            uid_param=2,
            group_param=3,
            role_param=4,
            has_scope=table in _TABLES_WITH_SCOPE,
        )
        query = f"""
            SELECT id
            FROM {table}
            WHERE organization_id = $1
              AND {clause}
            ORDER BY id
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, UUID(org_id), UUID(user_id), group_ids, role)
        return [str(r["id"]) for r in rows]

    async def get_ownership(
        self, resource_type: str, resource_id: str, org_id: str
    ) -> dict | None:
        """Return the ownership triple for a resource, or None if not found.

        Keys: ``id``, ``scope`` (None if the table has no scope column),
        ``owner_user_id``, ``owner_group_id``.
        """
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        has_scope = table in _TABLES_WITH_SCOPE
        scope_col = "scope" if has_scope else "NULL::varchar AS scope"
        query = f"""
            SELECT id, {scope_col}, owner_user_id, owner_group_id
            FROM {table}
            WHERE id = $1 AND organization_id = $2
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, UUID(resource_id), UUID(org_id))
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "scope": row["scope"],
            "owner_user_id": _norm_id(row["owner_user_id"]),
            "owner_group_id": _norm_id(row["owner_group_id"]),
        }

    async def get_access_set(
        self, resource_type: str, resource_id: str, org_id: str
    ) -> AccessSet | None:
        """Return the set of principals who can access a resource, or None.

        Combines the resource's built-in flag, its managing owner, and every
        row in ``resource_access_grants`` into an :class:`AccessSet`.
        """
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        rtype = CANONICAL_RESOURCE_TYPE[table]
        has_scope = table in _TABLES_WITH_SCOPE
        scope_col = "scope" if has_scope else "NULL::varchar AS scope"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {scope_col}, owner_user_id FROM {table} "
                f"WHERE id::text = $1 AND (organization_id = $2 OR organization_id IS NULL)",
                str(resource_id),
                UUID(org_id),
            )
            if row is None:
                return None
            grants = await conn.fetch(
                "SELECT principal_type, principal_id FROM resource_access_grants "
                "WHERE resource_type = $1 AND resource_id = $2 "
                "AND organization_id = $3",
                rtype,
                str(resource_id),
                UUID(org_id),
            )
        universal = row["scope"] == "built-in"
        users: set[str] = set()
        groups: set[str] = set()
        if row["owner_user_id"] is not None:
            users.add(str(row["owner_user_id"]))
        for g in grants:
            pt = g["principal_type"]
            if pt == "org":
                universal = True
            elif pt == "user":
                users.add(str(g["principal_id"]))
            elif pt == "group":
                groups.add(str(g["principal_id"]))
        return AccessSet(
            universal=universal,
            users=frozenset(users),
            groups=frozenset(groups),
        )

    async def list_access_grants(
        self, resource_type: str, resource_id: str, org_id: str
    ) -> list[dict]:
        """Return the explicit grant rows for a resource with resolved names.

        Each entry: ``principal_type`` (user|group|org), ``principal_id``,
        ``name`` (display name), ``granted_by``, ``granted_at``. The managing
        owner is not included here — callers surface it separately.
        """
        rtype = canonical_resource_type(resource_type)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT g.principal_type, g.principal_id, g.granted_by, g.granted_at,
                       CASE g.principal_type
                            WHEN 'user' THEN COALESCE(u.display_name, u.email)
                            WHEN 'group' THEN grp.name
                            WHEN 'org' THEN 'Organization (everyone)'
                       END AS name
                FROM resource_access_grants g
                LEFT JOIN users u  ON g.principal_type = 'user'  AND u.id = g.principal_id
                LEFT JOIN groups grp ON g.principal_type = 'group' AND grp.id = g.principal_id
                WHERE g.resource_type = $1 AND g.resource_id = $2
                  AND g.organization_id = $3
                ORDER BY g.principal_type, name
                """,
                rtype,
                str(resource_id),
                UUID(org_id),
            )
        return [
            {
                "principal_type": r["principal_type"],
                "principal_id": str(r["principal_id"]),
                "name": r["name"],
                "granted_by": str(r["granted_by"]) if r["granted_by"] else None,
                "granted_at": r["granted_at"],
            }
            for r in rows
        ]

    async def grant_access(
        self,
        *,
        resource_type: str,
        resource_id: str,
        org_id: str,
        principal_type: str,
        principal_id: str | None,
        granted_by: str | None = None,
    ) -> None:
        """Add an access grant. ``org`` grants ignore ``principal_id`` (uses org)."""
        rtype = canonical_resource_type(resource_type)
        ptype = (principal_type or "").strip().lower()
        if ptype not in ("user", "group", "org"):
            raise ValueError(f"Invalid principal_type: {principal_type}")
        pid = org_id if ptype == "org" else principal_id
        if pid is None:
            raise ValueError("principal_id is required for user/group grants")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resource_access_grants
                    (organization_id, resource_type, resource_id,
                     principal_type, principal_id, granted_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (resource_type, resource_id, principal_type, principal_id)
                DO NOTHING
                """,
                UUID(org_id),
                rtype,
                str(resource_id),
                ptype,
                UUID(pid),
                UUID(granted_by) if granted_by else None,
            )

    async def revoke_access(
        self,
        *,
        resource_type: str,
        resource_id: str,
        org_id: str,
        principal_type: str,
        principal_id: str | None,
    ) -> None:
        """Remove an access grant. ``org`` grants ignore ``principal_id``."""
        rtype = canonical_resource_type(resource_type)
        ptype = (principal_type or "").strip().lower()
        pid = org_id if ptype == "org" else principal_id
        if pid is None:
            raise ValueError("principal_id is required for user/group grants")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM resource_access_grants
                WHERE resource_type = $1 AND resource_id = $2
                  AND organization_id = $3
                  AND principal_type = $4 AND principal_id = $5
                """,
                rtype,
                str(resource_id),
                UUID(org_id),
                ptype,
                UUID(pid),
            )

    async def principal_exists_in_org(
        self, *, principal_type: str, principal_id: str | None, org_id: str
    ) -> bool:
        """Validate that a grant principal belongs to the caller's organization.

        Guards the REST/MCP boundary where ``principal_id`` is untrusted input:
        an ``org`` grant always targets the caller's own org; a ``user``/``group``
        grant must reference a row inside that same org. Cross-org principals are
        rejected so a caller cannot mint dangling or cross-tenant grant rows.
        """
        ptype = (principal_type or "").strip().lower()
        if ptype == "org":
            return True
        if ptype not in ("user", "group") or not principal_id:
            return False
        try:
            pid = UUID(principal_id)
            oid = UUID(org_id)
        except (ValueError, AttributeError):
            return False
        table = "users" if ptype == "user" else "groups"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT EXISTS(SELECT 1 FROM {table} "
                f"WHERE id = $1 AND organization_id = $2) AS e",
                pid,
                oid,
            )
        return bool(row["e"]) if row else False

    async def authorize_grant(
        self,
        *,
        grantor_id: str,
        org_id: str,
        actor_type: str,
        actor_id: str,
        capability_type: str,
        capability_id: str,
        override: bool = False,
    ) -> GrantDecision:
        """Authorize attaching a capability to an actor.

        Enforces, in order: the grantor may modify the actor, the grantor can
        access the capability, and the grant is scope-compatible (or explicitly
        overridden). This is the single decision point for every grant path.
        """
        if not await self.can_modify(grantor_id, actor_type, actor_id, org_id):
            return GrantDecision(
                allowed=False,
                reason="You do not have permission to modify the target.",
            )
        if not await self.can_access(grantor_id, capability_type, capability_id, org_id):
            return GrantDecision(
                allowed=False,
                reason="You do not have access to the capability being granted.",
            )

        actor = await self.get_access_set(actor_type, actor_id, org_id)
        capability = await self.get_access_set(capability_type, capability_id, org_id)
        if actor is None or capability is None:
            return GrantDecision(allowed=False, reason="Target or capability not found.")

        compat = check_grant_compatibility(actor, capability)
        if compat.ok:
            return GrantDecision(allowed=True, compatibility=compat)
        if override:
            return GrantDecision(
                allowed=True,
                compatibility=compat,
                requires_override=True,
                reason="Scope mismatch overridden.",
            )
        return GrantDecision(
            allowed=False,
            compatibility=compat,
            requires_override=True,
            reason=compat.reason,
        )

    # Junctions linking an agent (actor) to its granted capabilities.
    _GRANT_JUNCTIONS = (
        ("skill", "agent_skills", "skill_id", "skill_definitions"),
        ("mcp_server", "agent_mcp_servers", "mcp_server_id", "mcp_server_configs"),
        ("hook", "agent_hooks", "hook_id", "hook_definitions"),
        ("managed_tool", "agent_managed_tools", "tool_id", "managed_tool_definitions"),
    )

    async def scan_agent_grant_mismatches(
        self, agent_id: str, org_id: str
    ) -> list[dict]:
        """Return scope-compatibility warnings for an agent's existing grants.

        Each entry describes a granted capability whose access set does not
        cover the agent's access set, so some users who can run the agent may
        not be able to see or maintain the capability.
        """
        actor = await self.get_access_set("agent", agent_id, org_id)
        if actor is None:
            return []
        # Collect the granted capabilities first (single connection), then
        # resolve each capability's access set without nesting acquisitions.
        granted: list[tuple[str, str, str, bool]] = []
        async with self.pool.acquire() as conn:
            for cap_type, junction, cap_col, cap_table in self._GRANT_JUNCTIONS:
                rows = await conn.fetch(
                    f"""
                    SELECT c.id, c.name, j.grant_override
                    FROM {junction} j
                    JOIN {cap_table} c ON c.id = j.{cap_col}
                    WHERE j.agent_id = $1 AND c.organization_id = $2
                    """,
                    UUID(agent_id),
                    UUID(org_id),
                )
                for r in rows:
                    granted.append(
                        (cap_type, str(r["id"]), r["name"], bool(r["grant_override"]))
                    )
        warnings: list[dict] = []
        for cap_type, cap_id, cap_name, overridden in granted:
            capability = await self.get_access_set(cap_type, cap_id, org_id)
            if capability is None:
                continue
            compat = check_grant_compatibility(actor, capability)
            if compat.ok:
                continue
            warnings.append(
                {
                    "capability_type": cap_type,
                    "capability_id": cap_id,
                    "capability_name": cap_name,
                    "status": compat.status,
                    "reason": compat.reason,
                    "overridden": overridden,
                    "suggested_fixes": compat.suggested_fixes,
                }
            )
        return warnings


async def enforce_model_access(
    pool,
    *,
    user_id: str | None,
    role: str | None,
    org_id: str | None,
    model_id: str | None,
) -> str | None:
    """Return an error message if the requester may not USE ``model_id``.

    Selection-time guard shared by the request/schedule/chat creation paths.
    Keyed on the REQUESTING user's grants: admin/owner roles (including the
    daemon owner) pass via the role branch of the access clause, while members
    are limited to models granted to them, their groups, or the org. Returns
    ``None`` (allow) when no model, user, or org context is present —
    internal/system selection paths are trusted and already gated upstream.
    """
    if not model_id or not user_id or not org_id:
        return None
    acl = AccessControlService(pool)
    # Only cataloged models carry grants. A model absent from the requester's
    # catalog scope has no grant data to enforce — ``validate_model`` is the gate
    # there (covers registry/DB desync and degraded fallback modes).
    if not await acl.model_in_catalog(model_id, org_id):
        return None
    if await acl.can_access_model(user_id, model_id, org_id, role=role):
        return None
    return f"Model '{model_id}' is not available to you."
