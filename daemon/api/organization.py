"""Organization binding and daemon service identity resolution."""

from __future__ import annotations

from lucent.db.daemon import DaemonRepository


async def list_daemon_orgs(connection) -> list[tuple[str, str]]:
    """Every real organization this daemon may serve, as (id, name).

    The hidden system org is excluded: it is secrets-only and never a daemon
    target.
    """
    from daemon.runtime.module_proxy import runtime

    rows = await DaemonRepository(connection).list_organizations_except(
        runtime.SYSTEM_ORG_NAME
    )
    return [(str(row["id"]), row["name"]) for row in rows]


def is_worker() -> bool:
    """Whether this process is an ephemeral per-org worker."""
    from daemon.runtime.module_proxy import runtime

    return bool(runtime.DAEMON_WORKER)


async def resolve_daemon_org(connection) -> tuple[str, str] | None:
    """Resolve the organization this process operates on as a tenant.

    Two very different roles live behind one setting, so they resolve
    differently:

    * A **worker** is single-tenant by construction and must be bound to exactly
      one org — the launcher sets ``LUCENT_DAEMON_ORG`` explicitly.
    * The always-on **controller** is NOT single-tenant. It supervises every
      org that has work and spawns a correctly-bound worker per org, so pinning
      it to one org would leave every other org's queue undispatched forever.
      For the controller this returns the explicitly pinned org when the
      operator set one, and otherwise ``None`` — meaning "not a single org",
      which the callers that genuinely need a tenant treat as their normal
      multi-org path rather than an error.

    Cached per process: an org binding never changes while a process runs.
    """
    from daemon.runtime.module_proxy import runtime

    if runtime._resolved_daemon_org is not None:
        return runtime._resolved_daemon_org
    if runtime.DAEMON_ORG:
        row = await DaemonRepository(connection).resolve_organization(runtime.DAEMON_ORG)
        if not row:
            runtime.log(
                f"LUCENT_DAEMON_ORG={runtime.DAEMON_ORG!r} matches no organization; "
                "cannot bind this process to an org.",
                "ERROR",
            )
            return None
        runtime._resolved_daemon_org = (str(row["id"]), row["name"])
        runtime.log(
            f"Daemon bound to organization '{row['name']}' "
            "(explicit LUCENT_DAEMON_ORG)"
        )
        return runtime._resolved_daemon_org

    rows = await list_daemon_orgs(connection)
    if not rows:
        return None
    if is_worker():
        # A worker is spawned per org and always carries the pin. Reaching here
        # means a worker was started incorrectly; binding it to "the only org"
        # would make it silently dispatch for the wrong tenant if a second one
        # ever appeared.
        runtime.log(
            "A worker must be started with an explicit LUCENT_DAEMON_ORG; "
            f"found {len(rows)} organization(s) and no binding.",
            "ERROR",
        )
        return None
    if len(rows) > 1:
        # Controller across multiple orgs: expected, not an error. It has no
        # single tenant, and its multi-org paths (supervision, due schedules)
        # read across orgs deliberately.
        runtime.log(
            f"Controller serving {len(rows)} organizations: "
            + ", ".join(name for _org_id, name in rows[:6])
        )
        return None
    runtime._resolved_daemon_org = rows[0]
    runtime.log(
        f"Daemon auto-bound to the single organization '{rows[0][1]}'"
    )
    return runtime._resolved_daemon_org


async def resolve_service_org(connection) -> tuple[str, str] | None:
    """The org whose service identity this daemon process acts under.

    Distinct from :func:`resolve_daemon_org`, which answers "which tenant am I
    scoped to?" — the wrong question for a multi-org controller, which has no
    single tenant but still needs a service identity so it can authenticate and
    heartbeat at all.

    Resolution order: the explicit operator pin; otherwise the only real org;
    otherwise the first by creation. The last step is deterministic so restarts
    reuse the same service identity instead of thrashing between orgs.

    This is an identity choice, not an authority grant: it does not limit which
    orgs the controller supervises. It DOES currently limit which orgs it can
    schedule for, because a daemon API key resolves to exactly one org
    (``api_keys.organization_id``) and ``/schedules/due`` is scoped to it.
    """
    from daemon.runtime.module_proxy import runtime

    if runtime.DAEMON_ORG:
        return await resolve_daemon_org(connection)
    cached = getattr(runtime, "_resolved_service_org", None)
    if cached is not None:
        return cached
    rows = await list_daemon_orgs(connection)
    if not rows:
        return None
    runtime._resolved_service_org = rows[0]
    if len(rows) > 1:
        runtime.log(
            "No single-tenant binding; acting under "
            f"'{rows[0][1]}' as the daemon service identity "
            f"(first of {len(rows)} orgs). Work supervision covers all orgs."
        )
    return runtime._resolved_service_org


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
