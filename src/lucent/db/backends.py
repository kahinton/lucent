"""Database repository for engine backends.

A backend is one engine + one provider family + one credential/config binding
(see migration 136). Models and tasks may reference a backend; when they do not,
engine resolution falls back to the configured default engine.

Models are global (``models.organization_id IS NULL`` = shared) while
credentials are per-organization, so a backend that binds a credential is
org-scoped. ``list`` returns an organization's rows plus shared rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from lucent.db.pool import scoped_acquire


class BackendRepository:
    """CRUD operations for the backends table."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        *,
        name: str,
        engine: str,
        provider: str,
        config: dict | None = None,
        secret_refs: dict | None = None,
        is_enabled: bool = True,
        org_id: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """INSERT INTO backends (organization_id, name, engine, provider,
                   config, secret_refs, is_enabled, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)
                   RETURNING *""",
                UUID(org_id) if org_id else None,
                name,
                engine,
                provider,
                # Pass dicts: the pool registers a jsonb codec that encodes them.
                # Pre-serializing to str here would be double-encoded.
                config or {},
                secret_refs or {},
                is_enabled,
                now,
            )
        return dict(row)

    async def get(self, backend_id: str, *, org_id: str | None = None) -> dict | None:
        """By-ID lookup; when org_id is given the row must be org-scoped or shared."""
        async with self.pool.acquire() as conn:
            query = "SELECT * FROM backends WHERE id = $1"
            params: list[object] = [UUID(str(backend_id))]
            if org_id:
                query += " AND (organization_id IS NULL OR organization_id = $2)"
                params.append(UUID(org_id))
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def list(
        self,
        *,
        org_id: str | None = None,
        engine: str | None = None,
        provider: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict]:
        """List backends for an organization plus shared (org-NULL) rows."""
        query = "SELECT * FROM backends WHERE 1=1"
        params: list[object] = []
        idx = 1
        if org_id:
            query += f" AND (organization_id IS NULL OR organization_id = ${idx})"
            params.append(UUID(org_id))
            idx += 1
        else:
            query += " AND organization_id IS NULL"
        if engine:
            query += f" AND engine = ${idx}"
            params.append(engine)
            idx += 1
        if provider:
            query += f" AND provider = ${idx}"
            params.append(provider)
            idx += 1
        if enabled_only:
            query += " AND is_enabled = true"
        query += " ORDER BY organization_id NULLS LAST, engine, provider, name"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]

    async def update(
        self,
        backend_id: str,
        *,
        name: str | None = None,
        config: dict | None = None,
        secret_refs: dict | None = None,
        is_enabled: bool | None = None,
        org_id: str | None = None,
    ) -> dict | None:
        sets: list[str] = []
        params: list[object] = []
        idx = 1

        def _add(column: str, value: object) -> None:
            nonlocal idx
            sets.append(f"{column} = ${idx}")
            params.append(value)
            idx += 1

        if name is not None:
            _add("name", name)
        if config is not None:
            _add("config", config)
        if secret_refs is not None:
            _add("secret_refs", secret_refs)
        if is_enabled is not None:
            _add("is_enabled", is_enabled)
        if not sets:
            return await self.get(backend_id, org_id=org_id)

        _add("updated_at", datetime.now(timezone.utc))
        where = f"WHERE id = ${idx}"
        params.append(UUID(str(backend_id)))
        idx += 1
        if org_id:
            where += f" AND organization_id = ${idx}"
            params.append(UUID(org_id))

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE backends SET {', '.join(sets)} {where} RETURNING *",
                *params,
            )
        return dict(row) if row else None

    async def delete(self, backend_id: str, *, org_id: str | None = None) -> bool:
        query = "DELETE FROM backends WHERE id = $1"
        params: list[object] = [UUID(str(backend_id))]
        if org_id:
            query += " AND organization_id = $2"
            params.append(UUID(org_id))
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *params)
        return result.endswith(" 1")


def resolve_backend_for_model(
    model: dict,
    *,
    org_id: str | None,
    candidates: list[dict],
) -> dict | None:
    """Pick the backend a model should run through, or None to use the default.

    Precedence:
      1. The model's explicit ``backend_id``.
      2. An org-scoped backend matching the model's pinned (engine, provider).
      3. A shared backend matching the model's pinned (engine, provider).

    Models with no pinned engine and no backend_id return None — they resolve
    through the configured default engine, exactly as before backends existed.
    """
    enabled = [b for b in candidates if b.get("is_enabled", True)]
    backend_id = model.get("backend_id")
    if backend_id:
        match = next((b for b in enabled if str(b["id"]) == str(backend_id)), None)
        if match is not None:
            return match

    engine = model.get("engine")
    provider = model.get("provider")
    if not engine:
        return None

    def _matches(backend: dict) -> bool:
        return backend["engine"] == engine and backend["provider"] == provider

    def _is_org(backend: dict) -> bool:
        return str(backend.get("organization_id") or "") == str(org_id or "")

    org_matches = [b for b in enabled if _matches(b) and _is_org(b)]
    if org_matches:
        return org_matches[0]
    shared = [b for b in enabled if _matches(b) and not b.get("organization_id")]
    return shared[0] if shared else None
