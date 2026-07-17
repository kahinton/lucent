"""Event loops, LISTEN integration, and graceful source reload support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path


class RuntimeLoopsMixin:
    """Coordinates event-driven dispatch, scheduling, and process reloads."""

    async def _setup_listen(self):
        """Establish the persistent PostgreSQL LISTEN connection."""
        import asyncpg

        from daemon.runtime.module_proxy import runtime

        async with self._listen_lock:
            if self._listen_conn and not self._listen_conn.is_closed():
                try:
                    await self._listen_conn.fetchval("SELECT 1")
                    return True
                except Exception:
                    runtime.log("PG LISTEN connection stale, will reconnect", "WARN")
                    try:
                        await self._listen_conn.close()
                    except Exception:
                        runtime.log("Failed to close stale PG LISTEN connection", "DEBUG")
                    self._listen_conn = None
            try:
                self._listen_conn = await asyncpg.connect(runtime.DATABASE_URL)
                await self._listen_conn.add_listener("task_ready", self._on_task_ready)
                await self._listen_conn.add_listener("request_ready", self._on_request_ready)
                runtime.log("PG LISTEN established on 'task_ready' and 'request_ready' channels")
                return True
            except Exception as error:
                runtime.log(
                    f"PG LISTEN setup failed (dispatch will use polling only): {error}",
                    "WARN",
                )
                self._listen_conn = None
                return False

    def _on_task_ready(self, conn, pid, channel, payload):
        self._task_ready.set()

    def _on_request_ready(self, conn, pid, channel, payload):
        self._request_ready.set()
        try:
            decoded = json.loads(payload)
            request_id = (
                decoded.get("request_id") if isinstance(decoded, dict) else decoded
            )
        except (json.JSONDecodeError, TypeError):
            request_id = payload
        if request_id:
            self._decomposition_request_ids.add(str(request_id))
        self._decomposition_ready.set()

    def _take_decomposition_request_ids(self) -> set[str]:
        """Atomically drain request IDs queued by synchronous PG callbacks."""
        request_ids = set(self._decomposition_request_ids)
        self._decomposition_request_ids.clear()
        return request_ids

    async def _run_decomposition_pass(self, org_id: str) -> int:
        """Immediately decompose notified requests, or run the aged fallback."""
        request_ids = self._take_decomposition_request_ids()
        if not request_ids:
            return await self._backfill_pending_decomposition(
                org_id=org_id, min_age_seconds=300
            )

        attempted = 0
        for request_id in sorted(request_ids):
            attempted += await self._backfill_pending_decomposition(
                org_id=org_id,
                min_age_seconds=0,
                request_id=request_id,
            )
        return attempted

    async def _dispatch_loop(self):
        """Claim and execute queued tasks, using polling as a NOTIFY fallback."""
        from daemon.runtime.module_proxy import runtime

        runtime.log(f"Dispatch loop started (poll fallback: {runtime.DISPATCH_POLL_SECONDS}s)")
        await self._setup_listen()
        while self.running:
            try:
                try:
                    await asyncio.wait_for(
                        self._task_ready.wait(), timeout=runtime.DISPATCH_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
                self._task_ready.clear()
                if not await runtime._verify_and_provision_key(self.instance_id):
                    runtime.log("Dispatch: no valid API key, retrying in 30s", "WARN")
                    await asyncio.sleep(30)
                    continue
                await self._update_heartbeat()
                stale = await runtime.RequestAPI.release_stale(
                    runtime.STALE_HEARTBEAT_MINUTES
                )
                if stale:
                    runtime.log(f"Released {stale} stale tracked tasks")
                if self.draining:
                    await asyncio.sleep(5)
                    continue
                await self._reload_runtime_settings()
                await self._dispatch_tracked_tasks()
            except asyncio.CancelledError:
                break
            except Exception as error:
                import traceback

                runtime.log(
                    f"Dispatch loop error: {error}\n{traceback.format_exc()}", "ERROR"
                )
                await asyncio.sleep(5)
                if self._listen_conn is None or self._listen_conn.is_closed():
                    await self._setup_listen()

    async def _cognitive_loop(self):
        """Run long-horizon planning periodically or when requests arrive."""
        from daemon.runtime.module_proxy import runtime

        runtime.log(
            f"Cognitive loop started (interval: {runtime.DAEMON_INTERVAL_MINUTES}m)"
        )
        if not self._listen_conn or self._listen_conn.is_closed():
            await self._setup_listen()
        while self.running:
            try:
                if self.draining:
                    await asyncio.sleep(5)
                    continue
                await self.run_cognitive_cycle()
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Cognitive loop error: {error}", "ERROR")
            try:
                await asyncio.wait_for(
                    self._request_ready.wait(),
                    timeout=runtime.DAEMON_INTERVAL_MINUTES * 60,
                )
                runtime.log("Cognitive loop woke early: new user/API request detected")
            except asyncio.TimeoutError:
                pass
            self._request_ready.clear()

    async def _scheduler_loop(self):
        """Fire due system and user-defined schedules."""
        from daemon.runtime.module_proxy import runtime

        runtime.log(
            f"Scheduler loop started (interval: {runtime.SCHEDULER_CHECK_SECONDS}s)"
        )
        while self.running:
            try:
                if not await runtime._verify_and_provision_key(self.instance_id):
                    await asyncio.sleep(30)
                    continue
                if not getattr(self, "_schedules_seeded", False):
                    self._schedules_seeded = await self._seed_system_schedules()
                await self._check_due_schedules()
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Scheduler loop error: {error}", "ERROR")
            await asyncio.sleep(runtime.SCHEDULER_CHECK_SECONDS)

    async def _decomposition_loop(self):
        """Decompose new requests on NOTIFY, with an aged polling fallback."""
        from daemon.runtime.module_proxy import runtime

        runtime.log(
            "Request decomposition loop started "
            f"(poll fallback: {runtime.SCHEDULER_CHECK_SECONDS}s)"
        )
        await self._setup_listen()
        while self.running:
            try:
                if not await runtime._verify_and_provision_key(self.instance_id):
                    await asyncio.sleep(30)
                    continue
                org_id = await self._get_daemon_org_id()
                if org_id:
                    await self._run_decomposition_pass(org_id)
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Request decomposition loop error: {error}", "WARN")

            try:
                await asyncio.wait_for(
                    self._decomposition_ready.wait(),
                    timeout=runtime.SCHEDULER_CHECK_SECONDS,
                )
                runtime.log("Request decomposition woke immediately: request_ready")
            except asyncio.TimeoutError:
                pass
            self._decomposition_ready.clear()

    async def _get_daemon_org_id(self) -> str | None:
        """Return the daemon's bound organization id."""
        import asyncpg

        from daemon.runtime.module_proxy import runtime

        if cached := getattr(self, "_cached_daemon_org_id", None):
            return cached
        try:
            connection = await asyncpg.connect(runtime.DATABASE_URL)
        except Exception:
            return None
        try:
            bound = await runtime._resolve_daemon_org(connection)
            if not bound:
                return None
            self._cached_daemon_org_id = bound[0]
            return bound[0]
        finally:
            await connection.close()

    async def _autonomic_loop(self):
        """Legacy periodic maintenance loop retained for compatibility."""
        from daemon.runtime.module_proxy import runtime

        runtime.log(
            f"Autonomic loop started (learning: {runtime.LEARNING_MINUTES}m, "
            f"compression: {runtime.COMPRESSION_MINUTES}m)"
        )
        learning_file = Path("/tmp/lucent_last_learning")
        compression_file = Path("/tmp/lucent_last_compression")

        def minutes_since(path: Path) -> float:
            try:
                return (time.time() - float(path.read_text().strip())) / 60
            except (FileNotFoundError, ValueError):
                return float("inf")

        await asyncio.sleep(60)
        while self.running:
            try:
                if minutes_since(learning_file) >= runtime.LEARNING_MINUTES:
                    if await runtime._verify_and_provision_key(self.instance_id):
                        learning_file.write_text(str(time.time()))
                        await self.run_learning_extraction()
                if minutes_since(compression_file) >= runtime.COMPRESSION_MINUTES:
                    if await runtime._verify_and_provision_key(self.instance_id):
                        compression_file.write_text(str(time.time()))
                        await self.run_experience_compression()
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Autonomic loop error: {error}", "ERROR")
            await asyncio.sleep(60)

    async def _reload_watcher(self):
        """Drain and restart after watched source files change."""
        from daemon.runtime.module_proxy import runtime

        pending_reload = False
        while self.running:
            try:
                if self._should_reload():
                    pending_reload = True
                    self._source_mtimes = self._snapshot_source_files()
                if pending_reload:
                    if self.active_sessions:
                        runtime.log(
                            f"Reload deferred — {len(self.active_sessions)} session(s) active. "
                            "Will restart when sessions complete.",
                            "DEBUG",
                        )
                    else:
                        runtime.log("No active sessions — proceeding with deferred reload")
                        await self._graceful_restart()
                        return
            except asyncio.CancelledError:
                break
            except Exception as error:
                runtime.log(f"Reload watcher error: {error}", "WARN")
            await asyncio.sleep(30)

    async def _graceful_restart(self):
        """Drain in-flight sessions and replace the process."""
        from daemon.runtime.module_proxy import runtime

        active_count = len(self.active_sessions)
        runtime.log(
            "Source files changed — entering drain mode "
            f"({active_count} active session{'s' if active_count != 1 else ''})"
        )
        self.draining = True
        if self._tracer:
            self._drain_active.add(1)
        span = (
            self._tracer.start_as_current_span(
                "daemon.drain",
                attributes={"daemon.drain.active_sessions": active_count},
            )
            if self._tracer
            else contextlib.nullcontext()
        )
        with span:
            if active_count:
                deadline = time.time() + self.DRAIN_TIMEOUT
                while self.active_sessions and time.time() < deadline:
                    runtime.log(
                        f"Drain: waiting for {len(self.active_sessions)} session(s) "
                        f"({int(deadline - time.time())}s remaining)"
                    )
                    await asyncio.sleep(5)
                if self.active_sessions:
                    runtime.log(
                        f"Drain timeout — {len(self.active_sessions)} session(s) still active, "
                        "proceeding with restart",
                        "WARN",
                    )
                else:
                    runtime.log("Drain complete — all sessions finished cleanly")
            self.running = False
            self._restart_self()

    def _snapshot_source_files(self) -> dict[str, float]:
        """Capture mtimes for daemon Python and prompt files."""
        from daemon.runtime.module_proxy import runtime

        watched_paths = [
            runtime.DAEMON_DIR,
            runtime.COGNITIVE_PROMPT_PATH,
            runtime.AGENT_DEF_PATH,
        ]
        files: dict[str, float] = {}
        for path in watched_paths:
            if path.is_dir():
                for child in path.rglob("*.py"):
                    files[str(child)] = child.stat().st_mtime
            elif path.exists():
                files[str(path)] = path.stat().st_mtime
        return files

    def _should_reload(self) -> bool:
        """Return whether a watched source file changed or appeared."""
        from daemon.runtime.module_proxy import runtime

        current = self._snapshot_source_files()
        for path, modified_at in current.items():
            previous = self._source_mtimes.get(path)
            if previous is None or modified_at > previous:
                runtime.log(
                    f"File changed: {Path(path).name} (mtime {previous} -> {modified_at})"
                )
                return True
        return False

    def _restart_self(self):
        """Replace the process using its existing interpreter and arguments."""
        from daemon.runtime.module_proxy import runtime

        runtime.log("Executing self-restart...")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
