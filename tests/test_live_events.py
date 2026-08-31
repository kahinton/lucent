"""Unit tests for PostgreSQL-backed web live-event fanout."""

import asyncio
import json

import pytest

from lucent.web.live_events import LIVE_EVENT_CHANNEL, LiveEventBroker


@pytest.mark.asyncio
async def test_user_scoped_event_only_signals_the_matching_user():
    broker = LiveEventBroker()
    async with broker.subscribe("org-a", "user-a") as matching, broker.subscribe(
        "org-a", "user-b"
    ) as other_user, broker.subscribe("org-b", "user-a") as other_org:
        broker._on_notification(
            None,
            0,
            LIVE_EVENT_CHANNEL,
            json.dumps({"organization_id": "org-a", "user_id": "user-a"}),
        )

        await asyncio.wait_for(matching.get(), timeout=0.1)
        assert other_user.empty()
        assert other_org.empty()


@pytest.mark.asyncio
async def test_organization_wide_event_signals_only_that_organization():
    broker = LiveEventBroker()
    async with broker.subscribe("org-a", "user-a") as first_user, broker.subscribe(
        "org-a", "user-b"
    ) as second_user, broker.subscribe("org-b", "user-a") as other_org:
        broker._on_notification(
            None,
            0,
            LIVE_EVENT_CHANNEL,
            json.dumps({"organization_id": "org-a", "user_id": None}),
        )

        await asyncio.wait_for(first_user.get(), timeout=0.1)
        await asyncio.wait_for(second_user.get(), timeout=0.1)
        assert other_org.empty()