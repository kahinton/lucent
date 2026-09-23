"""Repository for administrative database bootstrap operations."""

from __future__ import annotations

from asyncpg import Connection


class BootstrapRepository:
    """Run role administration on a pre-pool bootstrap connection."""

    def __init__(self, conn: Connection):
        self.conn = conn

    async def role_exists(self, role_name: str) -> bool:
        return bool(
            await self.conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
                role_name,
            )
        )

    async def set_password(self, role_name: str, password: str) -> None:
        safe_role = role_name.replace('"', '""')
        literal = "'" + password.replace("'", "''") + "'"
        await self.conn.execute(
            f'ALTER ROLE "{safe_role}" WITH LOGIN PASSWORD {literal}'
        )
