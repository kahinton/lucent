"""Organization binding and daemon service identity resolution."""

from __future__ import annotations


async def resolve_daemon_org(connection) -> tuple[str, str] | None:
    """Resolve and cache the single organization served by this daemon."""
    from daemon.runtime.module_proxy import runtime

    if runtime._resolved_daemon_org is not None:
        return runtime._resolved_daemon_org
    if runtime.DAEMON_ORG:
        row = await connection.fetchrow(
            "SELECT id, name FROM organizations "
            "WHERE id::text = $1 OR name = $1 LIMIT 1",
            runtime.DAEMON_ORG,
        )
        if not row:
            runtime.log(
                f"LUCENT_DAEMON_ORG={runtime.DAEMON_ORG!r} matches no organization; "
                "cannot bind daemon to an org.",
                "ERROR",
            )
            return None
        runtime._resolved_daemon_org = (str(row["id"]), row["name"])
        runtime.log(
            f"Daemon bound to organization '{row['name']}' "
            "(explicit LUCENT_DAEMON_ORG)"
        )
        return runtime._resolved_daemon_org

    rows = await connection.fetch(
        "SELECT id, name FROM organizations WHERE name <> $1 ORDER BY created_at",
        runtime.SYSTEM_ORG_NAME,
    )
    if not rows:
        return None
    if len(rows) > 1:
        names = ", ".join(row["name"] for row in rows[:6])
        runtime.log(
            f"Found {len(rows)} organizations but no LUCENT_DAEMON_ORG binding; "
            f"a daemon serves a single org. Set LUCENT_DAEMON_ORG to one of: {names}",
            "ERROR",
        )
        return None
    runtime._resolved_daemon_org = (str(rows[0]["id"]), rows[0]["name"])
    runtime.log(
        f"Daemon auto-bound to the single organization '{rows[0]['name']}'"
    )
    return runtime._resolved_daemon_org


async def ensure_daemon_service_user(connection, org_id: str) -> dict | None:
    """Get or self-heal the organization-scoped daemon service user."""
    from daemon.runtime.module_proxy import runtime
    from lucent.daemon_identity import ensure_daemon_service_user as ensure

    try:
        return await ensure(connection, org_id)
    except Exception as error:
        runtime.log(
            f"Could not provision daemon-service user for org {org_id}: {error}",
            "ERROR",
        )
        return None
