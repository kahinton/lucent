"""Tests for lucent.integrations.adapters — AdapterRegistry, AdapterResponse, DiscordAdapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lucent.integrations.adapters import (
    AdapterRegistry,
    AdapterResponse,
    DiscordAdapter,
    IntegrationAdapter,
    IntegrationError,
)
from lucent.integrations.models import EventType, IntegrationEvent


# ---------------------------------------------------------------------------
# AdapterResponse
# ---------------------------------------------------------------------------


class TestAdapterResponse:
    """Tests for the AdapterResponse dataclass."""

    def test_defaults(self) -> None:
        r = AdapterResponse(text="hello")
        assert r.text == "hello"
        assert r.thread_id is None
        assert r.ephemeral is False
        assert r.blocks is None

    def test_all_fields(self) -> None:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        r = AdapterResponse(
            text="hello",
            thread_id="t123",
            ephemeral=True,
            blocks=blocks,
        )
        assert r.text == "hello"
        assert r.thread_id == "t123"
        assert r.ephemeral is True
        assert r.blocks == blocks

    def test_is_mutable(self) -> None:
        """AdapterResponse is a regular dataclass, not frozen."""
        r = AdapterResponse(text="a")
        r.text = "b"
        assert r.text == "b"


# ---------------------------------------------------------------------------
# DiscordAdapter stub
# ---------------------------------------------------------------------------


class TestDiscordAdapter:
    """Tests for the DiscordAdapter stub."""

    def test_platform_property(self) -> None:
        adapter = DiscordAdapter()
        assert adapter.platform == "discord"

    @pytest.mark.asyncio
    async def test_verify_signature_not_implemented(self) -> None:
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError, match="Discord adapter"):
            await adapter.verify_signature(MagicMock())

    @pytest.mark.asyncio
    async def test_parse_event_not_implemented(self) -> None:
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError, match="Discord adapter"):
            await adapter.parse_event(MagicMock())

    @pytest.mark.asyncio
    async def test_send_message_not_implemented(self) -> None:
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError, match="Discord adapter"):
            await adapter.send_message("C123", "hello")

    @pytest.mark.asyncio
    async def test_format_response_not_implemented(self) -> None:
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError, match="Discord adapter"):
            await adapter.format_response("hello")

    @pytest.mark.asyncio
    async def test_send_message_kwargs_not_implemented(self) -> None:
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.send_message("C1", "hi", thread_id="t1", metadata={"k": "v"})

    @pytest.mark.asyncio
    async def test_format_response_kwargs_not_implemented(self) -> None:
        adapter = DiscordAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.format_response("hi", ephemeral=True, metadata={"k": "v"})


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal adapter for registry tests."""

    def __init__(self, name: str = "fake") -> None:
        self._name = name

    @property
    def platform(self) -> str:
        return self._name

    async def verify_signature(self, request: Any) -> bool:
        return True

    async def parse_event(self, request: Any) -> IntegrationEvent:
        return IntegrationEvent(
            event_type=EventType.MESSAGE,
            platform=self._name,
            external_user_id="u1",
            channel_id="c1",
            text="hi",
        )

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return "ok"

    async def format_response(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"text": content}


class TestAdapterRegistryBasic:
    """Basic registry operations."""

    def test_empty_registry(self) -> None:
        reg = AdapterRegistry()
        assert len(reg) == 0
        assert reg.platforms == []
        assert "slack" not in reg

    def test_register_and_get(self) -> None:
        reg = AdapterRegistry()
        adapter = _FakeAdapter("slack")
        reg.register(adapter)

        assert reg.get("slack") is adapter
        assert "slack" in reg
        assert len(reg) == 1
        assert reg.platforms == ["slack"]

    def test_get_missing_returns_none(self) -> None:
        reg = AdapterRegistry()
        assert reg.get("nonexistent") is None

    def test_get_or_raise_missing(self) -> None:
        reg = AdapterRegistry()
        with pytest.raises(IntegrationError, match="No adapter registered"):
            reg.get_or_raise("missing")

    def test_get_or_raise_existing(self) -> None:
        reg = AdapterRegistry()
        adapter = _FakeAdapter("slack")
        reg.register(adapter)
        assert reg.get_or_raise("slack") is adapter

    def test_unregister_existing(self) -> None:
        reg = AdapterRegistry()
        reg.register(_FakeAdapter("slack"))
        assert reg.unregister("slack") is True
        assert "slack" not in reg
        assert len(reg) == 0

    def test_unregister_missing(self) -> None:
        reg = AdapterRegistry()
        assert reg.unregister("nonexistent") is False


class TestAdapterRegistryMultiple:
    """Registry with multiple adapters."""

    def test_register_multiple_platforms(self) -> None:
        reg = AdapterRegistry()
        reg.register(_FakeAdapter("slack"))
        reg.register(_FakeAdapter("discord"))

        assert len(reg) == 2
        assert set(reg.platforms) == {"slack", "discord"}
        assert reg.get("slack") is not None
        assert reg.get("discord") is not None

    def test_register_replaces_existing(self) -> None:
        reg = AdapterRegistry()
        a1 = _FakeAdapter("slack")
        a2 = _FakeAdapter("slack")
        reg.register(a1)
        reg.register(a2)

        assert len(reg) == 1
        assert reg.get("slack") is a2

    def test_unregister_one_of_many(self) -> None:
        reg = AdapterRegistry()
        reg.register(_FakeAdapter("slack"))
        reg.register(_FakeAdapter("discord"))
        reg.unregister("slack")

        assert len(reg) == 1
        assert "slack" not in reg
        assert "discord" in reg


class TestAdapterRegistryErrorDetails:
    """Error metadata on get_or_raise."""

    def test_error_includes_platform(self) -> None:
        reg = AdapterRegistry()
        try:
            reg.get_or_raise("msteams")
        except IntegrationError as exc:
            assert exc.platform == "msteams"
