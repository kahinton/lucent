"""Request-scoped API-key and MCP configuration lifecycle."""

from __future__ import annotations


async def mint_scoped_api_key(
    *,
    memory_scope: str,
    memory_scope_user_id: str | None = None,
    org_id: str,
    ttl_minutes: int = 60,
) -> str | None:
    """Mint a temporary key constrained to one memory scope.

    The scoped key is the data-isolation boundary for task agents. It must
    never be replaced with the daemon-wide key after an authentication retry.
    """
    import secrets

    import asyncpg
    import bcrypt
    from daemon.runtime.module_proxy import runtime

    try:
        connection = await asyncpg.connect(runtime.DATABASE_URL)
    except Exception as error:
        runtime.log(f"DB connect failed minting scoped key: {error}", "WARN")
        return None

    try:
        user = await runtime._ensure_daemon_service_user(connection, org_id)
        if not user:
            runtime.log("Cannot mint scoped key — no daemon-service user", "WARN")
            return None

        daemon_user_id = str(user["id"])
        plain_key = f"hs_{secrets.token_urlsafe(32)}"
        key_prefix = plain_key[:11]
        key_hash = bcrypt.hashpw(plain_key.encode(), bcrypt.gensalt()).decode()
        scope_suffix = memory_scope_user_id[:8] if memory_scope_user_id else "shared"
        key_name = (
            f"scoped-{memory_scope}-{scope_suffix}-{secrets.token_hex(4)}"
        )

        await connection.fetchrow(
            "INSERT INTO api_keys "
            "(user_id, organization_id, name, key_prefix, key_hash, scopes, "
            " expires_at, memory_scope, memory_scope_user_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '1 minute' * $7, $8, $9) "
            "RETURNING id, expires_at",
            daemon_user_id,
            org_id,
            key_name,
            key_prefix,
            key_hash,
            ["read", "write"],
            ttl_minutes,
            memory_scope,
            memory_scope_user_id,
        )
        runtime.log(
            f"Minted {memory_scope} scoped key (prefix: {key_prefix}, "
            f"scope_user: {scope_suffix}, ttl: {ttl_minutes}m)"
        )
        return plain_key
    except Exception as error:
        runtime.log(f"Scoped key provisioning failed: {error}", "ERROR")
        return None
    finally:
        await connection.close()


def build_scoped_memory_server_config(
    *,
    scoped_key: str,
    memory_scope: str,
    org_id: str,
    memory_scope_user_id: str | None = None,
    tools: list[str],
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Build one task-scoped internal MCP server configuration."""
    from daemon.runtime.module_proxy import runtime

    return runtime.build_scoped_internal_mcp_server(
        url=runtime.MCP_URL,
        bearer_token=scoped_key,
        memory_scope=memory_scope,
        organization_id=org_id,
        memory_scope_user_id=memory_scope_user_id,
        tools=tools,
        extra_headers=extra_headers,
    )


async def refresh_scoped_memory_server_config(
    mcp_config: dict | None,
) -> dict | None:
    """Refresh a task key while preserving its original scope boundary."""
    from daemon.runtime.module_proxy import runtime

    if not isinstance(mcp_config, dict):
        return None
    memory_server = mcp_config.get("memory-server")
    if not isinstance(memory_server, dict):
        return None
    headers = dict(memory_server.get("headers") or {})
    memory_scope = headers.get(runtime.MEMORY_SCOPE_HEADER)
    org_id = headers.get(runtime.ORG_ID_HEADER)
    if memory_scope not in runtime.VALID_MEMORY_SCOPES or not org_id:
        return None
    memory_scope_user_id = headers.get(runtime.MEMORY_SCOPE_USER_ID_HEADER) or None
    if memory_scope == runtime.MEMORY_SCOPE_USER and not memory_scope_user_id:
        return None

    scoped_key = await runtime._mint_scoped_api_key(
        memory_scope=memory_scope,
        memory_scope_user_id=memory_scope_user_id,
        org_id=org_id,
        ttl_minutes=60,
    )
    if not scoped_key:
        return None

    headers["Authorization"] = f"Bearer {scoped_key}"
    refreshed_memory_server = dict(memory_server)
    refreshed_memory_server["headers"] = headers
    refreshed = dict(mcp_config)
    refreshed["memory-server"] = refreshed_memory_server
    return refreshed