"""Database repository for model management.

Models are organization-level resources (migration 091 semantics aside, no
personal ownership): owners/admins manage every model in their org, while
*use* is clearance-driven and default-deny — an enabled model is only
runnable by principals with an explicit read clearance (user, group, or the
whole org). The enabled toggle is the "approved for use" gate on top.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Final
from uuid import UUID

import asyncpg

from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthTablePolicy,
    get_authorized_pool_for_user,
    scoped_acquire,
)

MODELS_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("models", direct_columns=())


async def get_authorized_models_pool(
    pool: asyncpg.Pool,
    user: dict[str, Any],
    required_clearance: str = "read",
) -> AuthorizedDatabasePool:
    """Return a pool whose model reads are clearance-gated (usage path).

    Default deny: only models the principal is explicitly granted — user,
    group, or org clearance — resolve here. Management surfaces (owner/admin
    views, toggles, grant lists) deliberately stay on the org-scoped legacy
    path; they are role-gated at the routes.
    """
    return await get_authorized_pool_for_user(
        pool,
        user,
        MODELS_AUTH_POLICY,
        required_clearance,
    )


def _jsonb_param(value):
    """Normalize JSONB values, avoiding double-encoded JSON strings."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class ModelRepository:
    """CRUD operations for the models table."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_models(
        self,
        provider: str | None = None,
        category: str | None = None,
        enabled_only: bool = False,
        org_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        base = " FROM models WHERE 1=1"
        params: list = []
        idx = 1

        if provider:
            base += f" AND provider = ${idx}"
            params.append(provider)
            idx += 1
        if category:
            base += f" AND category = ${idx}"
            params.append(category)
            idx += 1
        if enabled_only:
            base += " AND is_enabled = true"
        if org_id:
            base += f" AND (organization_id IS NULL OR organization_id = ${idx})"
            params.append(UUID(org_id))
            idx += 1

        count_query = f"SELECT COUNT(*) AS total{base}"
        query = f"SELECT *{base} ORDER BY provider, name LIMIT ${idx} OFFSET ${idx + 1}"
        params_with_page = [*params, limit, offset]

        # models is the global registry (organization_id NULL = org-shared);
        # admin callers gate visibility.
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def list_models_accessible_by(
        self,
        user_id: str,
        org_id: str,
        *,
        requester_role: str = "member",
        enabled_only: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> dict:
        """List models cleared for *use* by the principal.

        Usage path: on an authorized pool this is clearance-driven and
        default-deny (``user_id``/``requester_role`` are accepted for
        compatibility but a role grants nothing — owners/admins get models
        only via explicit grants, like everyone else). A plain pool is
        fail-closed. The ``is_enabled`` toggle stays in the caller's SQL: it
        is the "approved for use" gate and is deliberately independent of
        clearances.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Model usage reads require an AuthorizedDatabasePool built "
                "for the models policy (usage is clearance-driven and default-deny)"
            )
        enabled_clause = "AND m.is_enabled = true" if enabled_only else ""
        count_row = await self._authorized_conn_fetchrow(
            f"SELECT COUNT(*) AS total FROM models m WHERE m.organization_id = $1 {enabled_clause}",
            UUID(org_id),
        )
        total_count = count_row["total"] if count_row else 0
        rows = await self._authorized_conn_fetch(
            f"""
            SELECT m.* FROM models m
            WHERE m.organization_id = $1 {enabled_clause}
            ORDER BY m.provider, m.name LIMIT $2 OFFSET $3
            """,
            UUID(org_id),
            limit,
            offset,
        )
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def _authorized_conn_fetchrow(self, query: str, *params: object):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *params)

    async def _authorized_conn_fetch(self, query: str, *params: object):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *params)

    async def get_model(self, model_id: str, organization_id: str | None = None) -> dict | None:
        """By-ID lookup on the global registry; access gating is caller responsibility.

        User-facing management callers pass organization_id so a row from
        another organization is simply absent (404 at the route). None stays
        the system-infra branch: global-registry housekeeping and the create
        existence check, which must see every row for uniqueness.
        """
        async with self.pool.acquire() as conn:
            query = "SELECT * FROM models WHERE id = $1"
            params: list[object] = [model_id]
            if organization_id:
                query += " AND organization_id = $2"
                params.append(UUID(organization_id))
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def get_usable_model(self, model_id: str) -> dict | None:
        """By-ID *usage* lookup: the model must be cleared for this principal.

        Requires an authorized pool — the clearance subquery decides, and an
        ungranted model is simply absent. Management callers use get_model()
        on the org-scoped path instead.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError("Model usage reads require an AuthorizedDatabasePool")
        row = await self._authorized_conn_fetchrow(
            "SELECT * FROM models WHERE id = $1", model_id
        )
        return dict(row) if row else None

    async def list_initial_setup_models(self) -> list[dict]:
        """Return global models that are valid choices during first-run setup.

        Disabled seed rows represent providers that have not been configured and
        must not appear as usable choices. Provider-discovered and manually
        configured models are safe to present, along with anything already
        enabled by an operator before setup.
        """
        # pre-auth setup path: the global registry, before any tenant context exists.
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM models
                   WHERE organization_id IS NULL
                     AND (
                         is_enabled = true
                         OR discovery_source IN ('provider', 'manual')
                         OR is_custom = true
                     )
                   ORDER BY is_enabled DESC, provider, name"""
            )
        return [dict(row) for row in rows]

    async def enable_models(self, model_ids: list[str]) -> set[str]:
        """Enable model rows and return the IDs that were updated."""
        if not model_ids:
            return set()
        now = datetime.now(timezone.utc)
        # by-ID lookup on the global registry; access gating is caller responsibility.
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """UPDATE models
                   SET is_enabled = true, updated_at = $1
                   WHERE id = ANY($2::text[])
                   RETURNING id""",
                now,
                model_ids,
            )
        return {row["id"] for row in rows}

    async def create_model(
        self,
        model_id: str,
        provider: str,
        name: str,
        category: str = "general",
        api_model_id: str = "",
        context_window: int = 0,
        supports_tools: bool = True,
        supports_vision: bool = False,
        notes: str = "",
        tags: list[str] | None = None,
        reasoning_efforts: list[str] | None = None,
        is_enabled: bool = True,
        org_id: str | None = None,
        engine: str | None = None,
        discovery_source: str = "manual",
        is_custom: bool = True,
        discovery_metadata: dict | None = None,
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """INSERT INTO models (id, provider, name, category, api_model_id,
                   context_window, supports_tools, supports_vision, notes, tags,
                   reasoning_efforts, is_enabled, organization_id, engine,
                   discovery_source, is_custom, discovery_metadata, created_at,
                   updated_at, owner_user_id, owner_group_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                       $16,$17::jsonb,$18,$18,$19,$20)
                   RETURNING *""",
                model_id,
                provider,
                name,
                category,
                api_model_id,
                context_window,
                supports_tools,
                supports_vision,
                notes,
                tags or [],
                reasoning_efforts or [],
                is_enabled,
                UUID(org_id) if org_id else None,
                engine,
                discovery_source,
                is_custom,
                _jsonb_param(discovery_metadata),
                now,
                UUID(owner_user_id) if owner_user_id else None,
                UUID(owner_group_id) if owner_group_id else None,
            )
        return dict(row)

    async def update_model(
        self, model_id: str, *, organization_id: str | None = None, **kwargs
    ) -> dict | None:
        """Management write on the global registry; gating is caller responsibility.

        User-facing management callers pass organization_id so updates never
        reach another organization's row. None stays the system-infra branch
        (provider sync housekeeping owns the global namespace).
        """
        allowed = {
            "provider", "name", "category", "api_model_id", "context_window",
            "supports_tools", "supports_vision", "notes", "tags", "is_enabled",
            "reasoning_efforts", "engine", "discovery_source", "is_custom",
            "last_discovered_at", "discovery_metadata",
            "organization_id", "owner_user_id", "owner_group_id",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return await self.get_model(model_id, organization_id)

        updates["updated_at"] = datetime.now(timezone.utc)
        set_parts = []
        params = []
        for i, (key, val) in enumerate(updates.items(), start=1):
            if key == "discovery_metadata":
                set_parts.append(f"{key} = ${i}::jsonb")
                params.append(_jsonb_param(val))
            else:
                set_parts.append(f"{key} = ${i}")
                params.append(
                    UUID(val)
                    if key in {"organization_id", "owner_user_id", "owner_group_id"} and val
                    else val
                )

        params.append(model_id)
        query = f"UPDATE models SET {', '.join(set_parts)} WHERE id = ${len(params)}"
        if organization_id:
            # Separate slot: updates may legitimately carry organization_id
            # (an org move); the predicate binds the CALLER'S org.
            params.append(UUID(organization_id))
            query += f" AND organization_id = ${len(params)}"

        # by-ID lookup on the global registry; access gating is caller responsibility.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query + " RETURNING *", *params)
        return dict(row) if row else None

    async def sync_discovered_models(
        self,
        *,
        provider: str,
        models: list[dict],
        org_id: str | None = None,
        disable_missing: bool = False,
    ) -> dict:
        """Upsert provider-discovered models without clobbering manual rows.

        Manual/custom rows (``discovery_source='manual'`` or ``is_custom``) keep
        their human-authored provider/name/category/tags/enabled settings if a
        provider later reports the same ID. We still update discovery metadata so
        the UI can show that the custom model was seen in a provider catalog.
        """
        now = datetime.now(timezone.utc)
        upserted = 0
        discovered_ids = [m["model_id"] for m in models]
        async with scoped_acquire(organization_id=org_id) as conn:
            async with conn.transaction():
                for m in models:
                    # Update first; insert only when no row exists. A plain
                    # INSERT ... ON CONFLICT DO UPDATE still fires the BEFORE
                    # INSERT auth_id assign trigger on the conflict path,
                    # leaking an unused auth_ids row on every model re-sync
                    # (the surviving row keeps its original auth_id).
                    await conn.execute(
                        """
                        WITH existing AS (
                            SELECT 1 FROM models WHERE id = $1
                        )
                        INSERT INTO models (
                            id, provider, name, category, api_model_id,
                            context_window, supports_tools, supports_vision,
                            notes, tags, is_enabled, organization_id, engine,
                            reasoning_efforts, discovery_source, is_custom, last_discovered_at,
                            discovery_metadata, created_at, updated_at
                        ) SELECT
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,false,$11,$12,
                            $13,'provider',false,$14,$15::jsonb,$14,$14
                        WHERE NOT EXISTS (SELECT 1 FROM existing)
                        """,
                        m["model_id"],
                        m["provider"],
                        m["name"],
                        m.get("category", "general"),
                        m.get("api_model_id") or m["model_id"],
                        int(m.get("context_window") or 0),
                        bool(m.get("supports_tools", True)),
                        bool(m.get("supports_vision", False)),
                        m.get("notes", ""),
                        m.get("tags") or [],
                        UUID(org_id) if org_id else None,
                        m.get("engine"),
                        m.get("reasoning_efforts") or [],
                        now,
                        _jsonb_param(m.get("discovery_metadata")),
                    )
                    await conn.execute(
                        """
                        UPDATE models SET
                            provider = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.provider
                                ELSE $2
                            END,
                            name = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.name
                                ELSE $3
                            END,
                            category = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.category
                                ELSE $4
                            END,
                            api_model_id = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.api_model_id
                                ELSE $5
                            END,
                            context_window = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.context_window
                                ELSE $6
                            END,
                            supports_tools = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.supports_tools
                                ELSE $7
                            END,
                            supports_vision = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.supports_vision
                                ELSE $8
                            END,
                            notes = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.notes
                                ELSE $9
                            END,
                            tags = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.tags
                                ELSE $10
                            END,
                            engine = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.engine
                                ELSE $11
                            END,
                            reasoning_efforts = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.reasoning_efforts
                                ELSE $12
                            END,
                            discovery_source = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.discovery_source
                                ELSE 'provider'
                            END,
                            is_custom = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.is_custom
                                ELSE false
                            END,
                            last_discovered_at = $13,
                            discovery_metadata = $14::jsonb,
                            updated_at = $13
                        WHERE id = $1
                        """,
                        m["model_id"],
                        m["provider"],
                        m["name"],
                        m.get("category", "general"),
                        m.get("api_model_id") or m["model_id"],
                        int(m.get("context_window") or 0),
                        bool(m.get("supports_tools", True)),
                        bool(m.get("supports_vision", False)),
                        m.get("notes", ""),
                        m.get("tags") or [],
                        m.get("engine"),
                        m.get("reasoning_efforts") or [],
                        now,
                        _jsonb_param(m.get("discovery_metadata")),
                    )
                    upserted += 1

                disabled_missing = 0
                if disable_missing:
                    rows = await conn.fetch(
                        """
                        UPDATE models
                        SET is_enabled = false, updated_at = $1
                        WHERE provider = $2
                          AND discovery_source = 'provider'
                          AND is_custom = false
                          AND NOT (id = ANY($3::text[]))
                        RETURNING id
                        """,
                        now,
                        provider,
                        discovered_ids,
                    )
                    disabled_missing = len(rows)

        return {
            "upserted": upserted,
            "disabled_missing": disabled_missing if disable_missing else 0,
        }

    async def list_discovery_state(self) -> dict[str, dict]:
        """Return ``{model_id: discovery_metadata}`` for digest change detection.

        Lets discovery skip re-fetching provider details for models whose
        catalog digest has not changed since the last sync.
        """
        async with self.pool.acquire() as conn:  # system-infra: scope-less, audited
            rows = await conn.fetch("SELECT id, discovery_metadata FROM models")
        return {
            str(row["id"]): row["discovery_metadata"] or {}
            for row in rows
            if isinstance(row["discovery_metadata"], dict)
        }

    async def touch_discovered_at(self, model_ids: list[str]) -> None:
        """Bump ``last_discovered_at`` without touching row content.

        Used on unchanged-digest syncs so the model list still reflects that
        the provider catalog was seen recently.
        """
        if not model_ids:
            return
        async with self.pool.acquire() as conn:  # system-infra: scope-less, audited
            await conn.execute(
                """
                UPDATE models
                SET last_discovered_at = $1, updated_at = $1
                WHERE id = ANY($2::text[])
                """,
                datetime.now(timezone.utc),
                list(model_ids),
            )

    async def toggle_model(
        self, model_id: str, enabled: bool, organization_id: str | None = None
    ) -> dict | None:
        return await self.update_model(
            model_id, is_enabled=enabled, organization_id=organization_id
        )

    async def delete_model(self, model_id: str, organization_id: str | None = None) -> bool:
        """Management delete; user-facing callers pass organization_id so a
        row from another organization is simply absent. None stays the
        system-infra branch (provider-sync housekeeping)."""
        async with self.pool.acquire() as conn:
            query = "DELETE FROM models WHERE id = $1"
            params: list[object] = [model_id]
            if organization_id:
                query += " AND organization_id = $2"
                params.append(UUID(organization_id))
            result = await conn.execute(query, *params)
        return result == "DELETE 1"

    async def get_enabled_model_ids(self) -> set[str]:
        async with self.pool.acquire() as conn:  # system-infra: scope-less, audited
            rows = await conn.fetch(
                "SELECT id FROM models WHERE is_enabled = true"
            )
        return {r["id"] for r in rows}
