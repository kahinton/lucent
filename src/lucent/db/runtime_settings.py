"""Database repository for org-scoped runtime settings."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


class RuntimeSettingsRepository:
    """CRUD operations for non-secret runtime settings.

    The repository intentionally accepts only already-validated values. The
    allowlist and type validation live in :mod:`lucent.settings` so callers can
    use the same rules for env fallbacks, UI forms, and DB persistence.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def list_settings(
        self,
        organization_id: UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        """List runtime setting rows, optionally scoped to one organization."""
        if organization_id:
            query = """
                SELECT id, organization_id, key, value, value_type,
                       created_by, updated_by, created_at, updated_at
                FROM runtime_settings
                WHERE organization_id = $1
                ORDER BY key
            """
            params = [UUID(str(organization_id))]
        else:
            query = """
                SELECT id, organization_id, key, value, value_type,
                       created_by, updated_by, created_at, updated_at
                FROM runtime_settings
                ORDER BY organization_id, key
            """
            params = []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]

    async def get_setting(
        self,
        organization_id: UUID | str,
        key: str,
    ) -> dict[str, Any] | None:
        """Get a single runtime setting row by org and key."""
        query = """
            SELECT id, organization_id, key, value, value_type,
                   created_by, updated_by, created_at, updated_at
            FROM runtime_settings
            WHERE organization_id = $1 AND key = $2
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, UUID(str(organization_id)), key)
        return dict(row) if row else None

    async def upsert_setting(
        self,
        *,
        organization_id: UUID | str,
        key: str,
        value: Any,
        value_type: str,
        user_id: UUID | str | None,
    ) -> dict[str, Any]:
        """Create or update a runtime setting row after catalog validation."""
        from lucent.settings import (
            get_runtime_setting_definition,
            validate_runtime_setting_value,
        )

        definition = get_runtime_setting_definition(key)
        if not definition or not definition.editable:
            raise ValueError("Unknown or read-only runtime setting.")
        if value_type != definition.value_type:
            raise ValueError(
                f"Runtime setting {key} requires value_type={definition.value_type}."
            )
        value = validate_runtime_setting_value(key, value)
        query = """
            INSERT INTO runtime_settings
                (organization_id, key, value, value_type, created_by, updated_by)
            VALUES ($1, $2, $3::jsonb, $4, $5, $5)
            ON CONFLICT (organization_id, key) DO UPDATE SET
                value = EXCLUDED.value,
                value_type = EXCLUDED.value_type,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            RETURNING id, organization_id, key, value, value_type,
                      created_by, updated_by, created_at, updated_at
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                UUID(str(organization_id)),
                key,
                value,
                value_type,
                UUID(str(user_id)) if user_id else None,
            )
        return dict(row)

    async def delete_setting(
        self,
        organization_id: UUID | str,
        key: str,
    ) -> dict[str, Any] | None:
        """Delete a DB override so the setting falls back to env/default."""
        query = """
            DELETE FROM runtime_settings
            WHERE organization_id = $1 AND key = $2
            RETURNING id, organization_id, key, value, value_type,
                      created_by, updated_by, created_at, updated_at
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, UUID(str(organization_id)), key)
        return dict(row) if row else None
