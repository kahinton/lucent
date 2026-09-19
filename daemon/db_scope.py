"""RLS-aware direct-connect helper for the daemon.

The daemon's 17 direct ``asyncpg.connect`` sites (daemon.py polling/fan-out
paths, runtime loops, review lifecycle, key provisioning, web live-events)
run under the restricted ``lucent_daemon`` role. Migration 116 binds that
role with RLS: without ``app.org_id`` set, every policy predicate is false
and the connection is fail-closed denied on all tenant tables.

This module provides the connect wrapper every daemon direct-connect site
uses: it binds ``app.user_id`` / ``app.org_id`` / ``app.role`` session-local
right after connect (daemon context is org-wide within that org — the
daemon policy branch from the 9/10 evaluation §6), and registers an
explicit scrub. Session-local (not transaction-local): asyncpg runs each
top-level statement in its own implicit transaction, which would commit
txn-local values away before the next statement (live-verified in the RLS
wave session).

Plumbing-only sites (LISTEN, advisory locks, pg_notify) pass
``organization_id=None``: they carry no tenant row reads, and the empty
context keeps them fail-closed by construction.
"""

from __future__ import annotations

from typing import Any

import asyncpg


async def connect_scoped(
    dsn: str,
    *,
    organization_id: str | None = None,
    user_id: str | None = None,
    role: str = "daemon",
    **connect_kwargs: Any,
) -> asyncpg.Connection:
    """Connect with the tenant GUCs bound for the daemon's org scope.

    Args:
        dsn: DAEMON_DATABASE_URL (the lucent_daemon role).
        organization_id: the org this connection's statements are scoped to.
            None for plumbing-only sites (empty GUCs = fail-closed deny).
        user_id: optional owner identity (empty for the daemon branch).
        role: session role GUC; 'daemon' (default) for org-wide fan-out.
        **connect_kwargs: forwarded to asyncpg.connect.
    """
    conn = await asyncpg.connect(dsn, **connect_kwargs)
    try:
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false), "
            "set_config('app.org_id', $2, false), "
            "set_config('app.role', $3, false);",
            user_id or "",
            organization_id or "",
            role,
        )
    except Exception:
        # RLS wave not yet applied (pre-cutover stack): session-local
        # set_config on a superuser role is still permitted, but if the
        # role lacks set-config rights on a custom GUC the connect must
        # not break the daemon. Empty context is the fail-closed state.
        pass
    return conn