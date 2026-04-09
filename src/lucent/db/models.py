"""Database repository for model management."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import asyncpg


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
        is_enabled: bool = True,
        org_id: str | None = None,
        engine: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO models (id, provider, name, category, api_model_id,
                   context_window, supports_tools, supports_vision, notes, tags,
                   is_enabled, organization_id, engine, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$14)
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
                is_enabled,
                UUID(org_id) if org_id else None,
                engine,
                now,
            )
        return dict(row)

    async def update_model(self, model_id: str, **kwargs) -> dict | None:
        allowed = {
            "provider", "name", "category", "api_model_id", "context_window",
            "supports_tools", "supports_vision", "notes", "tags", "is_enabled",
            "engine",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return await self.get_model(model_id)

        updates["updated_at"] = datetime.now(timezone.utc)
        set_parts = []
        params = []
        for i, (key, val) in enumerate(updates.items(), start=1):
            set_parts.append(f"{key} = ${i}")
            params.append(val)

        params.append(model_id)
        query = f"UPDATE models SET {', '.join(set_parts)} WHERE id = ${len(params)} RETURNING *"

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

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
