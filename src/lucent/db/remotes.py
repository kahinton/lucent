"""Remote instance repository.

A "remote" is a registered non-local work executor (another Lucent
deployment, or a peer running the shipped remote agent client) that Lucent
can target with whole tasks. The remote authenticates with a dedicated
``lr_``-prefixed API key owned by a per-remote identity user provisioned in
the same transaction as the remote itself.

Ownership: only owners may create/update/revoke remotes; admins may list and
read. All reads and writes are organization-scoped via RLS.
"""

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import bcrypt
from asyncpg import Pool

from lucent.db.pool import (
    preauth_guc_preamble,
    scoped_acquire,
    scoped_acquire_on,
    tenant_guc_scrub,
)
from lucent.logging import get_logger

logger = get_logger(__name__)

REMOTE_KEY_SCOPES = ["remote-agent"]
# Default token lifetime for remote agent keys (30 days). Owners can rotate
# earlier; rotation revokes the previous key immediately.
REMOTE_KEY_TTL_DAYS = 30
REMOTE_INSTANCE_ID_PREFIX = "remote:"


def remote_daemon_instance_id(remote_id: str | UUID) -> str:
    """The ``daemon_instances`` instance_id a remote registers under."""
    return f"{REMOTE_INSTANCE_ID_PREFIX}{remote_id}"


def remote_agent_external_id(remote_id: str | UUID) -> str:
    """The identity ``users.external_id`` for a remote's agent key owner."""
    return f"remote-agent:{remote_id}"


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class RemoteRepository:
    """CRUD for remote instances + their agent identity and keys."""

    def __init__(self, pool: Pool):
        self.pool = pool

    # ── CRUD ───────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        org_id: str,
        name: str,
        created_by: str,
        description: str | None = None,
        capabilities: dict | None = None,
        expires_at: Any = None,
    ) -> tuple[dict, str]:
        """Create a remote and provision its agent identity + token.

        Mirrors the per-org ``daemon-service:{org_id}`` chokepoint in
        ``OrganizationRepository.create``: the identity user, the API key,
        and the remote row land in one transaction.

        Returns:
            Tuple of (remote record, plaintext token). The plaintext token is
            returned exactly once; only a bcrypt hash is persisted.
        """
        remote_id = uuid.uuid4()
        identity_external_id = remote_agent_external_id(remote_id)
        raw_key = secrets.token_urlsafe(32)
        plain_key = f"lr_{raw_key}"
        key_prefix = plain_key[:11]  # "lr_" + first 8 chars
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

        expires = _parse_expires_at(expires_at)
        if expires is None:
            expires = datetime.now(timezone.utc) + timedelta(days=REMOTE_KEY_TTL_DAYS)

        async with self.pool.acquire() as conn:  # pre-auth create path (owner-scoped below)
            await conn.execute(preauth_guc_preamble())
            try:
                async with conn.transaction():
                    remote_row = await conn.fetchrow(
                        """
                        INSERT INTO remote_instances
                            (id, organization_id, name, description,
                             status, capabilities, created_by)
                        VALUES ($1, $2, $3, $4, 'active', $5, $6)
                        RETURNING *
                        """,
                        remote_id,
                        UUID(org_id),
                        name,
                        description,
                        json.dumps(capabilities or {}),
                        UUID(created_by),
                    )

                    # Identity user for the agent key (role daemon = service
                    # account; no interactive login). Same shape as the
                    # per-org daemon-service identity.
                    identity_row = await conn.fetchrow(
                        """
                        INSERT INTO users
                            (id, external_id, provider, organization_id,
                             email, display_name, role)
                        VALUES ($1, $2, 'local', $3, $4, $5, 'daemon')
                        RETURNING id
                        """,
                        uuid.uuid4(),
                        identity_external_id,
                        UUID(org_id),
                        f"remote-agent+{remote_id}@lucent.local",
                        f"Remote agent [{name}]",
                    )

                    await conn.execute(
                        """
                        INSERT INTO api_keys
                            (user_id, organization_id, name,
                             key_prefix, key_hash, scopes, expires_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        identity_row["id"],
                        UUID(org_id),
                        f"remote-agent-{remote_id}",
                        key_prefix,
                        key_hash,
                        REMOTE_KEY_SCOPES,
                        expires,
                    )
            finally:
                await conn.execute(tenant_guc_scrub())
        remote = dict(remote_row)
        logger.info(
            "Remote instance created: name=%s remote_id=%s org=%s key_prefix=%s",
            name,
            remote_id,
            org_id,
            key_prefix,
        )
        return remote, plain_key

    async def get(self, remote_id: str, org_id: str) -> dict | None:
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM remote_instances WHERE id = $1 AND organization_id = $2",
                UUID(remote_id),
                UUID(org_id),
            )
        return dict(row) if row else None

    async def list(self, org_id: str, *, include_revoked: bool = False) -> list[dict]:
        query = """SELECT * FROM remote_instances WHERE organization_id = $1"""
        if not include_revoked:
            query += " AND status <> 'revoked'"
        query += " ORDER BY created_at ASC"
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            rows = await conn.fetch(query, UUID(org_id))
        return [dict(r) for r in rows]

    async def update(
        self,
        remote_id: str,
        org_id: str,
        *,
        description: str | None = None,
        capabilities: dict | None = None,
        status: str | None = None,
    ) -> dict | None:
        """Update mutable fields. Status transitions to ``revoked`` also
        revoke the remote's agent key(s) immediately."""
        sets: list[str] = []
        params: list[Any] = []
        if description is not None:
            params.append(description)
            sets.append(f"description = ${len(params)}")
        if capabilities is not None:
            params.append(json.dumps(capabilities))
            sets.append(f"capabilities = ${len(params)}")
        if status is not None:
            if status not in ("active", "disabled", "revoked"):
                raise ValueError(f"Invalid remote status: {status}")
            params.append(status)
            sets.append(f"status = ${len(params)}")
        if not sets:
            existing = await self.get(remote_id, org_id)
            return existing

        params.extend([remote_id, org_id])
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""UPDATE remote_instances
                        SET {', '.join(sets)}, updated_at = NOW()
                        WHERE id = ${len(params) - 1}
                          AND organization_id = ${len(params)}
                        RETURNING *""",
                    *params,
                )
                if row and status == "revoked":
                    # Fail closed: disable every agent key owned by this
                    # remote's identity user.
                    await conn.execute(
                        """UPDATE api_keys
                           SET is_active = false, revoked_at = NOW()
                           WHERE user_id IN (
                               SELECT id FROM users
                               WHERE external_id = $1
                           )
                             AND organization_id = $2
                           """,
                        remote_agent_external_id(remote_id),
                        UUID(org_id),
                    )
        return dict(row) if row else None

    async def rotate_token(
        self, remote_id: str, org_id: str, *, expires_at: Any = None
    ) -> str | None:
        """Revoke the remote's current agent key and mint a replacement.

        Returns the new plaintext token (shown once), or None if the remote
        does not exist or is revoked.
        """
        remote = await self.get(remote_id, org_id)
        if not remote or remote["status"] == "revoked":
            return None

        raw_key = secrets.token_urlsafe(32)
        plain_key = f"lr_{raw_key}"
        key_prefix = plain_key[:11]
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()

        expires = _parse_expires_at(expires_at)
        if expires is None:
            expires = datetime.now(timezone.utc) + timedelta(days=REMOTE_KEY_TTL_DAYS)

        external_id = remote_agent_external_id(remote_id)
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            async with conn.transaction():
                identity_row = await conn.fetchrow(
                    "SELECT id FROM users WHERE external_id = $1 AND organization_id = $2",
                    external_id,
                    UUID(org_id),
                )
                if not identity_row:
                    return None
                await conn.execute(
                    """UPDATE api_keys
                       SET is_active = false, revoked_at = NOW()
                       WHERE user_id = $1
                         AND name = $2
                         AND revoked_at IS NULL
                       """,
                    identity_row["id"],
                    f"remote-agent-{remote_id}",
                )
                await conn.execute(
                    """INSERT INTO api_keys
                           (user_id, organization_id, name,
                            key_prefix, key_hash, scopes, expires_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       """,
                    identity_row["id"],
                    UUID(org_id),
                    f"remote-agent-{remote_id}",
                    key_prefix,
                    key_hash,
                    REMOTE_KEY_SCOPES,
                    expires,
                )
        logger.info(
            "Remote token rotated: remote_id=%s org=%s key_prefix=%s",
            remote_id,
            org_id,
            key_prefix,
        )
        return plain_key

    async def touch_last_seen(self, remote_id: str, org_id: str) -> None:
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            await conn.execute(
                """UPDATE remote_instances
                   SET last_seen_at = NOW(), updated_at = NOW()
                   WHERE id = $1 AND organization_id = $2
                     AND status <> 'revoked'
                   """,
                UUID(remote_id),
                UUID(org_id),
            )

    async def require_active(self, remote_id: str, org_id: str) -> dict | None:
        """Fetch a remote and enforce dispatch eligibility (must be active)."""
        remote = await self.get(remote_id, org_id)
        if not remote or remote["status"] != "active":
            return None
        return remote

    async def get_key_expiry_info(self, remote_id: str, org_id: str) -> dict | None:
        """Summarize the active agent token for UI attention signals."""
        external_id = remote_agent_external_id(remote_id)
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """SELECT ak.expires_at, ak.last_used_at, ak.use_count
                   FROM api_keys ak
                   JOIN users u ON ak.user_id = u.id
                   WHERE u.external_id = $1
                     AND ak.organization_id = $2
                     AND ak.revoked_at IS NULL
                     AND ak.is_active = true
                   ORDER BY ak.created_at DESC
                   LIMIT 1
                   """,
                external_id,
                UUID(org_id),
            )
        return dict(row) if row else None