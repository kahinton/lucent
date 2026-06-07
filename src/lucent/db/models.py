"""Database repository for model management."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from lucent.access_control import build_access_clause


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
        *,
        requester_user_id: str | None = None,
        requester_role: str | None = None,
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

        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), requester_role or "member"])
            uid_idx, role_idx = idx, idx + 1
            idx += 2
            # Models are a global, cross-org catalog. Constrain grant matching to
            # the requester's organization so one org's grants cannot expose a
            # shared model to another org.
            org_clause_param = None
            if org_id:
                params.append(UUID(org_id))
                org_clause_param = idx
                idx += 1
            base += " AND " + build_access_clause(
                resource_type="model", uid_param=uid_idx, role_param=role_idx,
                org_param=org_clause_param,
            )

        count_query = f"SELECT COUNT(*) AS total{base}"
        query = f"SELECT *{base} ORDER BY provider, name LIMIT ${idx} OFFSET ${idx + 1}"
        params_with_page = [*params, limit, offset]

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

    async def get_model(self, model_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM models WHERE id = $1", model_id)
        return dict(row) if row else None

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
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO models (id, provider, name, category, api_model_id,
                   context_window, supports_tools, supports_vision, notes, tags,
                   reasoning_efforts, is_enabled, organization_id, engine,
                   discovery_source, is_custom, discovery_metadata, created_at,
                   updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                       $16,$17::jsonb,$18,$18)
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
            )
        return dict(row)

    async def update_model(self, model_id: str, **kwargs) -> dict | None:
        allowed = {
            "provider", "name", "category", "api_model_id", "context_window",
            "supports_tools", "supports_vision", "notes", "tags", "is_enabled",
            "reasoning_efforts", "engine", "discovery_source", "is_custom",
            "last_discovered_at", "discovery_metadata",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return await self.get_model(model_id)

        updates["updated_at"] = datetime.now(timezone.utc)
        set_parts = []
        params = []
        for i, (key, val) in enumerate(updates.items(), start=1):
            if key == "discovery_metadata":
                set_parts.append(f"{key} = ${i}::jsonb")
                params.append(_jsonb_param(val))
            else:
                set_parts.append(f"{key} = ${i}")
                params.append(val)

        params.append(model_id)
        query = f"UPDATE models SET {', '.join(set_parts)} WHERE id = ${len(params)} RETURNING *"

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
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
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for m in models:
                    await conn.fetchrow(
                        """
                        INSERT INTO models (
                            id, provider, name, category, api_model_id,
                            context_window, supports_tools, supports_vision,
                            notes, tags, is_enabled, organization_id, engine,
                            reasoning_efforts, discovery_source, is_custom, last_discovered_at,
                            discovery_metadata, created_at, updated_at
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,false,$11,$12,
                            $13,'provider',false,$14,$15::jsonb,$14,$14
                        )
                        ON CONFLICT (id) DO UPDATE SET
                            provider = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.provider
                                ELSE EXCLUDED.provider
                            END,
                            name = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.name
                                ELSE EXCLUDED.name
                            END,
                            category = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.category
                                ELSE EXCLUDED.category
                            END,
                            api_model_id = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.api_model_id
                                ELSE EXCLUDED.api_model_id
                            END,
                            context_window = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.context_window
                                ELSE EXCLUDED.context_window
                            END,
                            supports_tools = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.supports_tools
                                ELSE EXCLUDED.supports_tools
                            END,
                            supports_vision = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.supports_vision
                                ELSE EXCLUDED.supports_vision
                            END,
                            notes = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.notes
                                ELSE EXCLUDED.notes
                            END,
                            tags = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.tags
                                ELSE EXCLUDED.tags
                            END,
                            engine = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.engine
                                ELSE EXCLUDED.engine
                            END,
                            reasoning_efforts = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.reasoning_efforts
                                ELSE EXCLUDED.reasoning_efforts
                            END,
                            discovery_source = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.discovery_source
                                ELSE EXCLUDED.discovery_source
                            END,
                            is_custom = CASE
                                WHEN models.discovery_source = 'manual' OR models.is_custom
                                    THEN models.is_custom
                                ELSE EXCLUDED.is_custom
                            END,
                            last_discovered_at = EXCLUDED.last_discovered_at,
                            discovery_metadata = EXCLUDED.discovery_metadata,
                            updated_at = EXCLUDED.updated_at
                        RETURNING *
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

    async def toggle_model(self, model_id: str, enabled: bool) -> dict | None:
        return await self.update_model(model_id, is_enabled=enabled)

    async def delete_model(self, model_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM models WHERE id = $1", model_id)
        return result == "DELETE 1"

    async def get_enabled_model_ids(self) -> set[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM models WHERE is_enabled = true"
            )
        return {r["id"] for r in rows}
