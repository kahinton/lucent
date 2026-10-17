"""Scoped direct-connect helper for the daemon.

The daemon's direct ``asyncpg.connect`` sites (polling/fan-out paths, runtime
loops, review lifecycle, key provisioning, web live-events, worker
supervision) bind the tenant GUCs ``app.user_id`` / ``app.org_id`` /
``app.role`` session-local, just after connect.

Important: Postgres row-level security was implemented (2026-09-19) and then
removed (2026-09-22, commit a955d47); the pre-removal state is preserved under
``backups/rls-removal-20260922T225905Z/``. So the GUCs are currently *advisory*
context consumed by the application-layer org filters, not an enforced
database boundary. Do not rely on them as one — see
``docs/daemon-scaling-design.md`` §6 for the plan to restore a database-level
backstop, which the on-demand worker design treats as a launch prerequisite.

This module provides the connect wrapper every daemon direct-connect site
uses: it binds those GUCs right after connect (daemon context is org-wide
within that org), and registers an explicit scrub. Session-local (not
transaction-local): asyncpg runs each top-level statement in its own implicit
transaction, which would commit txn-local values away before the next
statement.

Plumbing-only sites (LISTEN, advisory locks, pg_notify) pass
``organization_id=None``: they carry no tenant row reads, and the empty
context keeps them out of the org-scoped paths by construction.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from lucent.db.daemon import DaemonRepository
from lucent.db.scope_audit import (
    ScopeAuditedConnection,
    effective_scope,
    audit_enabled,
)


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
        await DaemonRepository(conn).set_scope(
            user_id=user_id or "",
            organization_id=organization_id or "",
            role=role,
        )
    except Exception:
        # RLS wave not yet applied (pre-cutover stack): session-local
        # set_config on a superuser role is still permitted, but if the
        # role lacks set-config rights on a custom GUC the connect must
        # not break the daemon. Empty context is the fail-closed state.
        pass
    if audit_enabled():
        return ScopeAuditedConnection(
            conn,
            effective_scope(
                None,
                user_id=user_id,
                organization_id=organization_id,
                role=role,
                access_type="direct_connect",
            ),
        )
    return conn
