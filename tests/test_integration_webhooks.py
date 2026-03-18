"""Tests for lucent.integrations.webhooks — WebhookSignatureMiddleware.

The webhooks.py module uses a different route pattern (/webhooks/{platform})
compared to middleware.py (/integrations/webhook/{provider}).
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lucent.integrations.webhooks import (
    WebhookSignatureMiddleware,
    _build_request,
    _client_ip,
    _default_get_adapter,
)
from lucent.integrations.slack_adapter import SlackAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIGNING_SECRET = "webhooks_test_secret_xyz"
_BOT_TOKEN = "xoxb-webhooks-test-token"


def _make_scope(
    path: str = "/webhooks/slack",
    *,
    client: tuple[str, int] | None = ("10.0.0.1", 9000),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
        "query_string": b"",
    }
    if client:
        scope["client"] = client
    return scope


def _make_receive(body: bytes) -> AsyncMock:
    calls = [
        {"type": "http.request", "body": body, "more_body": False},
    ]
    return AsyncMock(side_effect=calls)


def _make_multi_chunk_receive(chunks: list[bytes]) -> AsyncMock:
    messages = []
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        messages.append({
            "type": "http.request",
            "body": chunk,
            "more_body": not is_last,
        })
    return AsyncMock(side_effect=messages)


def _sign_body(
    body: bytes,
    signing_secret: str = _SIGNING_SECRET,
    timestamp: int | None = None,
) -> tuple[str, str]:
    ts = timestamp if timestamp is not None else int(time.time())
    sig_basestring = f"v0:{ts}:{body.decode('utf-8')}"
    sig = "v0=" + hmac_mod.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig, str(ts)


def _scope_with_slack_headers(
    body: bytes,
    *,
    signing_secret: str = _SIGNING_SECRET,
    timestamp: int | None = None,
    path: str = "/webhooks/slack",
) -> dict[str, Any]:
    sig, ts = _sign_body(body, signing_secret, timestamp)
    headers = [
        (b"x-slack-signature", sig.encode()),
        (b"x-slack-request-timestamp", ts.encode()),
        (b"content-type", b"application/json"),
    ]
    return _make_scope(path, headers=headers)


class _ResponseCapture:
    """Capture ASGI send() calls."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self) -> bytes:
        for m in self.messages:
            if m["type"] == "http.response.body":
                return m.get("body", b"")
        return b""

    @property
    def json(self) -> dict:
        return json.loads(self.body)


# ===========================================================================
# Non-webhook routes — passthrough
# ===========================================================================


class TestWebhookPassthrough:
    """Non-webhook routes pass straight through."""

    @pytest.mark.asyncio
    async def test_non_webhook_path(self) -> None:
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/api/v1/integrations")
        receive = _make_receive(b"")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_non_http_scope(self) -> None:
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = {"type": "websocket", "path": "/webhooks/slack"}
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_endpoint(self) -> None:
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/health")
        receive = _make_receive(b"")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()

    @pytest.mark.asyncio
    async def test_root_path(self) -> None:
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/")
        receive = _make_receive(b"")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()


# ===========================================================================
# Unknown platform
# ===========================================================================


class TestWebhookUnknownPlatform:
    """Unknown platform returns 404."""

    @pytest.mark.asyncio
    async def test_default_adapter_returns_none(self) -> None:
        result = await _default_get_adapter("slack")
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_platform_404(self) -> None:
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/webhooks/slack")
        body = b'{"test": true}'
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 404
        assert send.json["error"] == "Unknown platform"
        app.assert_not_called()


# ===========================================================================
# Signature verification (with real SlackAdapter)
# ===========================================================================


class TestWebhookSignatureVerification:
    """Signature verification pass/fail with SlackAdapter."""

    @pytest.mark.asyncio
    async def test_valid_signature_passes(self) -> None:
        adapter = SlackAdapter(
            signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN,
        )

        async def get_adapter(platform: str) -> SlackAdapter | None:
            return adapter if platform == "slack" else None

        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app, get_adapter=get_adapter)

        body = json.dumps({"type": "event_callback", "event": {"type": "message", "text": "hi", "user": "U1", "channel": "C1"}}).encode()
        scope = _scope_with_slack_headers(body)
        receive = _make_receive(body)
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()
        # Raw body cached in scope
        assert scope.get("_webhook_raw_body") == body

    @pytest.mark.asyncio
    async def test_invalid_signature_401(self) -> None:
        adapter = SlackAdapter(
            signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN,
        )

        async def get_adapter(platform: str) -> SlackAdapter | None:
            return adapter if platform == "slack" else None

        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app, get_adapter=get_adapter)

        body = b'{"test": "data"}'
        # Sign with wrong secret
        scope = _scope_with_slack_headers(body, signing_secret="wrong_secret")
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        assert send.json["error"] == "Invalid signature"
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_adapter_exception_returns_401(self) -> None:
        """If verify_signature raises, treat as invalid."""

        async def failing_get_adapter(platform: str):
            adapter = AsyncMock()
            adapter.verify_signature = AsyncMock(side_effect=Exception("boom"))
            return adapter

        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app, get_adapter=failing_get_adapter)

        body = b'{"test": true}'
        scope = _make_scope("/webhooks/slack")
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()


# ===========================================================================
# Replay attack detection
# ===========================================================================


class TestWebhookReplayAttack:
    """Stale timestamps rejected by SlackAdapter via middleware."""

    @pytest.mark.asyncio
    async def test_stale_timestamp_rejected(self) -> None:
        adapter = SlackAdapter(
            signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN,
        )

        async def get_adapter(platform: str) -> SlackAdapter | None:
            return adapter if platform == "slack" else None

        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app, get_adapter=get_adapter)

        body = b'{"type": "event_callback", "event": {"type": "message"}}'
        old_ts = int(time.time()) - 600  # 10 minutes ago
        scope = _scope_with_slack_headers(body, timestamp=old_ts)
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()


# ===========================================================================
# Body buffering and replay
# ===========================================================================


class TestWebhookBodyBuffering:
    """Raw body is buffered and replayed for downstream handlers."""

    @pytest.mark.asyncio
    async def test_body_replayed_downstream(self) -> None:
        adapter = SlackAdapter(
            signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN,
        )

        async def get_adapter(platform: str) -> SlackAdapter | None:
            return adapter if platform == "slack" else None

        captured_body = None

        async def capture_app(scope, receive, send):
            nonlocal captured_body
            msg = await receive()
            captured_body = msg.get("body")

        mw = WebhookSignatureMiddleware(capture_app, get_adapter=get_adapter)

        body = json.dumps({"type": "event_callback", "event": {"type": "message", "text": "test", "user": "U1", "channel": "C1"}}).encode()
        scope = _scope_with_slack_headers(body)
        receive = _make_receive(body)
        send = AsyncMock()

        await mw(scope, receive, send)
        assert captured_body == body

    @pytest.mark.asyncio
    async def test_multi_chunk_body(self) -> None:
        adapter = SlackAdapter(
            signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN,
        )

        async def get_adapter(platform: str) -> SlackAdapter | None:
            return adapter if platform == "slack" else None

        captured_body = None

        async def capture_app(scope, receive, send):
            nonlocal captured_body
            msg = await receive()
            captured_body = msg.get("body")

        mw = WebhookSignatureMiddleware(capture_app, get_adapter=get_adapter)

        full_body = json.dumps({"type": "event_callback", "event": {"type": "message", "text": "hello", "user": "U1", "channel": "C1"}}).encode()
        # Split into chunks but sign the full body
        scope = _scope_with_slack_headers(full_body)
        mid = len(full_body) // 2
        receive = _make_multi_chunk_receive([full_body[:mid], full_body[mid:]])
        send = AsyncMock()

        await mw(scope, receive, send)
        assert captured_body == full_body

    @pytest.mark.asyncio
    async def test_empty_body(self) -> None:
        """Empty body still goes through middleware."""

        async def get_adapter(platform: str):
            adapter = AsyncMock()
            adapter.verify_signature = AsyncMock(return_value=True)
            return adapter

        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app, get_adapter=get_adapter)

        scope = _make_scope("/webhooks/slack")
        receive = _make_receive(b"")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()


# ===========================================================================
# Route matching
# ===========================================================================


class TestWebhookRouteMatching:
    """Route regex matching for /webhooks/{platform}."""

    @pytest.mark.asyncio
    async def test_slack_route(self) -> None:
        """Matches /webhooks/slack."""
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/webhooks/slack")
        receive = _make_receive(b"{}")
        send = _ResponseCapture()

        await mw(scope, receive, send)
        # Should intercept (404 because default adapter returns None)
        assert send.status == 404

    @pytest.mark.asyncio
    async def test_discord_route(self) -> None:
        """Matches /webhooks/discord."""
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/webhooks/discord")
        receive = _make_receive(b"{}")
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 404

    @pytest.mark.asyncio
    async def test_trailing_slash(self) -> None:
        """Matches /webhooks/slack/ with trailing slash."""
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/webhooks/slack/")
        receive = _make_receive(b"{}")
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 404  # Intercepted, not passed through

    @pytest.mark.asyncio
    async def test_unknown_platform_name(self) -> None:
        """Unrecognized platform names don't match the webhook route regex."""
        app = AsyncMock()
        mw = WebhookSignatureMiddleware(app)
        scope = _make_scope("/webhooks/msteams")
        receive = _make_receive(b"{}")
        send = AsyncMock()

        await mw(scope, receive, send)
        # msteams doesn't match (slack|discord) — passes through
        app.assert_called_once()


# ===========================================================================
# Helper functions
# ===========================================================================


class TestWebhookHelpers:
    """Tests for module-level helper functions."""

    def test_build_request(self) -> None:
        scope = _make_scope("/webhooks/slack")
        body = b"test body"
        req = _build_request(scope, body)
        assert req.scope["path"] == "/webhooks/slack"

    def test_client_ip_with_client(self) -> None:
        scope = _make_scope(client=("192.168.1.1", 8080))
        assert _client_ip(scope) == "192.168.1.1"

    def test_client_ip_without_client(self) -> None:
        scope = _make_scope(client=None)
        assert _client_ip(scope) == "unknown"
