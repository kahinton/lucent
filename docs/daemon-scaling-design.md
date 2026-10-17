# On-Demand Daemon Workers — Design

**Status:** Phase 1 implemented (Docker/dev only); Kubernetes, packaging, and the DB backstop pending ·
**Date:** 2026-10-13 · **Scope:** daemon execution topology for launch / cloud

> Phase 1 implementation notes are in [§11](#11-phase-1-implementation-notes) at the end.

## 1. Summary

Today the daemon is a long-lived process that binds to **exactly one organization** and
listens forever for work. Serving *N* orgs means running *N* always-on daemons, each idle
most of the time, and production does not currently ship the daemon at all.

This proposes replacing that with a small always-on **controller** that watches the work
queue and **spawns an ephemeral worker container per organization that has work to do**.
A worker drains its org's queue, then exits. We keep the invariant that matters for
security — **one worker serves exactly one org** — and change only *when* workers exist,
not *what they are allowed to touch*.

The goal is scale-to-zero execution that is safer and more reliable than the status quo,
not merely cheaper.

## 2. What we have today

### 2.1 The daemon

| Property | Today | Evidence |
|---|---|---|
| Entrypoint | `python -m daemon.daemon` → `LucentDaemon.run_forever()` | `daemon/daemon.py:5587`, `:5494` |
| Process model | One OS process, one asyncio loop, N role loops as tasks, +1 watchdog thread | `daemon/daemon.py:5517-5540`, `:1544` |
| Org binding | **Exactly one org** — `LUCENT_DAEMON_ORG`, else auto-bind iff exactly one real org, else refuse | `daemon/api/organization.py:7-46` |
| Work intake | `tasks`/`requests` tables via PG `LISTEN` on `task_ready` / `request_ready` (org id in payload), 60 s polling fallback | `daemon/runtime/loops.py:51-96`, `migrations/018_task_dispatch_triggers.sql` |
| Claim | Atomic conditional `UPDATE … WHERE status IN ('pending','planned')`, with lease + heartbeat | `src/lucent/db/requests.py:3092-3167` |
| Stale recovery | `release_stale()` on startup and each dispatch iteration | `daemon/daemon.py:2049`, `loops.py:140-144` |
| Instance registry | `daemon_instances` (org, roles, status, heartbeat) | `migrations/054_multi_instance_task_distribution.sql` |
| Concurrency | `daemon.max_sessions` (default 3) across all loops; dispatcher is serial | `daemon/daemon.py:204`, `settings.py:750-760` |
| Identity | Self-provisions a 24 h daemon API key bound to the org's "daemon-service" user | `daemon/api/daemon_keys.py:8-72` |
| Task identity | Per-task **scoped** key (memory scope + user) minted as the data-isolation boundary | `daemon/api/scoped_keys.py:5-67` |
| Roles | `dispatcher` / `scheduler` already split | `daemon/daemon.py:1800`, `:5517-5522` |
| Health | **None.** No HTTP endpoint; only outbound heartbeats to the server | `daemon/runtime/scheduling.py:14-40` |
| Shutdown | `SIGTERM` **cancels all tasks immediately — no drain**; `stop()` destroys live sessions | `daemon/daemon.py:3325-3364` |

### 2.2 Production packaging (the gap)

The daemon was **not deployable in production at all**:

- ~~`pyproject.toml` packages only `src/lucent`; `daemon/` is a top-level package excluded
  from the wheel, so the production image cannot run the daemon.~~ **Fixed** — the wheel now
  includes `daemon`, both Dockerfiles copy it, and a `lucent-daemon` console script exists.
  Verified by building the production image and running the daemon inside it.
- The Helm chart's only workload is the server `Deployment`; the `daemon.*` values are written
  into a ConfigMap consumed by a process that contains no daemon code
  (`deploy/helm/lucent/templates/configmap.yaml:9-16`, `values.yaml:88`). **Still open.**
- `docker-compose.prod.yml` has no daemon service. Only dev Compose runs the daemon
  (a single `daemon-1` service, now that the second-worker profile is gone).
  **Still open.**
- The operator writes Helm values to a ConfigMap and runs Jobs, but never applies Helm and
  never creates a daemon workload (`deploy/operator/src/operator.py:353-433`). **Still open.**
- `docs/kubernetes-deployment.md:937` documents a `daemon` container that does not exist.
  **Still open.**

With the image able to run a daemon, what remains is the *scheduling* story: nothing in
production decides to start one.

### 2.3 Tenancy enforcement (critical caveat)

Isolation is **application-layer**, not database-layer:

- Every query carries `organization_id`; `scoped_acquire()` is fail-closed without scope
  (`src/lucent/db/pool.py:566-627`).
- A default-deny `auth_clearances` ACL (role `read`/`write`/`owner`) is enforced by rewriting
  SQL through `AuthorizedDatabasePool`, which is read-only and rejects unsafe queries
  (`src/lucent/db/pool.py:899-1061`).
- **Postgres RLS was implemented and then removed** (2026-09-22, commit `a955d47`). The
  pre-removal state — policies on every tenant table and roles `lucent_daemon`/`lucent_app`
  created `NOBYPASSRLS` — is preserved in `backups/rls-removal-20260922T225905Z/`.
  `daemon/db_scope.py:1-21` still describes RLS as active; that comment is **stale**.

Consequence: a worker that runs with the org-bound `lucent_daemon` DSN is one application bug
away from cross-tenant reads. There is no database backstop today. This design treats
restoring a database-level backstop as a prerequisite (§6), not a nice-to-have.

### 2.4 Ephemeral execution already exists — for sandboxes

The K8s sandbox backend is the mold to copy: one Pod per unit of work, `restartPolicy: Never`,
`activeDeadlineSeconds`, seccomp `RuntimeDefault`, dropped capabilities, no service-account
token, org as a pod label, and a per-sandbox `NetworkPolicy` for egress
(`src/lucent/sandbox/k8s_backend.py:105-200`, `:292-337`). A worker pod should be built the
same way.

## 3. Design principles

These are hard constraints; a design that violates one is rejected regardless of benefit.

1. **One worker = one org.** Never a multi-tenant worker. The single-tenant binding is a
   security invariant, not a limitation to be optimized away.
2. **Org binding comes from the controller, never from task data.** A task or request field
   must never be able to influence which org a worker runs as.
3. **A worker holds org-scoped credentials only.** It never receives a broader DSN or a
   cross-org key than it needs; per-task work still uses per-task scoped keys.
4. **Fail closed.** No "best-effort" fallback that widens scope on error.
5. **Work is never lost.** A killed worker's claimed work is reclaimed; completion is
   idempotent.
6. **Scale-down drains.** SIGTERM must finish or cleanly release in-flight work rather than
   cancel it.
7. **Every spawn is attributable.** Org, reason, requester, and outcome are auditable.
8. **Cloud-agnostic.** The spawn mechanism sits behind an interface, like `SandboxBackend`.

## 4. Target architecture

Three roles:

```
                    ┌──────────────────────────────┐
   users / agents → │  lucent-server   (always on)  │  API · MCP · web · enqueue + NOTIFY
                    └──────────────┬───────────────┘
                                   │ LISTEN task_ready / request_ready
                    ┌──────────────▼───────────────┐
                    │  lucent-controller (always on)│  scheduler · decomposition · reaper
                    │  roles: scheduler (system)    │  worker supervisor (reconcile loop)
                    └──────────────┬───────────────┘
                          spawn Job with LUCENT_DAEMON_ORG=<org>
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
     ┌────────────────┐   ┌────────────────┐   ┌────────────────┐
     │ worker (org A) │   │ worker (org B) │   │ worker (org C) │   dispatcher role,
     │ ephemeral      │   │ ephemeral      │   │ ephemeral      │   one org, drains,
     └────────────────┘   └────────────────┘   └────────────────┘   then exits
```

- **server** — unchanged; owns enqueue and the NOTIFY emits it already performs.
- **controller** — new. The only always-on worker-side process. Runs the `scheduler` role
  (cron/interval/once), request decomposition, and the stale-task reaper, and supervises
  workers. It does **not** run LLM task sessions.
- **worker** — new, ephemeral. `roles=dispatcher`, pinned to one org. Claims and runs tasks
  (sandbox + LLM session + validation), then exits when idle.

This is not a new taxonomy: the daemon already separates `scheduler` from `dispatcher`
(`daemon/daemon.py:1800`). We are promoting that split to the deployment topology and making
the dispatcher side elastic.

### 4.1 Wake-up and reconcile

- The controller LISTENs on the existing `task_ready` / `request_ready` channels and reads
  `organization_id` from the payload (`migrations/018_task_dispatch_triggers.sql`).
- On a notify for org *O*, it reconciles *O* (debounced).
- A periodic reconcile (default 15 s) runs regardless, because `LISTEN/NOTIFY` is lossy and
  because dead workers must be detected without waiting for the next enqueue.
- Reconcile is **level-based, not edge-based**: it computes desired worker count from current
  queue state each time, so missed and duplicate events are harmless.

### 4.2 Worker lifecycle

```
spawned ──▶ registering ──▶ draining-queue ──▶ idle-exit
   (Job)      (self-provision    (claim + run)     (no work for idle_timeout)
              org-bound key)
                  │                  │
                  └──── SIGTERM ─────┴──▶ draining ──▶ exit
                        (scale-down / eviction: finish or release in-flight, then exit)
```

- Spawned as a Kubernetes **Job** (not a bare Pod) with `ttlSecondsAfterFinished`,
  `backoffLimit`, and `activeDeadlineSeconds` sized above `SESSION_TOTAL_TIMEOUT` so a long
  session is not killed mid-flight by the platform.
- **Idle exit** is what makes scale-to-zero real: after `idle_timeout` with no claimable work
  and no running session, the worker revokes its key, marks its instance stopped, and exits 0.
- **Drain on SIGTERM** is a prerequisite change (§7, Phase 0). Today `_handle_shutdown`
  cancels everything (`daemon/daemon.py:3325-3331`); for ephemeral workers we need the
  source-reload drain behavior (`daemon/runtime/loops.py:355-393`) applied to signals:
  stop claiming, wait for `active_sessions` to finish up to a deadline, and release any task
  that cannot finish so another worker can pick it up.

### 4.3 Spawn mechanism — `WorkerLauncher`

A thin interface, mirroring `SandboxBackend`:

```python
class WorkerLauncher(Protocol):
    async def ensure(self, org_id: str, count: int, spec: WorkerSpec) -> None: ...
    async def running(self, org_id: str) -> int: ...
    async def stop(self, worker_id: str, *, graceful: bool = True) -> None: ...
```

Backends:

- **KubernetesLauncher** — creates Jobs from a worker Job template; reads live worker count
  from the `daemon_instances` registry (already written by every daemon) with staleness.
- **DockerLauncher** — `docker run` of the worker image, for local dev parity with the
  Compose stack.
- Future: Nomad / ECS / etc. — no controller changes.

### 4.4 Scale policy

For each org with work:

```
pending = # eligible tasks (status in pending/planned)
running = # tasks claimed/running for the org
work    = pending + running
desired = 0 if work == 0 else clamp(ceil(pending / tasks_per_worker), 1, max_per_org)
```

- `tasks_per_worker`, `max_per_org`, and `idle_timeout` are **org-scoped runtime settings**
  (the settings layer already supports per-org values — `src/lucent/settings.py`
  `_setting_*(key, organization_id=…)`), so a noisy org can be capped without a global change.
- Hysteresis: an org that just scaled to zero is not respawned for a short cool-down, and a
  worker that is `running` is never scaled away, to avoid flapping.
- A global ceiling bounds total concurrent workers (noisy-neighbour protection).

### 4.5 The org-binding handoff (security-critical)

The controller is the only component that decides which org a worker runs as. The handoff:

1. The controller computes the org from **its own queue observation** (`tasks.organization_id`),
   never from a request/task body supplied by a user or agent.
2. It creates the worker Job with the binding as immutable environment supplied by the
   controller — `LUCENT_DAEMON_ORG=<org_id>` and a bootstrap credential from a Kubernetes
   Secret mounted into the controller (not derived from task content).
3. The worker self-provisions its org-bound daemon key exactly as today
   (`daemon/api/daemon_keys.py:8-72`), or receives a short-TTL org-bound token (§8, open).
4. Defense in depth: the worker asserts, on every claim, that the claimed task's
   `organization_id` equals its bound org and fails closed on mismatch. This should be an
   explicit check rather than relying on the server-side query filter alone.

## 5. Security analysis

Moving to spawned containers changes the **trust topology**: a new privileged component (the
controller) can cause execution in *any* org, so it becomes the highest-value target in the
system.

| Risk | Today | With on-demand workers | Mitigation |
|---|---|---|---|
| Cross-tenant read by a mis-bound worker | Single org, but no DB backstop since RLS removal | Same, plus a new spawner that could mis-bind | §4.5 binding from controller only; restore DB backstop (§6) |
| Compromised spawner spawns a worker for another org | N/A (no spawner) | Cross-tenant execution | Minimal RBAC (create Jobs only), no task-content code path, network-isolated, audited |
| Worker gets a broad credential | Daemon key scopes are already broad (`["read","write"]`) | A leaked worker credential is now more numerous | Keep per-task scoped keys; narrow daemon key scopes (open item M3) |
| Task content influences org | Impossible (org is process-global) | Must remain impossible | Explicitly forbid org fields from task payloads in the launcher path |
| Secret leakage across orgs | User-clearance-scoped, fail-closed | Unchanged, but more processes hold the provider config | Per-org Vault path; never ship `LUCENT_SECRET_KEY`/`VAULT_TOKEN` into user-visible containers |
| Worker pod escapes to control plane | N/A | Worker could abuse pod/Job privileges | No SA token, seccomp `RuntimeDefault`, drop caps, per-worker `NetworkPolicy` (§2.4) |
| Loss of audit attribution | Instance registry records org | Must record *why* each worker exists | Add spawn provenance to `daemon_instances.metadata` / a `worker_spawns` audit row |

**The controller must never be reachable from a task sandbox**, and must not hold a general
database credential beyond what supervision needs. Its privilege is "create Jobs in the worker
namespace + read the queue" — nothing that lets it read tenant data directly.

## 6. Prerequisite: restore a database-level tenancy backstop

With RLS removed, org isolation is a single application-layer filter deep. An on-demand
spawner multiplies the ways a bug could produce a mis-bound process, so this design **requires**
reinstating a database backstop before on-demand workers carry multi-tenant production
traffic. Options, in preference order:

1. **Re-apply RLS** on tenant tables for the restricted `lucent_app` / `lucent_daemon` roles
   (`NOBYPASSRLS`), keyed on the `app.org_id` GUC that `connect_scoped()` already sets
   (`daemon/db_scope.py:37-79`). The pre-removal artifacts in
   `backups/rls-removal-20260922T225905Z/` are a working starting point. This makes an unbound
   or mis-bound worker **fail closed at the database**, which is exactly the property an
   elastic spawner needs.
2. Per-org database roles (heavier operationally; not recommended at current scale).
3. Accept application-layer-only isolation for single-tenant/self-host deployments, where the
   blast radius is one org by construction.

Note that `daemon/db_scope.py`'s docstring currently claims RLS is active — it should be
corrected either way.

## 7. Reliability analysis

| Failure | Behavior | Needed work |
|---|---|---|
| Worker crashes mid-task | Claim lease expires; `release_stale` re-queues | Move the reaper into the controller so it runs with zero workers alive (a server-side fallback already exists as a `server_function` workflow) |
| Controller crashes | No new spawns until restart; in-flight workers finish | Leader election (K8s Lease) or single replica + fast restart; reconcile is idempotent |
| Scale-down during a session | **Today: session killed** | Implement drain-on-SIGTERM (§4.2, Phase 0) |
| `LISTEN/NOTIFY` dropped | Work sits until next poll | Periodic reconcile backstop (§4.1) |
| Spawn storm / noisy neighbour | New failure mode | Global ceiling + per-org caps + cool-down |
| Flapping at the boundary | New failure mode | Hysteresis + idle dwell + never scale away a running worker |
| Cold start too slow | New failure mode | Measure; consider a warm pool for hot orgs; pre-pulled images |
| Task outlives pod lifetime | Job killed | `activeDeadlineSeconds > SESSION_TOTAL_TIMEOUT`; heartbeat renews lease |
| Duplicate spawn | Two workers, one org | Harmless — claim is atomic; reconcile dedupes via the instance registry |

The existing atomic claim + lease + stale-reaper machinery is what makes elasticity safe;
this design leans on it heavily and must not weaken it.

## 8. Open decisions

1. **Binding handoff:** worker self-provisions its org key (least new machinery) vs. the
   controller mints a short-TTL org-bound token (tighter, more code). Recommend starting with
   self-provision + the org assertion in §4.5 step 4.
2. **Granularity:** one worker per org (proposed) vs. one worker per *task*. Per-task is
   simpler to reason about but pays cold start per task; per-org amortizes it. Recommend
   per-org with idle-exit.
3. **Warm pool:** settled by `daemon.worker_min_warm` — 0 for scale-to-zero, 1+ for a
   standby pool. Per-org, so a hot org can stay warm while the rest scale down.
4. **Personal/self-host:** `min_warm=1` gives the old always-on availability. Self-host
   without container spawning can still hand-run an in-process dispatcher with
   `LUCENT_DAEMON_ROLES=dispatcher`.
5. **DB backstop (§6)** is a launch gate; confirm scope and timing.

## 9. Phased rollout

- **Phase 0 — safety net (no topology change).**
  Drain-on-SIGTERM in the daemon; a daemon health/readiness signal; package the daemon into a
  real worker image (fix `pyproject.toml` / add a worker Dockerfile) so a worker *can* exist.
  These are independently valuable and unblock everything else.
- **Phase 1 — controller + DockerLauncher, dormant.**
  Build the controller and the launcher interface; run it alongside the existing always-on
  daemon with spawning disabled. Prove reconcile logic and metrics without changing execution.
- **Phase 2 — tune the warm floor.**
  `daemon.worker_min_warm` per org: 0 to scale to zero, 1+ to keep a standby pool. Roll out
  a hot org at 1 while the rest stay at 0.
- **Phase 3 — Kubernetes launcher + Helm.**
  Worker Job template, controller Deployment with leader election, RBAC, NetworkPolicies; fix
  the Helm/operator/docs drift so production actually runs daemons.
- **Phase 4 — hardening.**
  Restore the DB backstop (§6); narrow daemon key scopes (M3); warm pool; per-org quotas and
  cost telemetry.

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Controller becomes a cross-tenant pivot | High | Minimal RBAC, no tenant-data path, audit every spawn, network isolation |
| Cold start + per-task overhead raises latency | Medium | Per-org amortization, idle-exit tuning, warm pool option |
| Elasticity amplifies a binding bug | High | §4.5 controller-only binding + assertion + DB backstop (§6) |
| New operational surface (controller, Job template, RBAC) | Medium | Phased rollout, feature flag, always-on fallback |
| Cost of idle controllers / warm pools | Low | Scale controller separately; warm pool opt-in |

## 11. Phase 1 implementation notes

Phase 1 is the Docker/dev slice: a mode switch that works both ways, with the
spawn mechanism behind an interface. What landed, and where it diverges from
the design above:

- **Mode switch.** ~~`daemon.mode` (`LUCENT_DAEMON_MODE`)~~ — **superseded by the warm
  floor** (§4.4). There is one model: the always-on process is the controller and the
  scheduler, workers dispatch. `daemon.worker_min_warm` (per-org, default 0) is how many
  workers an org keeps alive with nothing queued: 0 scales to zero, 1 keeps one always
  ready. The old `on_demand` is `min_warm=0` and `always_on`'s benefit is `min_warm=1`, so
  the flag carried no extra meaning.
- **`"all"` is the always-on set** (`scheduler` + `controller`); `dispatcher` is
  explicit-only and runs in the spawned workers. A hand-run in-process dispatcher is still
  available with `LUCENT_DAEMON_ROLES=dispatcher` and does not idle-exit.
- **The controller is supervisor-only in this phase** — see the boundary note below. It
  supervises; it does not dispatch.
- **Discovery is per-org, not a cross-org query.** The controller lists orgs
  under the system scope (the existing bootstrap read) and then counts work for
  each org under an org-scoped connection (`DaemonRepository.count_work_for_org`,
  counts only). This keeps every tenant read inside an org scope and stays
  correct once RLS is restored; the controller is never granted a blanket
  tenant-table read. It does learn per-org backlog size — a bounded, deliberate
  widening, and the minimum needed to decide where to spawn.
- **Discovery includes in-flight work**, not just pending. An org whose only
  task is `claimed` by a dead worker must still get a worker, whose dispatch
  loop releases the stale claim.
- **`DockerLauncher` self-locates.** It finds the controller's own container and
  clones its image, networks, and mounts, then builds the worker environment
  from an allowlist. `ensure()` strips binding keys out of any caller-supplied
  env and applies the authoritative org binding last, so nothing a task or
  request carries can influence which org a worker serves.
- **Idle-exit applies only to spawned workers** (the launcher sets
  `LUCENT_DAEMON_WORKER`), so a hand-run in-process dispatcher never idle-exits.
  The controller only scales *up*; workers end themselves, so a worker is never
  killed out from under an in-flight session. The idle window subsumes the
  cool-down from §4.4, so no separate cooldown was added.
- **The warm floor is reserved atomically** (§4.4): a worker asks the
  `daemon_instances` registry whether it may leave, under a per-org advisory
  lock, so concurrent idle workers cannot all exit at once. With no floor (0) the
  check is skipped entirely.
- **Drain-on-SIGTERM applies to every daemon** (a deliberate decision, not
  mode-gated), replacing the previous immediate `task.cancel()`. The wait was
  extracted from `_graceful_restart` into `_drain_active_sessions()` and shared.
- **Claim-time org assertion.** After a successful claim, a daemon asserts the
  task's `organization_id` matches its bound org and releases the task on
  mismatch — defense in depth behind the server-side org-scoped claim.

**Phase 1 boundaries (explicitly unchanged):**

- The scheduler is not yet multi-org. It runs in the controller process and
  resolves one org via auto-bind, so it works in a single-org database and
  degrades to a logged no-op on a multi-org one. Making it iterate orgs is a
  follow-on.
- No Kubernetes launcher, no Helm, no RLS backstop. The packaging gap from §2.2
  **has been fixed** (the daemon now ships in the wheel and both Dockerfiles copy
  it, with a `lucent-daemon` console script), so an image can run a daemon without
  the dev bind mount. What remains is the K8s launcher and the DB backstop.
