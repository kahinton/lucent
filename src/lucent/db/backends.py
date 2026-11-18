"""Database repository for engine backends.

A backend is one engine + one provider family + one credential/config binding
(see migration 136). Models and tasks may reference a backend; when they do not,
engine resolution falls back to the configured default engine.

Models are global (``models.organization_id IS NULL`` = shared) while
credentials are per-organization, so a backend that binds a credential is
org-scoped. Migration 165 makes that ownership total: there are no shared
backends, and every read here requires an organization id. A lookup without one
raises :class:`MissingOrganizationScope` rather than widening to another
tenant's rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from lucent.db.pool import scoped_acquire


class MissingOrganizationScope(RuntimeError):
    """A backend lookup was attempted without an organization id.

    Backends bind per-organization credentials, so there is no shared/global
    scope to read. Callers must pass the tenant's organization id; reading
    without one would either serve another tenant's provider or invent a
    deployment-wide default that no tenant configured.
    """


def _require_org_id(org_id: str | None, caller: str) -> str:
    """Return ``org_id`` or raise the named scope error for ``caller``."""
    org = str(org_id).strip() if org_id is not None else ""
    if not org:
        raise MissingOrganizationScope(
            f"{caller} requires an organization id: providers (engine backends) "
            "are per-organization and there is no shared scope to read. "
            "Pass org_id=<organization id>."
        )
    return org


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
        org_id: str,
    ) -> dict:
        org_id = _require_org_id(org_id, "BackendRepository.create")
        now = datetime.now(timezone.utc)
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """INSERT INTO backends (organization_id, name, engine, provider,
                   config, secret_refs, is_enabled, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)
                   RETURNING *""",
                UUID(org_id),
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

    async def get(self, backend_id: str, *, org_id: str) -> dict | None:
        """By-ID lookup, restricted to a backend owned by ``org_id``.

        A backend owned by another organization (or, before migration 165, a
        shared org-NULL row) is unreachable here — not merely filtered out of a
        list, since callers bind credentials and route traffic through the row.
        """
        org_id = _require_org_id(org_id, "BackendRepository.get")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM backends WHERE id = $1 AND organization_id = $2",
                UUID(str(backend_id)),
                UUID(org_id),
            )
        return dict(row) if row else None

    async def list(
        self,
        *,
        org_id: str,
        engine: str | None = None,
        provider: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict]:
        """List the backends owned by ``org_id``.

        ``org_id`` is required: there are no shared (org-NULL) backends to merge
        in, and a listing without a tenant would be a cross-tenant read.
        """
        org_id = _require_org_id(org_id, "BackendRepository.list")
        query = "SELECT * FROM backends WHERE organization_id = $1"
        params: list[object] = [UUID(org_id)]
        idx = 2
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
        query += " ORDER BY engine, provider, name"
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
        org_id: str,
    ) -> dict | None:
        org_id = _require_org_id(org_id, "BackendRepository.update")
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
        where += f" AND organization_id = ${idx}"
        params.append(UUID(org_id))

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE backends SET {', '.join(sets)} {where} RETURNING *",
                *params,
            )
        return dict(row) if row else None

    async def delete(self, backend_id: str, *, org_id: str) -> bool:
        org_id = _require_org_id(org_id, "BackendRepository.delete")
        query = "DELETE FROM backends WHERE id = $1 AND organization_id = $2"
        params: list[object] = [UUID(str(backend_id)), UUID(org_id)]
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *params)
        return result.endswith(" 1")


def resolve_backend_for_model(
    model: dict,
    *,
    org_id: str,
    candidates: list[dict],
) -> dict | None:
    """Pick the backend a model should run through, or None to use the default.

    Precedence:
      1. The model's explicit ``backend_id``, if this organization owns it.
      2. An org-scoped backend matching the model's pinned (engine, provider).

    Only the organization's own rows are considered. A shared (org-NULL) row is
    never a candidate — resolution must not reach a provider this tenant does
    not own. Models with no pinned engine and no backend_id return None — they
    resolve through the configured default engine, exactly as before backends
    existed.
    """
    org_id = _require_org_id(org_id, "resolve_backend_for_model")
    enabled = [
        b
        for b in candidates
        if b.get("is_enabled", True)
        and str(b.get("organization_id") or "") == org_id
    ]
    backend_id = model.get("backend_id")
    if backend_id:
        # An explicit binding is exclusive: falling through to an (engine,
        # provider) match would silently run the model somewhere its author did
        # not choose.
        return next((b for b in enabled if str(b["id"]) == str(backend_id)), None)

    engine = model.get("engine")
    provider = model.get("provider")
    if not engine:
        return None

    for backend in enabled:
        if backend["engine"] == engine and backend["provider"] == provider:
            return backend
    return None
