"""Tests for lucent.integrations.webhooks — WebhookSignatureMiddleware.

Covers:
- Slack HMAC-SHA256 verification (valid, tampered, missing headers, replay)
- Discord Ed25519 verification (valid, tampered, missing headers)
- Non-webhook routes pass through
- Unknown platform returns 404
- Body caching for downstream handlers
- Adapter exceptions handled gracefully
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from lucent.integrations.base import IntegrationAdapter
from lucent.integrations.slack_adapter import SlackAdapter
from lucent.integrations.webhooks import WebhookSignatureMiddleware, _build_request, _client_ip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIGNING_SECRET = "test_signing_secret_abc123"
_BOT_TOKEN = "xoxb-test-token"

# Ed25519 keypair for Discord tests
_DISCORD_SIGNING_KEY = SigningKey.generate()
_DISCORD_PUBLIC_KEY = _DISCORD_SIGNING_KEY.verify_key


def _make_scope(
    path: str = "/webhooks/slack",
    *,
    client: tuple[str, int] | None = ("127.0.0.1", 8000),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    """Build a minimal ASGI HTTP scope."""
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
    """Build an ASGI receive callable that yields a single body chunk."""
    calls = [
        {"type": "http.request", "body": body, "more_body": False},
    ]
    return AsyncMock(side_effect=calls)


def _sign_slack_body(
    body: bytes,
    signing_secret: str = _SIGNING_SECRET,
    timestamp: int | None = None,
) -> tuple[str, str]:
    """Compute a valid Slack HMAC-SHA256 signature.

    Returns (signature, timestamp_str).
    """
    ts = timestamp if timestamp is not None else int(time.time())
    sig_basestring = f"v0:{ts}:{body.decode('utf-8')}"
    sig = (
        "v0="
        + hmac_mod.new(
            signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    return sig, str(ts)


def _slack_headers(body: bytes, **kwargs: Any) -> list[tuple[bytes, bytes]]:
    """Build ASGI-style header tuples with valid Slack signature."""
    sig, ts = _sign_slack_body(body, **kwargs)
    return [
        (b"x-slack-signature", sig.encode()),
        (b"x-slack-request-timestamp", ts.encode()),
        (b"content-type", b"application/json"),
    ]


def _sign_discord_body(body: bytes) -> tuple[str, str]:
    """Sign body with Ed25519 for Discord.

    Discord signs: timestamp + body.
    Returns (signature_hex, timestamp).
    """
    ts = str(int(time.time()))
    message = ts.encode() + body
    signed = _DISCORD_SIGNING_KEY.sign(message, encoder=HexEncoder)
    # signed.signature is the hex-encoded signature
    return signed.signature.decode(), ts


def _discord_headers(body: bytes) -> list[tuple[bytes, bytes]]:
    """Build ASGI-style header tuples with valid Discord signature."""
    sig, ts = _sign_discord_body(body)
    return [
        (b"x-signature-ed25519", sig.encode()),
        (b"x-signature-timestamp", ts.encode()),
        (b"content-type", b"application/json"),
    ]


class _FakeDiscordAdapter:
    """Minimal Discord adapter that does Ed25519 verification.

    Implements only what the middleware needs: platform + verify_signature.
    """

    def __init__(self, public_key_hex: str) -> None:
        from nacl.encoding import HexEncoder as _Hex
        from nacl.signing import VerifyKey

        self._verify_key = VerifyKey(public_key_hex.encode(), encoder=_Hex)

    @property
    def platform(self) -> str:
        return "discord"

    async def verify_signature(self, request: Any) -> bool:
        sig_hex = request.headers.get("X-Signature-Ed25519")
        timestamp = request.headers.get("X-Signature-Timestamp")
        if not sig_hex or not timestamp:
            return False

        body = await request.body()
        message = timestamp.encode() + body

        try:
            self._verify_key.verify(message, bytes.fromhex(sig_hex))
            return True
        except Exception:
            return False


def _discord_public_key_hex() -> str:
    return _DISCORD_PUBLIC_KEY.encode(encoder=HexEncoder).decode()


# ---------------------------------------------------------------------------
# Captured response helper
# ---------------------------------------------------------------------------


class _ResponseCapture:
    """Capture ASGI response start + body."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


# ---------------------------------------------------------------------------
# Non-webhook passthrough
# ---------------------------------------------------------------------------


class TestPassthrough:
    @pytest.mark.asyncio
    async def test_non_webhook_route_passes_through(self) -> None:
        """Routes not matching /webhooks/{platform} are passed to inner app."""
        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner)

        scope = _make_scope(path="/api/v1/memories")
        receive = _make_receive(b"")
        send = _ResponseCapture()

        await mw(scope, receive, send)
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self) -> None:
        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner)

        scope = {"type": "websocket", "path": "/webhooks/slack"}
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_unmatched_platform_passes_through(self) -> None:
        """Platforms other than slack/discord are not intercepted."""
        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner)

        scope = _make_scope(path="/webhooks/teams")
        receive = _make_receive(b"")
        send = _ResponseCapture()

        await mw(scope, receive, send)
        inner.assert_called_once()


# ---------------------------------------------------------------------------
# Unknown / unconfigured platform
# ---------------------------------------------------------------------------


class TestUnknownPlatform:
    @pytest.mark.asyncio
    async def test_no_adapter_returns_404(self) -> None:
        """When get_adapter returns None, respond with 404."""

        async def no_adapter(platform: str) -> None:
            return None

        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner, get_adapter=no_adapter)

        body = b'{"type":"event_callback"}'
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 404
        inner.assert_not_called()


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------


class TestSlackVerification:
    @pytest.fixture()
    def slack_adapter(self) -> SlackAdapter:
        return SlackAdapter(signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN)

    @pytest.fixture()
    def middleware(self, slack_adapter: SlackAdapter) -> WebhookSignatureMiddleware:
        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            if platform == "slack":
                return slack_adapter
            return None

        inner = AsyncMock()
        return WebhookSignatureMiddleware(inner, get_adapter=get_adapter)

    @pytest.mark.asyncio
    async def test_valid_signature_passes(self, middleware: WebhookSignatureMiddleware) -> None:
        body = b'{"type":"event_callback"}'
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        # Inner app was called (passthrough)
        middleware.app.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_signature_with_trailing_slash(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        body = b'{"type":"event_callback"}'
        scope = _make_scope(path="/webhooks/slack/", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        middleware.app.assert_called_once()

    @pytest.mark.asyncio
    async def test_tampered_body_returns_401(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        body = b'{"type":"event_callback"}'
        tampered = b'{"type":"TAMPERED"}'
        # Sign the original body, but send tampered body
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(tampered)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        assert send.status == 401
        resp = json.loads(send.body)
        assert resp["error"] == "Invalid signature"

    @pytest.mark.asyncio
    async def test_missing_headers_returns_401(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        body = b'{"type":"event_callback"}'
        scope = _make_scope(path="/webhooks/slack", headers=[])
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        assert send.status == 401

    @pytest.mark.asyncio
    async def test_stale_timestamp_returns_401(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        """Timestamps older than 5 minutes are rejected (replay protection)."""
        body = b'{"type":"event_callback"}'
        old_ts = int(time.time()) - 600  # 10 minutes ago
        headers = _slack_headers(body, timestamp=old_ts)
        scope = _make_scope(path="/webhooks/slack", headers=headers)
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        assert send.status == 401

    @pytest.mark.asyncio
    async def test_wrong_secret_returns_401(self) -> None:
        """A different signing secret should fail verification."""
        wrong_adapter = SlackAdapter(signing_secret="wrong_secret", bot_token=_BOT_TOKEN)

        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            return wrong_adapter if platform == "slack" else None

        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner, get_adapter=get_adapter)

        body = b'{"type":"event_callback"}'
        # Signed with _SIGNING_SECRET but adapter has "wrong_secret"
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401


# ---------------------------------------------------------------------------
# Discord signature verification
# ---------------------------------------------------------------------------


class TestDiscordVerification:
    @pytest.fixture()
    def discord_adapter(self) -> _FakeDiscordAdapter:
        return _FakeDiscordAdapter(_discord_public_key_hex())

    @pytest.fixture()
    def middleware(self, discord_adapter: _FakeDiscordAdapter) -> WebhookSignatureMiddleware:
        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            if platform == "discord":
                return discord_adapter
            return None

        inner = AsyncMock()
        return WebhookSignatureMiddleware(inner, get_adapter=get_adapter)

    @pytest.mark.asyncio
    async def test_valid_signature_passes(self, middleware: WebhookSignatureMiddleware) -> None:
        body = b'{"type":1}'
        scope = _make_scope(path="/webhooks/discord", headers=_discord_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        middleware.app.assert_called_once()

    @pytest.mark.asyncio
    async def test_tampered_body_returns_401(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        body = b'{"type":1}'
        tampered = b'{"type":2}'
        scope = _make_scope(path="/webhooks/discord", headers=_discord_headers(body))
        receive = _make_receive(tampered)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        assert send.status == 401

    @pytest.mark.asyncio
    async def test_missing_headers_returns_401(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        body = b'{"type":1}'
        scope = _make_scope(path="/webhooks/discord", headers=[])
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        assert send.status == 401

    @pytest.mark.asyncio
    async def test_bad_signature_returns_401(
        self, middleware: WebhookSignatureMiddleware
    ) -> None:
        """A completely invalid signature hex should return 401."""
        body = b'{"type":1}'
        headers = [
            (b"x-signature-ed25519", b"00" * 64),
            (b"x-signature-timestamp", str(int(time.time())).encode()),
            (b"content-type", b"application/json"),
        ]
        scope = _make_scope(path="/webhooks/discord", headers=headers)
        receive = _make_receive(body)
        send = _ResponseCapture()

        await middleware(scope, receive, send)
        assert send.status == 401

    @pytest.mark.asyncio
    async def test_wrong_key_returns_401(self) -> None:
        """A different public key should fail verification."""
        other_key = SigningKey.generate()
        other_pub = other_key.verify_key.encode(encoder=HexEncoder).decode()
        adapter = _FakeDiscordAdapter(other_pub)

        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            return adapter if platform == "discord" else None

        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner, get_adapter=get_adapter)

        body = b'{"type":1}'
        # Signed with _DISCORD_SIGNING_KEY but adapter has other_key
        scope = _make_scope(path="/webhooks/discord", headers=_discord_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401


# ---------------------------------------------------------------------------
# Body caching and replay
# ---------------------------------------------------------------------------


class TestBodyCaching:
    @pytest.mark.asyncio
    async def test_raw_body_cached_in_scope(self) -> None:
        """After verification, raw body should be in scope['_webhook_raw_body']."""
        adapter = SlackAdapter(signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN)
        captured_scope: dict[str, Any] = {}

        async def capture_app(scope: dict, receive: Any, send: Any) -> None:
            captured_scope.update(scope)
            # Also verify we can read the replayed body
            msg = await receive()
            captured_scope["_replayed_body"] = msg["body"]

        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            return adapter if platform == "slack" else None

        mw = WebhookSignatureMiddleware(capture_app, get_adapter=get_adapter)

        body = b'{"type":"event_callback"}'
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)

        assert captured_scope["_webhook_raw_body"] == body
        assert captured_scope["_replayed_body"] == body

    @pytest.mark.asyncio
    async def test_body_replay_matches_original(self) -> None:
        """The replayed body should be byte-identical to what was sent."""
        adapter = SlackAdapter(signing_secret=_SIGNING_SECRET, bot_token=_BOT_TOKEN)
        replayed_bodies: list[bytes] = []

        async def capture_app(scope: dict, receive: Any, send: Any) -> None:
            msg = await receive()
            replayed_bodies.append(msg["body"])

        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            return adapter if platform == "slack" else None

        mw = WebhookSignatureMiddleware(capture_app, get_adapter=get_adapter)

        body = b'{"complex": "payload", "nested": {"key": "value"}}'
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)

        assert len(replayed_bodies) == 1
        assert replayed_bodies[0] == body


# ---------------------------------------------------------------------------
# Adapter error handling
# ---------------------------------------------------------------------------


class TestAdapterErrors:
    @pytest.mark.asyncio
    async def test_adapter_exception_returns_401(self) -> None:
        """If adapter.verify_signature raises, treat as verification failure."""
        adapter = AsyncMock(spec=IntegrationAdapter)
        adapter.platform = "slack"
        adapter.verify_signature = AsyncMock(side_effect=RuntimeError("boom"))

        async def get_adapter(platform: str) -> IntegrationAdapter | None:
            return adapter if platform == "slack" else None

        inner = AsyncMock()
        mw = WebhookSignatureMiddleware(inner, get_adapter=get_adapter)

        body = b'{"type":"event_callback"}'
        scope = _make_scope(path="/webhooks/slack", headers=_slack_headers(body))
        receive = _make_receive(body)
        send = _ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        inner.assert_not_called()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_client_ip_from_scope(self) -> None:
        scope: dict[str, Any] = {"client": ("10.0.0.1", 9000)}
        assert _client_ip(scope) == "10.0.0.1"

    def test_client_ip_missing(self) -> None:
        scope: dict[str, Any] = {}
        assert _client_ip(scope) == "unknown"

    @pytest.mark.asyncio
    async def test_build_request_body(self) -> None:
        scope = _make_scope()
        body = b"test body content"
        request = _build_request(scope, body)
        result = await request.body()
        assert result == body
