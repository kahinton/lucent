"""Lifecycle operations for the daemon instance's own API key."""

from __future__ import annotations


async def provision_daemon_api_key(instance_id: str) -> str | None:
    """Provision and record one daemon-instance API key with a bounded TTL."""
    import secrets

    import asyncpg
    import bcrypt
    from daemon.runtime.module_proxy import runtime

    try:
        connection = await asyncpg.connect(runtime.DATABASE_URL)
    except Exception as error:
        runtime.log(f"DB connect failed during key provisioning: {error}", "WARN")
        return None

    try:
        bound_organization = await runtime._resolve_daemon_org(connection)
        if not bound_organization:
            runtime.log(
                "Cannot provision daemon API key: no organization to bind to. "
                "Create an organization (sign in) or set LUCENT_DAEMON_ORG.",
                "WARN",
            )
            return None
        organization_id, _organization_name = bound_organization
        user = await runtime._ensure_daemon_service_user(connection, organization_id)
        if not user:
            return None

        user_id = str(user["id"])
        organization_id = (
            str(user["organization_id"])
            if user["organization_id"]
            else organization_id
        )
        key_name = f"daemon-{instance_id}"
        await connection.execute(
            "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
            "WHERE user_id = $1 AND name = $2 AND revoked_at IS NULL",
            user_id,
            key_name,
        )
        await connection.execute(
            "DELETE FROM api_keys WHERE user_id = $1 "
            "AND name LIKE 'daemon-%' AND revoked_at IS NOT NULL "
            "AND id NOT IN ("
            "  SELECT id FROM api_keys WHERE user_id = $1 "
            "  AND name LIKE 'daemon-%' AND revoked_at IS NOT NULL "
            "  ORDER BY revoked_at DESC LIMIT 5"
            ")",
            user_id,
        )

        plain_key = f"hs_{secrets.token_urlsafe(32)}"
        key_prefix = plain_key[:11]
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()
        row = await connection.fetchrow(
            "INSERT INTO api_keys "
            "(user_id, organization_id, name, key_prefix, key_hash, scopes, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '1 hour' * $7) "
            "RETURNING id, expires_at",
            user_id,
            organization_id,
            key_name,
            key_prefix,
            key_hash,
            runtime.DAEMON_KEY_SCOPES,
            runtime.KEY_TTL_HOURS,
        )
        runtime._current_key_db_id = str(row["id"])
        runtime._current_key_expires_at = row["expires_at"]
        runtime.log(
            f"Provisioned daemon API key (prefix: {key_prefix}, "
            f"expires in {runtime.KEY_TTL_HOURS}h)"
        )
        return plain_key
    except Exception as error:
        runtime.log(f"Key provisioning failed: {error}", "ERROR")
        return None
    finally:
        await connection.close()


async def revoke_current_key() -> None:
    """Revoke the recorded daemon key and clear its local lifecycle state."""
    import asyncpg
    from daemon.runtime.module_proxy import runtime

    key_id = runtime._current_key_db_id
    if not key_id:
        return
    try:
        connection = await asyncpg.connect(runtime.DATABASE_URL)
        try:
            await connection.execute(
                "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
                "WHERE id = $1 AND revoked_at IS NULL",
                key_id,
            )
            runtime.log(f"Revoked daemon API key on shutdown (id: {key_id[:8]}...)")
        finally:
            await connection.close()
    except Exception as error:
        runtime.log(f"Failed to revoke key on shutdown: {error}", "WARN")
    finally:
        runtime._current_key_db_id = None
        runtime._current_key_expires_at = None