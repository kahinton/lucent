"""Lifecycle operations for the daemon instance's own API key."""

from __future__ import annotations

from lucent.db.daemon import DaemonRepository


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
        # rls: bind the daemon org branch once the org identity is known
        await DaemonRepository(connection).set_daemon_scope(organization_id)
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
            # rls: daemon-owned flow — resolve + bind the daemon org branch;
            # on failure the PK-bound revoke stays fail-closed (safe no-op).
            bound = await runtime._resolve_daemon_org(connection)
            if bound:
                await DaemonRepository(connection).set_daemon_scope(bound[0])
            await DaemonRepository(connection).revoke_api_key(key_id)
            runtime.log(f"Revoked daemon API key on shutdown (id: {key_id[:8]}...)")
        finally:
            await connection.close()
    except Exception as error:
        runtime.log(f"Failed to revoke key on shutdown: {error}", "WARN")
    finally:
        runtime._current_key_db_id = None
        runtime._current_key_expires_at = None
