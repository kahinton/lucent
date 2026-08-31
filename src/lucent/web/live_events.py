"""PostgreSQL-backed live refresh signals for authenticated web sessions."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from typing import Any

import asyncpg

from lucent.logging import get_logger

logger = get_logger(__name__)

LIVE_EVENT_CHANNEL = "lucent_live_update"


class LiveEventBroker:
    """Fan PostgreSQL refresh signals out to matching browser connections.

    Queues hold one signal because clients re-fetch current state; keeping every
    intermediate mutation would only make a busy task tree lag behind reality.
    """

    def __init__(self) -> None:
        self._connection: asyncpg.Connection | None = None
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[None]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def start(self, database_url: str | None = None) -> None:
        """Start the dedicated LISTEN connection, without blocking web startup."""
        if self._connection and not self._connection.is_closed():
            return
        database_url = database_url or os.environ.get("DATABASE_URL")
        if not database_url:
            return
        try:
            connection = await asyncpg.connect(database_url)
            await connection.add_listener(LIVE_EVENT_CHANNEL, self._on_notification)
            self._connection = connection
            logger.info("Live event listener connected on %s", LIVE_EVENT_CHANNEL)
        except Exception:
            logger.warning("Live event listener unavailable; polling remains active", exc_info=True)

    async def stop(self) -> None:
        """Close the listener and release all waiting streams during shutdown."""
        connection, self._connection = self._connection, None
        if connection and not connection.is_closed():
            try:
                await connection.remove_listener(LIVE_EVENT_CHANNEL, self._on_notification)
                await connection.close()
            except Exception:
                logger.debug("Failed to close live event listener", exc_info=True)
        async with self._lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for queues in subscribers:
            for queue in queues:
                self._signal(queue)

    @asynccontextmanager
    async def subscribe(self, organization_id: str, user_id: str) -> AsyncIterator[asyncio.Queue[None]]:
        """Register one browser connection for scoped refresh notifications."""
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        key = (organization_id, user_id)
        async with self._lock:
            self._subscribers[key].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                queues = self._subscribers.get(key)
                if queues:
                    queues.discard(queue)
                    if not queues:
                        self._subscribers.pop(key, None)

    def _on_notification(
        self,
        _connection: asyncpg.Connection,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        try:
            event: dict[str, Any] = json.loads(payload)
            organization_id = str(event["organization_id"])
            user_id = event.get("user_id")
            user_id = str(user_id) if user_id else None
        except (TypeError, ValueError, KeyError):
            logger.warning("Ignoring malformed live event payload")
            return

        for (subscriber_org_id, subscriber_user_id), queues in tuple(self._subscribers.items()):
            if subscriber_org_id != organization_id:
                continue
            if user_id is not None and subscriber_user_id != user_id:
                continue
            for queue in tuple(queues):
                self._signal(queue)

    @staticmethod
    def _signal(queue: asyncio.Queue[None]) -> None:
        if not queue.full():
            queue.put_nowait(None)


live_event_broker = LiveEventBroker()