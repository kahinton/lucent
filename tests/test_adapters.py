"""Tests for lucent.integrations.adapters — AdapterResponse, DiscordAdapter, AdapterRegistry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lucent.integrations.adapters import (
    AdapterRegistry,
    AdapterResponse,
    DiscordAdapter,
)
from lucent.integrations.base import IntegrationAdapter, IntegrationError
from lucent.integrations.models import EventType, IntegrationEvent
from lucent.integrations.slack_adapter import SlackAdapter


# ---------------------------------------------------------------------------
# AdapterResponse
# ---------------------------------------------------------------------------


class TestAdapterResponse:
    def test_minimal(self):
        resp = AdapterResponse(text="hello")
        assert resp.text == "hello"
        assert resp.thread_id is None
        assert resp.ephemeral is False
        assert resp.blocks is None

    def test_all_fields(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        resp = AdapterResponse(
            text="hello",
            thread_id="t123",
            ephemeral=True,
            blocks=blocks,
        )
        assert resp.text == "hello"
        assert resp.thread_id == "t123"
        assert resp.ephemeral is True
        assert resp.blocks == blocks

    def test_equality(self):
        a = AdapterResponse(text="x", ephemeral=True)
        b = AdapterResponse(text="x", ephemeral=True)
        assert a == b

    def test_mutable(self):
        resp = AdapterResponse(text="original")
        resp.text = "updated"
        assert resp.text == "updated"


# ---------------------------------------------------------------------------
# DiscordAdapter stub
# ---------------------------------------------------------------------------


class TestDiscordAdapter:
    def test_platform_name(self):
        adapter = DiscordAdapter()
        assert adapter.platform == "discord"

    @pytest.mark.asyncio
    async def test_verify_signature_raises(self):
        adapter = DiscordAdapter()
        request = MagicMock()
        with pytest.raises(NotImplementedError, match="Discord"):
            await adapter.verify_signature(request)

    @pytest.mark.asyncio
    async def test_parse_event_raises(self):
        adapter = DiscordAdapter()
        request = MagicMock()
        with pytest.raises(NotImplementedError, match="Discord"):
            await adapter.parse_event(request)

    @pytest.mark.asyncio
    async def test_send_message_raises(self):
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError, match="Discord"):
            await adapter.send_message("channel", "hi")

    @pytest.mark.asyncio
    async def test_format_response_raises(self):
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError, match="Discord"):
            await adapter.format_response("hi")

    def test_satisfies_adapter_protocol(self):
        """DiscordAdapter should be recognized as an IntegrationAdapter."""
        adapter = DiscordAdapter()
        assert isinstance(adapter, IntegrationAdapter)


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal adapter for registry tests."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def platform(self) -> str:
        return self._name

    async def verify_signature(self, request: Any) -> bool:
        return True

    async def parse_event(self, request: Any) -> IntegrationEvent:
        return IntegrationEvent(
            platform=self._name,
            event_type=EventType.UNKNOWN,
            external_user_id="",
            channel_id="",
        )

    async def send_message(
        self, channel_id: str, content: str, **kwargs: Any
    ) -> str:
        return "msg-1"

    async def format_response(self, content: str, **kwargs: Any) -> dict:
        return {"text": content}


class TestAdapterRegistry:
    def test_empty_registry(self):
        reg = AdapterRegistry()
        assert len(reg) == 0
        assert reg.platforms == []
        assert "slack" not in reg

    def test_register_and_get(self):
        reg = AdapterRegistry()
        adapter = _FakeAdapter("slack")
        reg.register(adapter)
        assert reg.get("slack") is adapter
        assert "slack" in reg
        assert len(reg) == 1

    def test_get_missing_returns_none(self):
        reg = AdapterRegistry()
        assert reg.get("nonexistent") is None

    def test_get_or_raise_success(self):
        reg = AdapterRegistry()
        adapter = _FakeAdapter("slack")
        reg.register(adapter)
        assert reg.get_or_raise("slack") is adapter

    def test_get_or_raise_missing(self):
        reg = AdapterRegistry()
        with pytest.raises(IntegrationError, match="No adapter registered"):
            reg.get_or_raise("teams")

    def test_get_or_raise_error_has_platform(self):
        reg = AdapterRegistry()
        try:
            reg.get_or_raise("teams")
        except IntegrationError as e:
            assert e.platform == "teams"

    def test_register_multiple(self):
        reg = AdapterRegistry()
        reg.register(_FakeAdapter("slack"))
        reg.register(_FakeAdapter("discord"))
        assert sorted(reg.platforms) == ["discord", "slack"]
        assert len(reg) == 2

    def test_register_overwrites(self):
        reg = AdapterRegistry()
        first = _FakeAdapter("slack")
        second = _FakeAdapter("slack")
        reg.register(first)
        reg.register(second)
        assert reg.get("slack") is second
        assert len(reg) == 1

    def test_unregister(self):
        reg = AdapterRegistry()
        reg.register(_FakeAdapter("slack"))
        assert reg.unregister("slack") is True
        assert "slack" not in reg
        assert len(reg) == 0

    def test_unregister_missing(self):
        reg = AdapterRegistry()
        assert reg.unregister("nope") is False

    def test_contains(self):
        reg = AdapterRegistry()
        reg.register(_FakeAdapter("slack"))
        assert "slack" in reg
        assert "discord" not in reg

    def test_with_real_adapters(self):
        """Registry works with actual SlackAdapter and DiscordAdapter."""
        reg = AdapterRegistry()
        slack = SlackAdapter(
            signing_secret="secret", bot_token="xoxb-token"
        )
        discord = DiscordAdapter()
        reg.register(slack)
        reg.register(discord)
        assert reg.get("slack") is slack
        assert reg.get("discord") is discord
        assert sorted(reg.platforms) == ["discord", "slack"]
