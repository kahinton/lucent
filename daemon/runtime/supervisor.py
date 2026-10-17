"""Worker supervision (controller) and idle-exit (worker).

Two halves of the same feature:

* :class:`SupervisorMixin` runs in the always-on controller. It discovers which
  organizations need workers and asks the launcher to run them. Reconcile is
  level-based — it recomputes the desired worker count from the current queue
  state every pass — so a missed or duplicated notification costs nothing.
* :class:`WorkerLifecycleMixin` runs in an ephemeral worker. It watches for
  idleness and exits the process once it is above its organization's warm floor.

How many workers an org needs is ``max(work, warm floor)``. A floor of 0 scales
to zero; 1 keeps a worker always ready so work starts without a cold start.

The controller only ever scales *up*. Workers end themselves through idle-exit,
so a worker is never killed out from under an in-flight session, and the
``daemon.worker_idle_seconds`` window bounds churn without a separate cool-down.
The floor is the one thing that needs coordination, and it is reserved
atomically against the instance registry (:meth:`_release_warm_slot`).
"""

from __future__ import annotations

import asyncio
import math
import time

from lucent import settings as runtime_settings

# How many pending tasks one worker is asked to take on at a time.
WORKER_TASKS_PER_WORKER = 1
# Upper bound on concurrent workers for a single organization.
WORKER_MAX_PER_ORG = 2
# Upper bound on concurrent workers across all organizations.
WORKER_MAX_TOTAL = 10


def desired_worker_count(
    pending: int,
    running: int,
    *,
    tasks_per_worker: int = WORKER_TASKS_PER_WORKER,
    max_per_org: int = WORKER_MAX_PER_ORG,
    min_warm: int = 0,
) -> int:
    """How many workers an organization should have. Pure; safe to unit-test.

    At least the configured warm floor, so an org with no queue still has
    ``min_warm`` workers ready (0 means scale to zero). Above the floor, work
    drives the count, and a task in flight never lets it fall to zero — an org
    whose worker died leaves its task ``claimed``, and spawning a worker there is
    how the stale claim gets released and retried.

    ``max_per_org`` bounds work-driven scaling only. The floor is an explicit
    operator setting and is never silently capped by it.
    """
    floor = max(0, min_warm)
    work = pending + running
    if work <= 0:
        return floor
    per_worker = max(1, tasks_per_worker)
    desired = math.ceil(pending / per_worker) if pending > 0 else 0
    desired = max(desired, 1)
    return max(min(desired, max(1, max_per_org)), floor)


class SupervisorMixin:
    """Controller-side: decide how many workers each organization needs."""

    def _get_launcher(self):
        if self._launcher is None:
            from daemon.launcher import get_launcher

            self._launcher = get_launcher(controller_id=self.instance_id)
        return self._launcher

    async def _discover_orgs_with_work(self) -> list[dict]:
        """Return the orgs with pending or in-flight work, and their counts.

        Organization listing is a bootstrap read under the system scope, exactly
        as the daemon resolves its own binding. The per-org counts then run under
        an org-scoped connection, so every tenant-table read stays inside an org
        scope — the controller never gets a cross-org tenant grant. Only counts
        are read; never task content.
        """
        from daemon.db_scope import connect_scoped
        from daemon.runtime.module_proxy import runtime
        from lucent.db.daemon import DaemonRepository

        connection = await connect_scoped(runtime.DATABASE_URL, role="system")
        try:
            organizations = await DaemonRepository(connection).list_organizations_except(
                runtime.SYSTEM_ORG_NAME
            )
        finally:
            await connection.close()

        results: list[dict] = []
        for organization in organizations:
            org_id = str(organization["id"])
            org_conn = None
            try:
                org_conn = await connect_scoped(runtime.DATABASE_URL, organization_id=org_id)
                counts = await DaemonRepository(org_conn).count_work_for_org(org_id)
            except Exception as error:
                runtime.log(f"Work discovery failed for org {org_id[:8]}: {error}", "WARN")
                continue
            finally:
                if org_conn is not None:
                    await org_conn.close()
            # An org with a warm floor needs workers even with an empty queue, or
            # a worker that dies with nothing queued would never be replaced.
            floor = runtime_settings.daemon_worker_min_warm(organization_id=org_id)
            if counts["pending"] or counts["running"] or floor > 0:
                results.append(
                    {
                        "org_id": org_id,
                        "name": organization.get("name", ""),
                        "pending": counts["pending"],
                        "running": counts["running"],
                        "min_warm": floor,
                    }
                )
        return results

    async def _reconcile_workers_once(self) -> dict[str, int]:
        """One level-based supervision pass. Idempotent; tolerates failures."""
        from daemon.launcher import WorkerSpec
        from daemon.runtime.module_proxy import runtime

        summary = {"orgs": 0, "spawned": 0, "pruned": 0, "live": 0}
        launcher = self._get_launcher()

        try:
            organizations = await self._discover_orgs_with_work()
        except Exception as error:
            runtime.log(f"Worker discovery failed: {error}", "ERROR")
            return summary

        summary["orgs"] = len(organizations)
        live_total = 0
        for entry in organizations:
            org_id = entry["org_id"]
            try:
                live = await launcher.running(org_id)
            except Exception as error:
                runtime.log(f"Worker count failed for org {org_id[:8]}: {error}", "WARN")
                continue

            desired = desired_worker_count(
                int(entry["pending"]),
                int(entry["running"]),
                min_warm=int(entry.get("min_warm", 0)),
            )
            # Global ceiling: never exceed max total workers across all orgs.
            headroom = max(0, WORKER_MAX_TOTAL - live_total)
            target = min(desired, live + headroom)

            if target > live:
                try:
                    started = await launcher.ensure(org_id, target, WorkerSpec())
                    summary["spawned"] += started
                    live += started
                except Exception as error:
                    runtime.log(
                        f"Worker spawn failed for org {org_id[:8]}: {error}", "ERROR"
                    )
            live_total += live

        summary["live"] = live_total

        # Collect exited workers across every org, not just the discovered ones —
        # an org that just stopped needing workers is the one whose containers are
        # left behind, and it is by definition absent from the list above.
        try:
            summary["pruned"] = await launcher.prune()
        except Exception as error:
            runtime.log(f"Worker prune failed: {error}", "WARN")

        return summary

    async def _supervisor_loop(self):
        """Reconcile workers, waking early when a task notification arrives."""
        from daemon.runtime.module_proxy import runtime

        await self._setup_listen()
        runtime.log(
            f"Worker supervisor started (reconcile: {runtime.WORKER_RECONCILE_SECONDS}s)"
        )
        while self.running:
            try:
                if not self.draining:
                    await self._reconcile_workers_once()
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Supervisor loop error: {error}", "ERROR")
            try:
                await asyncio.wait_for(
                    self._task_ready.wait(), timeout=runtime.WORKER_RECONCILE_SECONDS
                )
            except asyncio.TimeoutError:
                pass
            self._task_ready.clear()


class WorkerLifecycleMixin:
    """Worker-side: exit the process once this worker has been idle long enough."""

    async def _worker_is_busy(self) -> bool:
        """Whether this worker still has work in flight.

        The in-process signals are free, so they are checked first. The database
        check catches the window between claiming a task and appending its
        session, which the in-process signals would miss.
        """
        if self.active_sessions or self._claim_operations:
            return True
        return (await self._has_owned_active_tasks()) > 0

    async def _should_idle_exit(self) -> bool:
        """True once this worker may leave: idle long enough and above the floor.

        The idle window measures continuous idleness. The warm floor is checked
        last, because releasing the slot is a commitment — the org is about to
        have one fewer worker — and it must not be spent unless the worker really
        is leaving.
        """
        from daemon.runtime.module_proxy import runtime

        now = time.monotonic()
        if await self._worker_is_busy():
            self._last_work_at = now
            return False
        if (now - self._last_work_at) < runtime.WORKER_IDLE_SECONDS:
            return False
        return await self._release_warm_slot()

    async def _release_warm_slot(self) -> bool:
        """Whether this worker may exit given its org's warm floor.

        The floor decision is reserved atomically against the instance registry
        so concurrent idle workers cannot all leave at once. Fails closed: if the
        floor cannot be checked, the worker stays alive.
        """
        from daemon.db_scope import connect_scoped
        from daemon.runtime.module_proxy import runtime
        from lucent.db.daemon import DaemonRepository

        org_id = await self._get_daemon_org_id()
        if not org_id:
            # No org to reason about. A worker always has one; if it does not,
            # staying alive is the safer failure.
            runtime.log("Idle-exit skipped: no bound organization", "WARN")
            return False

        floor = runtime_settings.daemon_worker_min_warm(organization_id=org_id)
        if floor <= 0:
            # No warm floor: any idle worker may go, no coordination needed.
            return True

        connection = None
        try:
            connection = await connect_scoped(runtime.DATABASE_URL, organization_id=org_id)
            return await DaemonRepository(connection).try_reserve_idle_exit(
                org_id, self.instance_id, floor
            )
        except Exception as error:
            runtime.log(f"Warm-floor check failed; staying alive: {error}", "WARN")
            return False
        finally:
            if connection is not None:
                await connection.close()

    async def _idle_exit_loop(self):
        """Exit this worker after it has been idle for ``worker_idle_seconds``."""
        from daemon.runtime.module_proxy import runtime

        poll = max(5, min(30, runtime.WORKER_IDLE_SECONDS // 4))
        while self.running:
            try:
                if await self._should_idle_exit():
                    runtime.log(
                        f"Worker idle for {runtime.WORKER_IDLE_SECONDS}s — exiting",
                        "INFO",
                    )
                    # Belt-and-suspenders: refuse a claim that races this exit.
                    self.draining = True
                    self.running = False
                    return
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Worker idle-exit check failed: {error}", "WARN")
            await asyncio.sleep(poll)
