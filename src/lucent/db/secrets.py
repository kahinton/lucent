"""Repository for scoped secret persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from asyncpg import Connection, Pool

from lucent.db.pool import (
    AuthorizedDatabasePool,
    AuthTablePolicy,
    get_authorized_pool_for_user,
    scoped_acquire_on,
)

if TYPE_CHECKING:
    from lucent.secrets.base import SecretScope


SECRETS_AUTH_POLICY: Final[AuthTablePolicy] = AuthTablePolicy("secrets", direct_columns=())


async def get_authorized_secrets_pool(
    pool: Pool,
    user: dict[str, Any],
    required_clearance: str = "read",
) -> "AuthorizedDatabasePool":
    """Return a pool whose secret reads use auth-ID clearances.

    Clearance-driven reads see secrets the principal owns (migration 123),
    ones shared with their groups (migration 124, read role) or granted to
    them. Scope-predicated provider access and writes stay on the scoped
    pool — the authorized connection is read-only and system_managed secrets
    carry no clearances.
    """
    return await get_authorized_pool_for_user(
        pool,
        user,
        SECRETS_AUTH_POLICY,
        required_clearance,
    )


class SecretRepository:
    """Encapsulate all SQL access for the secrets table."""

    def __init__(self, pool: Pool):
        self.pool = pool

    def _scope_filter(self, scope: "SecretScope") -> tuple[str, list[Any]]:
        conditions = ["organization_id = $1"]
        params: list[Any] = [UUID(scope.organization_id)]
        placeholder = 2
        if scope.system_managed:
            conditions.extend(
                [
                    "system_managed = true",
                    "owner_user_id IS NULL",
                    "owner_group_id IS NULL",
                ]
            )
        else:
            conditions.append("system_managed = false")
        if scope.owner_user_id:
            conditions.append(f"owner_user_id = ${placeholder}")
            params.append(UUID(scope.owner_user_id))
            placeholder += 1
        if scope.owner_group_id:
            conditions.append(f"owner_group_id = ${placeholder}")
            params.append(UUID(scope.owner_group_id))
        return " AND ".join(conditions), params

    async def get_encrypted_value(self, key: str, scope: "SecretScope") -> bytes | None:
        where, params = self._scope_filter(scope)
        params.append(key)
        async with scoped_acquire_on(self.pool, organization_id=scope.organization_id) as conn:
            row = await conn.fetchrow(
                f"SELECT encrypted_value FROM secrets WHERE key = ${len(params)} AND {where}",
                *params,
            )
        return None if row is None else bytes(row["encrypted_value"])

    async def upsert_encrypted_value(
        self,
        key: str,
        encrypted_value: bytes,
        scope: "SecretScope",
    ) -> None:
        owner_user_id = UUID(scope.owner_user_id) if scope.owner_user_id else None
        owner_group_id = UUID(scope.owner_group_id) if scope.owner_group_id else None
        async with scoped_acquire_on(self.pool, organization_id=scope.organization_id) as conn:
            async with conn.transaction():
                where, params = self._scope_filter(scope)
                result = await conn.execute(
                    f"UPDATE secrets SET encrypted_value = ${len(params) + 1}, "
                    f"updated_at = NOW() WHERE key = ${len(params) + 2} AND {where}",
                    *params,
                    encrypted_value,
                    key,
                )
                if result == "UPDATE 0":
                    await conn.execute(
                        "INSERT INTO secrets (key, encrypted_value, owner_user_id, "
                        "owner_group_id, organization_id, system_managed) "
                        "VALUES ($1, $2, $3, $4, $5, $6)",
                        key,
                        encrypted_value,
                        owner_user_id,
                        owner_group_id,
                        UUID(scope.organization_id),
                        scope.system_managed,
                    )

    async def delete(self, key: str, scope: "SecretScope") -> bool:
        where, params = self._scope_filter(scope)
        params.append(key)
        async with scoped_acquire_on(self.pool, organization_id=scope.organization_id) as conn:
            result = await conn.execute(
                f"DELETE FROM secrets WHERE key = ${len(params)} AND {where}",
                *params,
            )
        return result != "DELETE 0"

    async def list_keys(self, scope: "SecretScope") -> list[str]:
        where, params = self._scope_filter(scope)
        async with scoped_acquire_on(self.pool, organization_id=scope.organization_id) as conn:
            rows = await conn.fetch(
                f"SELECT key FROM secrets WHERE {where} ORDER BY key",
                *params,
            )
        return [str(row["key"]) for row in rows]

    async def get_id(self, key: str, scope: "SecretScope") -> str | None:
        where, params = self._scope_filter(scope)
        params.append(key)
        async with scoped_acquire_on(self.pool, organization_id=scope.organization_id) as conn:
            row = await conn.fetchrow(
                f"SELECT id FROM secrets WHERE key = ${len(params)} AND {where}",
                *params,
            )
        return None if row is None else str(row["id"])

    async def get_usable_encrypted_value(
        self,
        key: str,
        organization_id: str,
    ) -> dict[str, Any] | None:
        """By-key value fetch on the clearance path.

        Requires an ``AuthorizedDatabasePool``: exactly the rows the
        clearance-driven listings show (owned via migration 123, group
        shares via 124, granted explicitly — no role-based org-wide
        branch) are readable here. The org predicate stays as defense in
        depth; ``app.user_id`` (bound on every authorized connection)
        orders the caller's own row first, matching the legacy
        own-before-group probe order.

        Fail closed on a plain pool rather than widening to org scope;
        system_managed secrets have no clearances and stay reachable only
        through their scoped system paths.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Clearance-driven secret value fetch requires an "
                "AuthorizedDatabasePool built for the secrets policy"
            )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.id, s.encrypted_value, s.owner_user_id, s.owner_group_id
                FROM secrets s
                WHERE s.key = $1 AND s.organization_id = $2
                ORDER BY (
                    s.owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
                ) DESC NULLS LAST,
                s.created_at ASC
                LIMIT 1
                """,
                key,
                UUID(organization_id),
            )
        return dict(row) if row else None

    async def list_keys_cleared(self, organization_id: str) -> list[dict[str, Any]]:
        """Clearance-driven key listing (no values), mirroring list_scoped.

        Requires an ``AuthorizedDatabasePool``; same rules as the web
        listing — owner (123), group shares (124), and grants.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Clearance-driven secret listing requires an "
                "AuthorizedDatabasePool built for the secrets policy"
            )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.key, s.owner_user_id, s.owner_group_id
                FROM secrets s
                WHERE s.organization_id = $1
                ORDER BY s.key ASC
                """,
                UUID(organization_id),
            )
        return [dict(row) for row in rows]

    async def list_scoped(
        self,
        *,
        organization_id: str,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Paginated clearance-driven listing of secrets, with owner names.

        Requires an ``AuthorizedDatabasePool``: what the caller sees comes
        from auth_clearances (owned via migration 123, group-shared with a
        read role via migration 124, granted explicitly) and nothing else —
        there is no role-based org-wide branch. Fail closed when the caller
        forgot the authorized pool rather than widening to org scope.
        """
        if not isinstance(self.pool, AuthorizedDatabasePool):
            raise TypeError(
                "Clearance-driven secret listing requires an "
                "AuthorizedDatabasePool built for the secrets policy"
            )
        # Ownership (123), group shares (124) and grants are all expressed in
        # auth_clearances. The org predicate stays as defense in depth —
        # auth_ids are globally unique, so this should never filter anything.
        total = 0
        items: list[dict[str, Any]] = []
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM secrets s "
                "WHERE s.organization_id = $1",
                UUID(organization_id),
            )
            total = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                """
                SELECT
                    s.key,
                    s.owner_user_id,
                    s.owner_group_id,
                    s.created_at,
                    COALESCE(u.display_name, u.email, 'Unknown user') AS owner_user_name,
                    g.name AS owner_group_name
                FROM secrets s
                LEFT JOIN users u ON u.id = s.owner_user_id
                LEFT JOIN groups g ON g.id = s.owner_group_id
                WHERE s.organization_id = $1
                ORDER BY s.created_at DESC, s.key ASC
                LIMIT $2 OFFSET $3
                """,
                UUID(organization_id),
                limit,
                offset,
            )
            items = [dict(row) for row in rows]
        return total, items


class SecretMigrationRepository:
    """Operate on every secret row for offline encryption migration."""

    def __init__(self, conn: Connection):
        self.conn = conn

    async def list_ciphertexts(self) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            "SELECT id, key, encrypted_value FROM secrets ORDER BY created_at, id"
        )
        return [dict(row) for row in rows]

    async def update_ciphertext(self, secret_id: UUID, encrypted_value: bytes) -> None:
        async with self.conn.transaction():
            await self.conn.execute(
                "UPDATE secrets SET encrypted_value = $1, updated_at = NOW() "
                "WHERE id = $2",
                encrypted_value,
                secret_id,
            )
