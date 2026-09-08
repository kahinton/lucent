"""Serialized FIFO persistence bridge for LLM session events.

Engine event callbacks (CopilotEngine and LangChainEngine both funnel through
chat.py's shared ``on_event``) must never block on a database write, but
persisting them out-of-order would corrupt the replay sequence: the DB layer
assigns each ``llm_session_events`` row its ``sequence`` at insert time
(MAX+1 under the session-row lock), so the persistence order *is* the replay
order. A single FIFO worker converts "non-blocking callbacks" into "exact
emission-order persistence" — the stable ordering key for reload.

Append-only is enforced downstream in ``LLMSessionRepository.add_event``
(ON CONFLICT DO NOTHING): rows are never rewritten, only appended.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[[Any, Any], Awaitable[None]]


class EventPersistBridge:
    """Queue engine events and persist them strictly in emission order.

    A single worker drains the FIFO queue; each item is handled in order, and
    a failed persist is logged loudly (never silently dropped) without killing
    the worker — later events must still get their rows. Every consumed item
    (including the ``None`` stop sentinel) calls ``task_done()`` so the
    finalize-path ``join()`` drain can never hang.
    """

    def __init__(self, handler: Handler):
        self._handler = handler
        self._queue: asyncio.Queue[tuple[Any, Any] | None] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    def enqueue(self, payload: Any, event: Any) -> None:
        """Queue an event for ordered persistence (synchronous, non-blocking)."""
        self._queue.put_nowait((payload, event))

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                payload, event = item
                try:
                    await self._handler(payload, event)
                except Exception:
                    logger.exception(
                        "Failed to persist chat session event (type=%s tool=%s)",
                        payload.get("type") if isinstance(payload, dict) else None,
                        payload.get("tool") if isinstance(payload, dict) else None,
                    )
            finally:
                self._queue.task_done()

    async def drain_and_stop(self) -> None:
        """Wait for every queued item, then stop the worker.

        ``join()`` waits for the in-flight item even when the queue is empty,
        so a stream can finalize while an event write is mid-flight without
        losing it. Items that race the stop sentinel (an abort-path SDK
        callback firing while the drain is already running) are swept with a
        bounded re-drain instead of being silently stranded — callers that can
        still receive events should stop intake first so the sweep stays a
        defensive no-op.
        """
        while True:
            await self._queue.join()
            if self._queue.empty():
                break
        self._queue.put_nowait(None)
        if self._worker_task is not None:
            try:
                await self._worker_task
            except Exception:
                logger.exception("Event persist worker crashed before drain")
        if not self._queue.empty():
            logger.warning(
                "Event persist bridge: %d late event(s) raced the stop sentinel; sweeping",
                self._queue.qsize(),
            )
            self.start()
            await self._queue.join()
            self._queue.put_nowait(None)
            if self._worker_task is not None:
                try:
                    await self._worker_task
                except Exception:
                    logger.exception("Event persist worker crashed during sweep")


__all__ = ["EventPersistBridge"]