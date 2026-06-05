"""Repository for enterprise credential storage and OAuth state challenges."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


class CredentialRepository:
    """CRUD and token lifecycle operations for enterprise credentials."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create_credential(
        self,
        *,
        organization_id: str,
        integration_type: str,
        credential_kind: str,
        scope_type: str,
        owner_user_id: str | None,
        owner_agent_id: str | None,
        display_name: str,
        scopes: list[str],
        encrypted_secret_payload: bytes,
        encrypted_metadata: bytes | None,
        access_token_expires_at: datetime | None,
        refresh_token_expires_at: datetime | None,
        created_by: str,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO enterprise_credentials (
                    organization_id, integration_type, credential_kind,
                    scope_type, owner_user_id, owner_agent_id,
                    display_name, scopes, encrypted_secret_payload,
                    encrypted_metadata, access_token_expires_at,
                    refresh_token_expires_at, created_by
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13)
                RETURNING *
                """,
                UUID(organization_id),
                integration_type,
                credential_kind,
                scope_type,
                UUID(owner_user_id) if owner_user_id else None,
                UUID(owner_agent_id) if owner_agent_id else None,
                display_name,
                json.dumps(scopes),
                encrypted_secret_payload,
                encrypted_metadata,
                access_token_expires_at,
                refresh_token_expires_at,
                UUID(created_by),
            )
        return self._row_to_dict(row)

    async def get_credential(
        self, credential_id: str, organization_id: str
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM enterprise_credentials WHERE id = $1 AND organization_id = $2",
                UUID(credential_id),
                UUID(organization_id),
            )
        return self._row_to_dict(row) if row else None

    async def list_credentials(
        self,
        organization_id: str,
        *,
        scope_type: str | None = None,
        owner_user_id: str | None = None,
        owner_agent_id: str | None = None,
        integration_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["organization_id = $1"]
        params: list[Any] = [UUID(organization_id)]
        idx = 2

        if scope_type:
            conditions.append(f"scope_type = ${idx}")
            params.append(scope_type)
            idx += 1
        if owner_user_id:
            conditions.append(f"owner_user_id = ${idx}")
            params.append(UUID(owner_user_id))
            idx += 1
        if owner_agent_id:
            conditions.append(f"owner_agent_id = ${idx}")
            params.append(UUID(owner_agent_id))
            idx += 1
        if integration_type:
            conditions.append(f"integration_type = ${idx}")
            params.append(integration_type)
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        params.append(min(limit, 200))
        query = (
            f"SELECT * FROM enterprise_credentials WHERE {' AND '.join(conditions)} "
            f"ORDER BY created_at DESC LIMIT ${idx}"
        )

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._row_to_dict(row) for row in rows]

    async def update_credential(
        self,
        credential_id: str,
        organization_id: str,
        *,
        updated_by: str,
        display_name: str | None = None,
        scopes: list[str] | None = None,
        encrypted_secret_payload: bytes | None = None,
        encrypted_metadata: bytes | None = None,
        access_token_expires_at: datetime | None = None,
        refresh_token_expires_at: datetime | None = None,
        status: str | None = None,
        rotate_token_version: bool = False,
    ) -> dict[str, Any] | None:
        sets = ["updated_by = $3", "updated_at = NOW()"]
        params: list[Any] = [UUID(credential_id), UUID(organization_id), UUID(updated_by)]
        idx = 4

        if display_name is not None:
            sets.append(f"display_name = ${idx}")
            params.append(display_name)
            idx += 1
        if scopes is not None:
            sets.append(f"scopes = ${idx}::jsonb")
            params.append(json.dumps(scopes))
            idx += 1
        if encrypted_secret_payload is not None:
            sets.append(f"encrypted_secret_payload = ${idx}")
            params.append(encrypted_secret_payload)
            idx += 1
            sets.append("token_rotated_at = NOW()")
            if rotate_token_version:
                sets.append("refresh_token_version = refresh_token_version + 1")
        if encrypted_metadata is not None:
            sets.append(f"encrypted_metadata = ${idx}")
            params.append(encrypted_metadata)
            idx += 1
        if access_token_expires_at is not None:
            sets.append(f"access_token_expires_at = ${idx}")
            params.append(access_token_expires_at)
            idx += 1
        if refresh_token_expires_at is not None:
            sets.append(f"refresh_token_expires_at = ${idx}")
            params.append(refresh_token_expires_at)
            idx += 1
        if status is not None:
            sets.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE enterprise_credentials SET {', '.join(sets)} "
                f"WHERE id = $1 AND organization_id = $2 RETURNING *",
                *params,
            )
        return self._row_to_dict(row) if row else None

    async def mark_refreshed(
        self,
        credential_id: str,
        organization_id: str,
        *,
        updated_by: str,
        encrypted_secret_payload: bytes,
        access_token_expires_at: datetime | None,
        refresh_token_expires_at: datetime | None,
        rotated_refresh_token: bool,
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE enterprise_credentials
                SET encrypted_secret_payload = $3,
                    access_token_expires_at = $4,
                    refresh_token_expires_at = $5,
                    last_refreshed_at = NOW(),
                    updated_at = NOW(),
                    updated_by = $6,
                    token_rotated_at = NOW(),
                    refresh_token_version = CASE
                        WHEN $7 THEN refresh_token_version + 1
                        ELSE refresh_token_version
                    END
                WHERE id = $1 AND organization_id = $2
                RETURNING *
                """,
                UUID(credential_id),
                UUID(organization_id),
                encrypted_secret_payload,
                access_token_expires_at,
                refresh_token_expires_at,
                UUID(updated_by),
                rotated_refresh_token,
            )
        return self._row_to_dict(row) if row else None

    async def delete_credential(self, credential_id: str, organization_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM enterprise_credentials WHERE id = $1 AND organization_id = $2",
                UUID(credential_id),
                UUID(organization_id),
            )
        return result != "DELETE 0"

    async def create_oauth_state(
        self,
        *,
        organization_id: str,
        provider: str,
        state_hash: str,
        pkce_verifier: str | None,
        redirect_uri: str,
        requested_scopes: list[str],
        scope_type: str,
        owner_user_id: str | None,
        owner_agent_id: str | None,
        created_by: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO oauth2_state_challenges (
                    organization_id, provider, state_hash, pkce_verifier,
                    redirect_uri, requested_scopes, scope_type,
                    owner_user_id, owner_agent_id, created_by, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
                RETURNING *
                """,
                UUID(organization_id),
                provider,
                state_hash,
                pkce_verifier,
                redirect_uri,
                json.dumps(requested_scopes),
                scope_type,
                UUID(owner_user_id) if owner_user_id else None,
                UUID(owner_agent_id) if owner_agent_id else None,
                UUID(created_by),
                expires_at,
            )
        return self._row_to_dict(row)

    async def consume_oauth_state(
        self,
        *,
        organization_id: str,
        provider: str,
        state_hash: str,
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE oauth2_state_challenges
                SET consumed_at = NOW()
                WHERE organization_id = $1
                  AND provider = $2
                  AND state_hash = $3
                  AND consumed_at IS NULL
                  AND expires_at > NOW()
                RETURNING *
                """,
                UUID(organization_id),
                provider,
                state_hash,
            )
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in ("scopes", "requested_scopes"):
            value = result.get(key)
            if isinstance(value, str):
                try:
                    result[key] = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    result[key] = []
        return result
