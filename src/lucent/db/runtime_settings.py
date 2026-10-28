"""Database repository for org-scoped runtime settings."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.pool import runner_guc_preamble, scoped_acquire, tenant_guc_scrub


class RuntimeSettingLockedError(ValueError):
    """An instance operator has locked this setting against tenant changes.

    Subclasses ``ValueError`` so existing "invalid update" handling in the web
    routes and the settings service keeps working: a refused write is a refused
    write, and only the message differs.
    """

    def __init__(self, key: str, reason: str = "", organization_id: Any = None):
        self.key = key
        self.reason = reason or ""
        self.organization_id = organization_id
        detail = f"Setting {key} is locked by an instance administrator."
        if self.reason:
            detail = f"{detail} {self.reason}"
        super().__init__(detail)


class RuntimeSettingsRepository:
    """CRUD operations for non-secret runtime settings.

    The repository intentionally accepts only already-validated values. The
    allowlist and type validation live in :mod:`lucent.settings` so callers can
    use the same rules for env fallbacks, UI forms, and DB persistence.

    It also owns the **lock barrier**. A row in ``runtime_setting_locks`` (153)
    means an instance operator has pinned this setting, and ordinary writes are
    refused with :class:`RuntimeSettingLockedError`. The check runs inside the
    same connection as the write rather than against the in-process settings
    cache, because that cache is per-process: a lock applied by another replica
    or by the ops console would otherwise be invisible and the barrier would be
    advisory at best.

    Only an explicit ``ignore_lock=True`` bypasses it. That flag exists for the
    operator path — the same code that creates the lock — and for nothing else.
    Keeping it an explicit parameter rather than inferring it from the caller's
    role means a future tenant-facing code path cannot acquire the bypass by
    accident.
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

        async with scoped_acquire(organization_id=organization_id) as conn:
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
        async with scoped_acquire(organization_id=organization_id) as conn:
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
        ignore_lock: bool = False,
    ) -> dict[str, Any]:
        """Create or update a runtime setting row after catalog validation.

        Args:
            ignore_lock: Operator-only escape hatch. Set this *only* from code
                that manages the locks themselves; a tenant-facing caller must
                never pass it.

        Raises:
            ValueError: If the key/type is not in the catalog.
            RuntimeSettingLockedError: If an instance operator locked the
                setting and ``ignore_lock`` is false.
        """
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
            SELECT $1, $2, $3::jsonb, $4, $5, $5
            WHERE NOT EXISTS (
                SELECT 1 FROM runtime_settings
                WHERE organization_id = $1 AND key = $2
            )
            RETURNING id, organization_id, key, value, value_type,
                      created_by, updated_by, created_at, updated_at
        """
        update_query = """
            UPDATE runtime_settings SET
                value = $3::jsonb,
                value_type = $4,
                updated_by = $5,
                updated_at = NOW()
            WHERE organization_id = $1 AND key = $2
            RETURNING id, organization_id, key, value, value_type,
                      created_by, updated_by, created_at, updated_at
        """
        creator = UUID(str(user_id)) if user_id else None
        # Update-then-insert: a plain ON CONFLICT DO UPDATE fires the BEFORE
        # INSERT auth_id trigger on the conflict path, leaking an unused
        # auth_ids row each time an existing key is changed.
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            if not ignore_lock:
                await self._raise_if_locked(conn, organization_id, key)
            row = await conn.fetchrow(
                update_query,
                UUID(str(organization_id)),
                key,
                value,
                value_type,
                creator,
            )
            if row is None:
                row = await conn.fetchrow(
                    query,
                    UUID(str(organization_id)),
                    key,
                    value,
                    value_type,
                    creator,
                )
        return dict(row)

    async def delete_setting(
        self,
        organization_id: UUID | str,
        key: str,
        *,
        ignore_lock: bool = False,
    ) -> dict[str, Any] | None:
        """Delete a DB override so the setting falls back to env/default.

        A locked setting cannot be reset by the tenant: "reset to the instance
        default" would otherwise be a one-click way around an operator's pin,
        because the locked value usually *is* the tenant's DB row.
        """
        query = """
            DELETE FROM runtime_settings
            WHERE organization_id = $1 AND key = $2
            RETURNING id, organization_id, key, value, value_type,
                      created_by, updated_by, created_at, updated_at
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            if not ignore_lock:
                await self._raise_if_locked(conn, organization_id, key)
            row = await conn.fetchrow(query, UUID(str(organization_id)), key)
        return dict(row) if row else None

    # -- locks -------------------------------------------------------------
    #
    # A lock is a write barrier, not a value. The effective value always comes
    # from ``runtime_settings`` (or its env/default fallback), so the lock and the
    # value can never disagree. Locking a setting that has no saved row is legal
    # and meaningful: it means "whatever this resolves to now, the tenant may not
    # change it".

    async def _raise_if_locked(
        self,
        conn: asyncpg.Connection,
        organization_id: UUID | str,
        key: str,
    ) -> None:
        """Raise :class:`RuntimeSettingLockedError` when ``key`` is pinned."""
        row = await conn.fetchrow(
            "SELECT reason FROM runtime_setting_locks "
            "WHERE organization_id = $1 AND key = $2",
            UUID(str(organization_id)),
            key,
        )
        if row is not None:
            raise RuntimeSettingLockedError(
                key, row["reason"] or "", organization_id=organization_id
            )

    async def list_locks(
        self,
        organization_id: UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        """List setting locks, optionally scoped to one organization."""
        select = """
            SELECT l.id, l.organization_id, l.key, l.reason,
                   l.locked_by, l.locked_at, l.updated_at,
                   u.display_name AS locked_by_name,
                   u.email        AS locked_by_email
            FROM runtime_setting_locks l
            LEFT JOIN users u ON u.id = l.locked_by
        """
        if organization_id:
            query = f"{select} WHERE l.organization_id = $1 ORDER BY l.key"
            params: list[Any] = [UUID(str(organization_id))]
        else:
            query = f"{select} ORDER BY l.organization_id, l.key"
            params = []

        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]

    async def get_lock(
        self,
        organization_id: UUID | str,
        key: str,
    ) -> dict[str, Any] | None:
        """Return the lock row for one setting, if it is pinned."""
        query = """
            SELECT id, organization_id, key, reason, locked_by, locked_at, updated_at
            FROM runtime_setting_locks
            WHERE organization_id = $1 AND key = $2
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(query, UUID(str(organization_id)), key)
        return dict(row) if row else None

    async def lock_setting(
        self,
        *,
        organization_id: UUID | str,
        key: str,
        user_id: UUID | str | None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Pin one setting so only an instance operator can change it.

        Re-locking an already locked setting updates the reason and the operator,
        which is how a pin is re-justified after a plan change.
        """
        query = """
            INSERT INTO runtime_setting_locks
                (organization_id, key, reason, locked_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (organization_id, key) DO UPDATE SET
                reason = EXCLUDED.reason,
                locked_by = EXCLUDED.locked_by,
                updated_at = NOW()
            RETURNING id, organization_id, key, reason, locked_by,
                      locked_at, updated_at
        """
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                query,
                UUID(str(organization_id)),
                key,
                reason or "",
                UUID(str(user_id)) if user_id else None,
            )
        return dict(row)

    async def lock_settings_bulk(
        self,
        *,
        organization_id: UUID | str,
        entries: list[dict[str, str]],
        user_id: UUID | str | None,
    ) -> list[dict[str, Any]]:
        """Pin many settings for one organization in a single statement.

        One statement rather than a loop because an outside tier application
        applies a whole lock set at a time: a partial tier is a worse outcome
        than a failed call, and a single ``INSERT ... ON CONFLICT`` is atomic on
        its own. Unnesting the arrays keeps it to one round trip regardless of
        how many settings a tier names.

        Args:
            entries: ``[{"key": ..., "reason": ...}]``. An empty list is a no-op.
        """
        if not entries:
            return []
        keys = [str(entry["key"]) for entry in entries]
        reasons = [str(entry.get("reason") or "") for entry in entries]
        query = """
            INSERT INTO runtime_setting_locks
                (organization_id, key, reason, locked_by)
            SELECT $1, k, r, $2
            FROM unnest($3::text[], $4::text[]) AS t(k, r)
            ON CONFLICT (organization_id, key) DO UPDATE SET
                reason = EXCLUDED.reason,
                locked_by = EXCLUDED.locked_by,
                updated_at = NOW()
            RETURNING id, organization_id, key, reason, locked_by,
                      locked_at, updated_at
        """
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                query,
                UUID(str(organization_id)),
                UUID(str(user_id)) if user_id else None,
                keys,
                reasons,
            )
        return [dict(row) for row in rows]

    async def unlock_setting(
        self,
        organization_id: UUID | str,
        key: str,
    ) -> dict[str, Any] | None:
        """Remove a lock. The stored value, if any, is deliberately untouched."""
        query = """
            DELETE FROM runtime_setting_locks
            WHERE organization_id = $1 AND key = $2
            RETURNING id, organization_id, key, reason, locked_by,
                      locked_at, updated_at
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(query, UUID(str(organization_id)), key)
        return dict(row) if row else None

    async def unlock_settings_bulk(
        self,
        organization_id: UUID | str,
        keys: list[str],
    ) -> list[dict[str, Any]]:
        """Remove several locks at once; returns the rows that were released.

        Keys that were not locked are silently absent from the result, so a
        caller treating this as "make sure these are unlocked" is idempotent.
        """
        if not keys:
            return []
        query = """
            DELETE FROM runtime_setting_locks
            WHERE organization_id = $1 AND key = ANY($2::text[])
            RETURNING id, organization_id, key, reason, locked_by,
                      locked_at, updated_at
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                query, UUID(str(organization_id)), [str(key) for key in keys]
            )
        return [dict(row) for row in rows]

    async def list_locks_with_values(
        self,
        *,
        key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every lock on the instance, with the tenant's own stored value.

        This is the cross-tenant monitoring view: "who has this pinned, and to
        what". The stored value is joined in rather than resolved through the
        settings cache because resolving it per organization would mean loading
        every tenant's settings; ``value`` is the DB override and is NULL when the
        tenant inherits the environment/built-in default, which is exactly the
        fact a monitoring caller needs.
        """
        where = ""
        params: list[Any] = []
        if key:
            where = "WHERE l.key = $1"
            params.append(key)
        query = f"""
            SELECT l.organization_id, o.name AS organization_name,
                   l.key, l.reason, l.locked_by, l.locked_at,
                   u.display_name AS locked_by_name,
                   u.email        AS locked_by_email,
                   rs.value       AS db_value
            FROM runtime_setting_locks l
            JOIN organizations o ON o.id = l.organization_id
            LEFT JOIN users u ON u.id = l.locked_by
            LEFT JOIN runtime_settings rs
                   ON rs.organization_id = l.organization_id
                  AND rs.key = l.key
            {where}
            ORDER BY o.name, l.key
        """
        # Cross-tenant read, so it needs an explicit system context: the
        # scoped_acquire guard exists to stop tenant work from running unscoped,
        # and this row set spans every tenant by definition. Same preamble the
        # other instance-wide reads use.
        async with self.pool.acquire() as conn:
            await conn.execute(runner_guc_preamble())
            try:
                rows = await conn.fetch(query, *params)
            finally:
                await conn.execute(tenant_guc_scrub())
        return [dict(row) for row in rows]
