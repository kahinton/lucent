"""Stream a running task's in-flight model output to the activity page.

A task's final text only lands in ``tasks.result`` when the session settles, so
the request detail page had nothing to show while work was actually happening
(just a "Running" badge). This module closes that gap: the daemon forwards
assistant text deltas to ``task_live_output`` as they arrive, and the page polls
a small read-only endpoint for them.

Design constraints that shaped this module:

* **Never slow the model down.** Writes happen on a background flush task, not
  on the delta callback. The callback only appends to an in-memory buffer.
* **Never let bookkeeping break a task.** Every database operation is wrapped;
  failures are logged at debug level and swallowed. A live-output outage must
  not fail, stall, or requeue real work.
* **Bounded memory and rows.** Buffers and stored text are capped (``MAX_CONTENT``)
  keeping the tail, because a reader wants to see what the model is writing
  *now*, not the opening paragraph of a very long session.
* **Scratch, not history.** One row per task, deleted when the task settles.
  ``task_events`` stays the durable timeline; flooding it with stream ticks
  would both bloat the table and make the historical record unreadable.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from daemon.db_scope import connect_scoped

# How much in-flight text is kept per task. The tail is what matters to a
# viewer watching progress, and an unbounded column would let one runaway
# session grow a row without limit.
MAX_CONTENT = 20_000

# Minimum seconds between database writes for one task. Deltas arrive many
# times a second; writing each one would be pure write amplification.
FLUSH_INTERVAL_SECONDS = 1.5

# Text shorter than this is not worth a round trip yet, unless the task has
# been silent long enough that the buffer is all we have.
_MIN_FLUSH_CHARS = 40

# Consecutive write failures before this writer stops trying. A dropped
# connection mid-run is normal; a missing grant or table is not, and either way
# the model session must not be affected.
_MAX_CONSECUTIVE_FAILURES = 5


class TaskLiveOutputWriter:
    """Owns the scratch row for one running task.

    Lifecycle: construct when a session starts, ``flush()`` opportunistically,
    then ``close()`` after the task settles (which deletes the row).

    A writer with no ``task_id`` is inert — every method is a no-op. That keeps
    call sites unconditional: planning fan-outs, fusion candidates, and
    continuation sessions all run through the same session runner but should
    not each own the task's visible output.
    """

    def __init__(
        self,
        *,
        task_id: str | None,
        organization_id: str | None,
        database_url: str | None,
        log: Any = None,
    ) -> None:
        self.task_id = str(task_id) if task_id else None
        self.organization_id = str(organization_id) if organization_id else None
        self.database_url = database_url
        self._log = log
        self._buffer: list[str] = []
        self._buffered_chars = 0
        self._last_flush = time.monotonic()
        self._closed = False
        self._failed = False
        self._consecutive_failures = 0
        # Guards against spawning a write task per delta: deltas arrive in
        # bursts, and `should_flush` would stay true for every one of them
        # until the first write finished.
        self._flush_scheduled = False

    # ── Delta intake ─────────────────────────────────────────────────────

    def append(self, chunk: str | None) -> None:
        """Buffer one streamed chunk. Never raises, never awaits, never blocks."""
        if not chunk or self._closed or not self._enabled:
            return
        self._buffer.append(chunk)
        self._buffered_chars += len(chunk)
        # Trim the buffer itself so a long silent stretch cannot accumulate
        # megabytes before the next flush.
        if self._buffered_chars > MAX_CONTENT * 2:
            joined = "".join(self._buffer)[-MAX_CONTENT:]
            self._buffer = [joined]
            self._buffered_chars = len(joined)

    def should_flush(self) -> bool:
        """Whether enough text and time have accumulated to justify a write.

        Both conditions matter. Deltas arrive dozens of times a second, so a
        text-only trigger would write constantly; a time-only trigger would
        write empty rows for a model that is thinking. A large backlog forces
        a write regardless of the clock so a reader is never far behind.
        """
        if not self._enabled or self._closed or self._failed or not self._buffer:
            return False
        if self._flush_scheduled:
            return False
        if self._buffered_chars >= _MIN_FLUSH_CHARS * 10:
            return True
        if self._buffered_chars < _MIN_FLUSH_CHARS:
            return False
        return (time.monotonic() - self._last_flush) >= FLUSH_INTERVAL_SECONDS

    async def flush(self, *, force: bool = False) -> None:
        """Append the buffered text to the task's live row.

        Appends rather than replaces: deltas arrive in many small flushes, so
        overwriting would leave only the last chunk visible. The SQL keeps the
        tail at ``MAX_CONTENT`` so the row cannot grow without bound.
        """
        if not self._enabled or self._closed or self._failed:
            return
        if not self._buffer and not force:
            return
        self._flush_scheduled = True
        text = "".join(self._buffer)
        self._buffer = []
        self._buffered_chars = 0
        self._last_flush = time.monotonic()
        if not text:
            self._flush_scheduled = False
            return
        try:
            await self._write(
                """INSERT INTO task_live_output (task_id, organization_id, content, updated_at)
                   VALUES ($1::uuid, $2::uuid, right($3, $4), NOW())
                   ON CONFLICT (task_id) DO UPDATE
                       SET content = right(task_live_output.content || EXCLUDED.content, $4),
                           updated_at = NOW()""",
                self.task_id,
                self.organization_id,
                text,
                MAX_CONTENT,
            )
        finally:
            self._flush_scheduled = False

    async def close(self) -> None:
        """Delete the scratch row. The durable result now lives on the task."""
        if not self._enabled or self._closed:
            return
        self._closed = True
        self._buffer = []
        self._buffered_chars = 0
        await self._write(
            "DELETE FROM task_live_output WHERE task_id = $1::uuid",
            self.task_id,
        )

    # ── Internals ───────────────────────────────────────────────────────

    @property
    def _enabled(self) -> bool:
        return bool(self.task_id and self.organization_id and self.database_url)

    async def _execute(self, sql: str, *args: Any) -> None:
        """Run one statement on a short-lived, org-scoped connection."""
        connection = await connect_scoped(
            self.database_url,
            organization_id=self.organization_id,
        )
        try:
            await connection.execute(sql, *args)
        finally:
            with contextlib.suppress(Exception):
                await connection.close()

    async def _write(self, sql: str, *args: Any) -> None:
        """Execute a statement, absorbing failures.

        Live output is a progressive enhancement layered on top of a real model
        session. It must never raise into the caller, and repeated failures (a
        missing grant, a dropped table) must stop the writer rather than log on
        every flush — while a single transient error must not cost the viewer
        the rest of the run.
        """
        try:
            await self._execute(sql, *args)
            self._consecutive_failures = 0
        except Exception as error:  # noqa: BLE001 — bookkeeping must never break work
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._failed = True
                if self._log:
                    self._log(
                        "Live output disabled for task "
                        f"{str(self.task_id)[:8]} after "
                        f"{self._consecutive_failures} failed writes: {error}",
                        "WARN",
                    )
            elif self._log:
                self._log(
                    f"Live output write failed for task {str(self.task_id)[:8]}: {error}",
                    "DEBUG",
                )


def build_live_output_writer(
    audit_context: dict[str, Any] | None,
    *,
    database_url: str | None,
    log: Any = None,
) -> TaskLiveOutputWriter:
    """Create the writer for a session from its audit context.

    ``source`` distinguishes the task's *primary* session from the auxiliary
    ones (fusion candidates, completion continuations, fan-out passes). Only
    the primary session's text is what a viewer should see attributed to the
    task; the auxiliaries would otherwise overwrite each other's words and
    produce a flickering stream.
    """
    context = audit_context or {}
    source = str(context.get("source") or "")
    primary_sources = {"daemon.task"}
    return TaskLiveOutputWriter(
        task_id=context.get("task_id") if source in primary_sources else None,
        organization_id=context.get("organization_id"),
        database_url=database_url,
        log=log,
    )
