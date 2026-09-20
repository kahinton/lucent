"""Authentication and user-session repository."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from asyncpg import Pool


class AuthRepository:
    """Repository for authentication, sessions, and password state."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def find_user_by_login(self, username: str) -> dict[str, Any] | None:
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name,
                   avatar_url, provider_metadata, is_active, created_at, updated_at,
                   last_login_at, role, password_hash, force_password_change
            FROM users
            WHERE (LOWER(email) = LOWER($1) OR LOWER(display_name) = LOWER($1))
              AND is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, username)
        return dict(row) if row is not None else None

    async def create_session(
        self, token_hash: str, user_id: UUID, expires_at: Any
    ) -> None:
        query = """
            INSERT INTO user_sessions (token_hash, user_id, expires_at, last_seen_at)
            VALUES ($1, $2, $3, NOW())
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, token_hash, str(user_id), expires_at)

    async def rotate_session(
        self, current_token_hash: str, new_token_hash: str, expires_at: Any
    ) -> bool:
        query = """
            UPDATE user_sessions
            SET token_hash = $1, expires_at = $2, last_seen_at = NOW()
            WHERE token_hash = $3
        """
        async with self.pool.acquire() as conn:
            status = await conn.execute(query, new_token_hash, expires_at, current_token_hash)
        return status != "UPDATE 0"

    async def validate_session(self, token_hash: str) -> dict[str, Any] | None:
        query = """
            SELECT u.id, u.external_id, u.provider, u.organization_id, u.email,
                   u.display_name, u.avatar_url, u.provider_metadata, u.is_active,
                   u.created_at, u.updated_at, u.last_login_at, u.role,
                   u.force_password_change, s.expires_at AS session_expires_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = $1
              AND s.expires_at > NOW()
              AND u.is_active = true
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, token_hash)
            if row is not None:
                await conn.execute(
                    "UPDATE user_sessions SET last_seen_at = NOW() WHERE token_hash = $1",
                    token_hash,
                )
        return dict(row) if row is not None else None

    async def organization_allows_access(self, user: dict[str, Any]) -> bool:
        if user.get("role") == "hyperadmin":
            return True

        organization_id = user.get("organization_id")
        if organization_id is None:
            return True

        async with self.pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM organizations WHERE id = $1",
                str(organization_id),
            )
        return status == "active"

    async def destroy_session(self, token_hash: str) -> bool:
        async with self.pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM user_sessions WHERE token_hash = $1", token_hash
            )
        return status != "DELETE 0"

    async def destroy_all_user_sessions(
        self, user_id: UUID, *, except_token_hash: str | None = None
    ) -> None:
        if except_token_hash:
            query = """
                DELETE FROM user_sessions
                WHERE user_id = $1 AND token_hash != $2
            """
            params: tuple[Any, ...] = (str(user_id), except_token_hash)
        else:
            query = "DELETE FROM user_sessions WHERE user_id = $1"
            params = (str(user_id),)
        async with self.pool.acquire() as conn:
            await conn.execute(query, *params)

    async def set_user_password(
        self, user_id: UUID, password_hash: str, *, force_change: bool
    ) -> None:
        query = (
            "UPDATE users SET password_hash = $1, force_password_change = $2 WHERE id = $3"
        )
        async with self.pool.acquire() as conn:
            await conn.execute(query, password_hash, force_change, str(user_id))

    async def clear_force_password_change(self, user_id: UUID) -> None:
        query = "UPDATE users SET force_password_change = false WHERE id = $1"
        async with self.pool.acquire() as conn:
            await conn.execute(query, str(user_id))

    async def has_active_human_user(self) -> bool:
        query = """
            SELECT EXISTS(
                SELECT 1
                FROM users
                WHERE is_active = TRUE
                  AND role <> 'daemon'
                  AND COALESCE(external_id, '') NOT LIKE '%-service%'
                  AND COALESCE(email, '') NOT LIKE '%@lucent.local'
                LIMIT 1
            )
        """
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(query))
