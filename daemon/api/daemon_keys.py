"""Lifecycle operations for the daemon instance's own API keys.

One key per organization. This is the isolation boundary: a daemon credential
is scoped to a single org (``api_keys.organization_id``), and every tenant call
carries the credential of the org it is acting for. A multi-org controller
therefore holds a *set* of org-scoped keys and never one key that spans tenants.
"""

from __future__ import annotations

import secrets

import asyncpg
import bcrypt

from daemon.runtime.module_proxy import runtime
from lucent.db.daemon import DaemonRepository

#: (org_id -> key_id). Process-local lifecycle state, one entry per org.
_key_ids: dict[str, str] = {}
#: (org_id -> expiry). One entry per org.
_key_expiries: dict[str, object] = {}


def recorded_key_id(organization_id: str | None) -> str | None:
    return _key_ids.get(organization_id or "")


def recorded_key_expiry(organization_id: str | None) -> object | None:
    return _key_expiries.get(organization_id or "")


def _forget(organization_id: str) -> None:
    _key_ids.pop(organization_id, None)
    _key_expiries.pop(organization_id, None)


async def _service_user(connection, organization_id: str) -> dict | None:
    """The daemon-service user for one org, self-healing if missing."""
    await DaemonRepository(connection).set_daemon_scope(organization_id)
    return await runtime._ensure_daemon_service_user(connection, organization_id)


async def provision_daemon_api_key(
    instance_id: str, *, organization_id: str
) -> str | None:
    """Provision this instance's API key for exactly one organization.

    Each org gets its own daemon-service user and its own key, so a leaked or
    misused credential can only ever reach the org it was minted for.
    """
    try:
        connection = await asyncpg.connect(runtime.DATABASE_URL)
    except Exception as error:
        runtime.log(f"DB connect failed during key provisioning: {error}", "WARN")
        return None

    try:
        user = await _service_user(connection, organization_id)
        if not user:
            runtime.log(
                f"Cannot provision daemon API key: no daemon-service user for "
                f"org {organization_id[:8]}",
                "WARN",
            )
            return None

        user_id = str(user["id"])
        organization_id = str(user["organization_id"] or organization_id)
        # Per-org key name: two orgs of one instance must not collide, and the
        # name is what rotation/revocation keys off of.
        key_name = f"daemon-{instance_id}-{organization_id[:8]}"
        repository = DaemonRepository(connection)
        await repository.revoke_active_daemon_key(user_id, key_name)
        await repository.prune_revoked_daemon_keys(user_id)

        plain_key = f"hs_{secrets.token_urlsafe(32)}"
        key_prefix = plain_key[:11]
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()
        row = await repository.create_daemon_key(
            user_id,
            organization_id,
            key_name,
            key_prefix,
            key_hash,
            scopes=runtime.DAEMON_KEY_SCOPES,
            ttl_hours=runtime.KEY_TTL_HOURS,
        )
        _key_ids[organization_id] = str(row["id"])
        _key_expiries[organization_id] = row["expires_at"]
        runtime.log(
            f"Provisioned daemon API key for org {organization_id[:8]} "
            f"(prefix: {key_prefix}, expires in {runtime.KEY_TTL_HOURS}h)"
        )
        return plain_key
    except Exception as error:
        runtime.log(f"Key provisioning failed: {error}", "ERROR")
        return None
    finally:
        await connection.close()


async def revoke_key(organization_id: str) -> None:
    """Revoke one org's daemon key and forget its lifecycle state."""
    key_id = _key_ids.get(organization_id)
    if not key_id:
        return
    try:
        connection = await asyncpg.connect(runtime.DATABASE_URL)
        try:
            await DaemonRepository(connection).set_daemon_scope(organization_id)
            await DaemonRepository(connection).revoke_api_key(key_id)
            runtime.log(
                f"Revoked daemon API key for org {organization_id[:8]} "
                f"(id: {key_id[:8]}...)"
            )
        finally:
            await connection.close()
    except Exception as error:
        runtime.log(f"Failed to revoke key on shutdown: {error}", "WARN")
    finally:
        _forget(organization_id)


async def revoke_all_keys() -> None:
    """Revoke every org key this process provisioned (shutdown)."""
    for organization_id in list(_key_ids):
        await revoke_key(organization_id)
