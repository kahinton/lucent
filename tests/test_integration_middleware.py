"""Tests for lucent.integrations.middleware — SignatureVerificationMiddleware.

Covers:
- Signature verification pass/fail (with real HMAC via SlackAdapter)
- Replay attack detection (old timestamps)
- Missing headers
- Non-webhook routes passthrough
- Body buffering for downstream handlers
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lucent.integrations.middleware import (
    SignatureVerificationMiddleware,
    _build_request,
    _client_ip,
)
from lucent.integrations.slack_adapter import SlackAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIGNING_SECRET = "test_signing_secret_abc123"
_BOT_TOKEN = "xoxb-test-token"


def _make_scope(
    path: str = "/integrations/webhook/slack",
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
    receive = AsyncMock(side_effect=calls)
    return receive


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


def _make_slack_scope(
    body: bytes,
    *,
    signing_secret: str = _SIGNING_SECRET,
    timestamp: int | None = None,
    omit_signature: bool = False,
    omit_timestamp: bool = False,
    tamper_signature: bool = False,
) -> tuple[dict[str, Any], AsyncMock]:
    """Build ASGI scope+receive with Slack signature headers."""
    sig, ts_str = _sign_slack_body(body, signing_secret, timestamp)
    if tamper_signature:
        sig = sig[:-4] + "dead"

    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
    ]
    if not omit_signature:
        headers.append((b"x-slack-signature", sig.encode()))
    if not omit_timestamp:
        headers.append((b"x-slack-request-timestamp", ts_str.encode()))

    scope = _make_scope(headers=headers)
    receive = _make_receive(body)
    return scope, receive


class ResponseCapture:
    """Captures ASGI send() calls for assertions."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
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


class FakeAdapter:
    """Test adapter with controllable verify_signature result."""

    def __init__(self, *, valid: bool = True, raise_error: bool = False) -> None:
        self._valid = valid
        self._raise_error = raise_error

    @property
    def platform(self) -> str:
        return "fake"

    async def verify_signature(self, request: Any) -> bool:
        if self._raise_error:
            raise RuntimeError("verification exploded")
        return self._valid


def _slack_adapter() -> SlackAdapter:
    """Create a SlackAdapter with test credentials."""
    return SlackAdapter(
        signing_secret=_SIGNING_SECRET,
        bot_token=_BOT_TOKEN,
        bot_user_id="U_TEST",
    )


def _get_slack_adapter():
    """Factory returning an async get_adapter function for SlackAdapter."""
    adapter = _slack_adapter()

    async def get_adapter(provider: str) -> SlackAdapter | None:
        if provider == "slack":
            return adapter
        return None

    return get_adapter


def _get_fake_adapter(*, valid: bool = True, raise_error: bool = False):
    """Factory returning an async get_adapter function for FakeAdapter."""
    adapter = FakeAdapter(valid=valid, raise_error=raise_error)

    async def get_adapter(provider: str) -> FakeAdapter:
        return adapter

    return get_adapter


# ---------------------------------------------------------------------------
# Tests — Non-webhook routes passthrough
# ---------------------------------------------------------------------------


class TestNonWebhookRoutes:
    """Middleware should pass through non-webhook paths untouched."""

    @pytest.mark.asyncio
    async def test_non_webhook_path_passes_through(self) -> None:
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/api/v1/memories")
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self) -> None:
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = {"type": "websocket", "path": "/ws"}
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_health_endpoint_passes_through(self) -> None:
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/health")
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_integration_non_webhook_path_passes_through(self) -> None:
        """Paths under /integrations/ that aren't /webhook/ pass through."""
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/integrations/config/slack")
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_passthrough_preserves_original_receive(self) -> None:
        """Non-webhook routes get the original receive callable, not a replay."""
        original_receive = AsyncMock()
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/api/v1/memories")

        await mw(scope, original_receive, AsyncMock())
        # The exact same receive object should be passed downstream
        _, args, _ = app.mock_calls[0]
        assert args[1] is original_receive


# ---------------------------------------------------------------------------
# Tests — Unknown provider
# ---------------------------------------------------------------------------


class TestUnknownProvider:
    """Middleware should 404 for unknown providers."""

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_404(self) -> None:
        app = AsyncMock()

        async def no_adapter(provider: str) -> None:
            return None

        mw = SignatureVerificationMiddleware(app, get_adapter=no_adapter)
        scope = _make_scope(path="/integrations/webhook/unknown_platform")
        receive = _make_receive(b'{"test": true}')
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 404
        assert send.json["error"] == "Unknown provider"
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_adapter_returns_404(self) -> None:
        """With no get_adapter supplied, all providers are unknown."""
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/integrations/webhook/slack")
        receive = _make_receive(b'{"test": true}')
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 404


# ---------------------------------------------------------------------------
# Tests — Signature verification pass/fail (with FakeAdapter)
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    """Core signature verification flow using mock adapters."""

    @pytest.mark.asyncio
    async def test_valid_signature_passes(self) -> None:
        downstream_called = False

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_called
            downstream_called = True
            msg = await receive()
            assert msg["body"] == b'{"event": "data"}'
            assert msg["more_body"] is False

        adapter = FakeAdapter(valid=True)

        async def get_adapter(provider: str) -> FakeAdapter:
            return adapter

        mw = SignatureVerificationMiddleware(downstream_app, get_adapter=get_adapter)
        scope = _make_scope()
        receive = _make_receive(b'{"event": "data"}')
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_called

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self) -> None:
        app = AsyncMock()
        adapter = FakeAdapter(valid=False)

        async def get_adapter(provider: str) -> FakeAdapter:
            return adapter

        mw = SignatureVerificationMiddleware(app, get_adapter=get_adapter)
        scope = _make_scope()
        receive = _make_receive(b'{"event": "data"}')
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        assert send.json["error"] == "Invalid signature"
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_verification_exception_returns_401(self) -> None:
        app = AsyncMock()
        adapter = FakeAdapter(raise_error=True)

        async def get_adapter(provider: str) -> FakeAdapter:
            return adapter

        mw = SignatureVerificationMiddleware(app, get_adapter=get_adapter)
        scope = _make_scope()
        receive = _make_receive(b'{"event": "data"}')
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_401_response_is_json_content_type(self) -> None:
        """Verify 401 response has proper JSON content-type header."""
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app, get_adapter=_get_fake_adapter(valid=False))
        scope = _make_scope()
        receive = _make_receive(b'{"event": "data"}')
        send = ResponseCapture()

        await mw(scope, receive, send)
        start_msg = send.messages[0]
        headers = dict(start_msg["headers"])
        assert headers[b"content-type"] == b"application/json"


# ---------------------------------------------------------------------------
# Tests — Real Slack HMAC signature verification
# ---------------------------------------------------------------------------


class TestSlackSignatureVerification:
    """End-to-end signature verification with actual Slack HMAC computation."""

    @pytest.mark.asyncio
    async def test_valid_slack_signature_passes(self) -> None:
        """A correctly signed Slack request is accepted."""
        downstream_called = False
        body = b'{"type":"event_callback","event":{"type":"message"}}'

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_called
            downstream_called = True

        scope, receive = _make_slack_scope(body)
        mw = SignatureVerificationMiddleware(downstream_app, get_adapter=_get_slack_adapter())
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_called

    @pytest.mark.asyncio
    async def test_tampered_slack_signature_rejected(self) -> None:
        """A request with a tampered signature is rejected with 401."""
        app = AsyncMock()
        body = b'{"type":"event_callback","event":{"type":"message"}}'
        scope, receive = _make_slack_scope(body, tamper_signature=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_signing_secret_rejected(self) -> None:
        """Signature computed with a different secret is rejected."""
        app = AsyncMock()
        body = b'{"event":"data"}'
        # Sign with a different secret than what the adapter uses
        scope, receive = _make_slack_scope(body, signing_secret="wrong_secret_xyz")

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_tampered_body_rejected(self) -> None:
        """Signature was valid for original body, but body was tampered."""
        app = AsyncMock()
        original_body = b'{"event":"original"}'
        sig, ts = _sign_slack_body(original_body)

        # Build scope with valid sig but send different body
        headers = [
            (b"content-type", b"application/json"),
            (b"x-slack-signature", sig.encode()),
            (b"x-slack-request-timestamp", ts.encode()),
        ]
        scope = _make_scope(headers=headers)
        receive = _make_receive(b'{"event":"tampered"}')

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Replay attack detection (stale timestamps)
# ---------------------------------------------------------------------------


class TestReplayAttackDetection:
    """Slack rejects requests with timestamps older than 5 minutes."""

    @pytest.mark.asyncio
    async def test_stale_timestamp_rejected(self) -> None:
        """A request older than 300 seconds is rejected (replay attack)."""
        app = AsyncMock()
        body = b'{"event":"data"}'
        old_timestamp = int(time.time()) - 600  # 10 minutes ago
        scope, receive = _make_slack_scope(body, timestamp=old_timestamp)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_future_timestamp_rejected(self) -> None:
        """A request with a timestamp too far in the future is rejected."""
        app = AsyncMock()
        body = b'{"event":"data"}'
        future_timestamp = int(time.time()) + 600  # 10 minutes in the future
        scope, receive = _make_slack_scope(body, timestamp=future_timestamp)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_timestamp_just_within_window_accepted(self) -> None:
        """A request 250 seconds old (within 300s window) is accepted."""
        downstream_called = False
        body = b'{"event":"data"}'
        recent_timestamp = int(time.time()) - 250

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_called
            downstream_called = True

        scope, receive = _make_slack_scope(body, timestamp=recent_timestamp)
        mw = SignatureVerificationMiddleware(downstream_app, get_adapter=_get_slack_adapter())
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_called

    @pytest.mark.asyncio
    async def test_timestamp_just_outside_window_rejected(self) -> None:
        """A request 301 seconds old (just past the 300s window) is rejected."""
        app = AsyncMock()
        body = b'{"event":"data"}'
        stale_timestamp = int(time.time()) - 301
        scope, receive = _make_slack_scope(body, timestamp=stale_timestamp)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Missing headers
# ---------------------------------------------------------------------------


class TestMissingHeaders:
    """SlackAdapter rejects requests missing required signature headers."""

    @pytest.mark.asyncio
    async def test_missing_signature_header_rejected(self) -> None:
        app = AsyncMock()
        body = b'{"event":"data"}'
        scope, receive = _make_slack_scope(body, omit_signature=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_timestamp_header_rejected(self) -> None:
        app = AsyncMock()
        body = b'{"event":"data"}'
        scope, receive = _make_slack_scope(body, omit_timestamp=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_both_headers_rejected(self) -> None:
        app = AsyncMock()
        body = b'{"event":"data"}'
        scope, receive = _make_slack_scope(body, omit_signature=True, omit_timestamp=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_integer_timestamp_rejected(self) -> None:
        """A non-numeric timestamp header is rejected."""
        app = AsyncMock()
        body = b'{"event":"data"}'
        sig, _ = _sign_slack_body(body)
        headers = [
            (b"content-type", b"application/json"),
            (b"x-slack-signature", sig.encode()),
            (b"x-slack-request-timestamp", b"not-a-number"),
        ]
        scope = _make_scope(headers=headers)
        receive = _make_receive(body)

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_body_with_no_headers_rejected(self) -> None:
        """An empty body with no auth headers is rejected."""
        app = AsyncMock()
        scope = _make_scope()  # no headers
        receive = _make_receive(b"")

        mw = SignatureVerificationMiddleware(app, get_adapter=_get_slack_adapter())
        send = ResponseCapture()

        await mw(scope, receive, send)
        assert send.status == 401
        app.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Route matching
# ---------------------------------------------------------------------------


class TestRouteMatching:
    """Webhook route regex matching."""

    @pytest.mark.asyncio
    async def test_slack_route(self) -> None:
        app = AsyncMock()

        async def get_adapter(provider: str) -> FakeAdapter:
            assert provider == "slack"
            return FakeAdapter(valid=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=get_adapter)
        scope = _make_scope(path="/integrations/webhook/slack")
        receive = _make_receive(b"{}")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()

    @pytest.mark.asyncio
    async def test_discord_route(self) -> None:
        app = AsyncMock()

        async def get_adapter(provider: str) -> FakeAdapter:
            assert provider == "discord"
            return FakeAdapter(valid=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=get_adapter)
        scope = _make_scope(path="/integrations/webhook/discord")
        receive = _make_receive(b"{}")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()

    @pytest.mark.asyncio
    async def test_trailing_slash(self) -> None:
        app = AsyncMock()

        async def get_adapter(provider: str) -> FakeAdapter:
            return FakeAdapter(valid=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=get_adapter)
        scope = _make_scope(path="/integrations/webhook/slack/")
        receive = _make_receive(b"{}")
        send = AsyncMock()

        await mw(scope, receive, send)
        app.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_matching_similar_path(self) -> None:
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/integrations/webhook/slack/extra")
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        # Should pass through since the regex doesn't match
        app.assert_called_once()

    @pytest.mark.asyncio
    async def test_provider_name_with_hyphens(self) -> None:
        app = AsyncMock()
        extracted_provider = None

        async def get_adapter(provider: str) -> FakeAdapter:
            nonlocal extracted_provider
            extracted_provider = provider
            return FakeAdapter(valid=True)

        mw = SignatureVerificationMiddleware(app, get_adapter=get_adapter)
        scope = _make_scope(path="/integrations/webhook/my-custom-bot")
        receive = _make_receive(b"{}")
        send = AsyncMock()

        await mw(scope, receive, send)
        assert extracted_provider == "my-custom-bot"

    @pytest.mark.asyncio
    async def test_provider_must_start_with_letter(self) -> None:
        """Provider names starting with a digit don't match the route."""
        app = AsyncMock()
        mw = SignatureVerificationMiddleware(app)
        scope = _make_scope(path="/integrations/webhook/123bad")
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)
        # Passes through since regex requires ^[a-z]
        app.assert_called_once_with(scope, receive, send)


# ---------------------------------------------------------------------------
# Tests — Body buffering for downstream handlers
# ---------------------------------------------------------------------------


class TestBodyBuffering:
    """Body buffering and replay for downstream handlers."""

    @pytest.mark.asyncio
    async def test_single_chunk_body_replayed(self) -> None:
        """Single-chunk body is correctly replayed to downstream."""
        downstream_body = b""

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_body
            msg = await receive()
            downstream_body = msg["body"]
            assert msg["more_body"] is False

        mw = SignatureVerificationMiddleware(
            downstream_app, get_adapter=_get_fake_adapter(valid=True)
        )
        scope = _make_scope()
        receive = _make_receive(b'{"single":"chunk"}')
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_body == b'{"single":"chunk"}'

    @pytest.mark.asyncio
    async def test_multi_chunk_body_reassembled(self) -> None:
        """Multiple body chunks are joined and replayed as single message."""
        downstream_body = b""

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_body
            msg = await receive()
            downstream_body = msg["body"]

        chunks = [
            {"type": "http.request", "body": b'{"first": ', "more_body": True},
            {"type": "http.request", "body": b'"chunk"}', "more_body": False},
        ]
        receive = AsyncMock(side_effect=chunks)

        mw = SignatureVerificationMiddleware(
            downstream_app, get_adapter=_get_fake_adapter(valid=True)
        )
        scope = _make_scope()
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_body == b'{"first": "chunk"}'

    @pytest.mark.asyncio
    async def test_three_chunk_body(self) -> None:
        """Three body chunks are properly reassembled."""
        downstream_body = b""

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_body
            msg = await receive()
            downstream_body = msg["body"]

        chunks = [
            {"type": "http.request", "body": b"aaa", "more_body": True},
            {"type": "http.request", "body": b"bbb", "more_body": True},
            {"type": "http.request", "body": b"ccc", "more_body": False},
        ]
        receive = AsyncMock(side_effect=chunks)

        mw = SignatureVerificationMiddleware(
            downstream_app, get_adapter=_get_fake_adapter(valid=True)
        )
        scope = _make_scope()
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_body == b"aaabbbccc"

    @pytest.mark.asyncio
    async def test_empty_body_replayed(self) -> None:
        """An empty body is correctly buffered and replayed."""
        downstream_body = None

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_body
            msg = await receive()
            downstream_body = msg["body"]

        mw = SignatureVerificationMiddleware(
            downstream_app, get_adapter=_get_fake_adapter(valid=True)
        )
        scope = _make_scope()
        receive = _make_receive(b"")
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_body == b""

    @pytest.mark.asyncio
    async def test_replay_receive_yields_body_once_then_delegates(self) -> None:
        """After body is replayed, subsequent receive() calls delegate to original."""
        receive_calls: list[dict] = []

        # After body is consumed, the next receive() should delegate to the
        # original receive callable (e.g., for http.disconnect).
        disconnect_msg = {"type": "http.disconnect"}
        original_receive = AsyncMock(
            side_effect=[
                {"type": "http.request", "body": b"body", "more_body": False},
                disconnect_msg,
            ]
        )

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            msg1 = await receive()
            receive_calls.append(msg1)
            msg2 = await receive()
            receive_calls.append(msg2)

        mw = SignatureVerificationMiddleware(
            downstream_app, get_adapter=_get_fake_adapter(valid=True)
        )
        scope = _make_scope()
        send = AsyncMock()

        await mw(scope, original_receive, send)

        # First call: replayed body
        assert receive_calls[0]["type"] == "http.request"
        assert receive_calls[0]["body"] == b"body"
        # Second call: delegated to original receive (disconnect)
        assert receive_calls[1]["type"] == "http.disconnect"

    @pytest.mark.asyncio
    async def test_large_body_buffered_correctly(self) -> None:
        """A large body (100KB) is buffered and replayed intact."""
        large_body = b"x" * 100_000
        downstream_body = b""

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_body
            msg = await receive()
            downstream_body = msg["body"]

        # Deliver in 10KB chunks
        chunk_size = 10_000
        chunks = []
        for i in range(0, len(large_body), chunk_size):
            chunk = large_body[i : i + chunk_size]
            is_last = (i + chunk_size) >= len(large_body)
            chunks.append({"type": "http.request", "body": chunk, "more_body": not is_last})
        receive = AsyncMock(side_effect=chunks)

        mw = SignatureVerificationMiddleware(
            downstream_app, get_adapter=_get_fake_adapter(valid=True)
        )
        scope = _make_scope()
        send = AsyncMock()

        await mw(scope, receive, send)
        assert downstream_body == large_body
        assert len(downstream_body) == 100_000

    @pytest.mark.asyncio
    async def test_body_available_for_both_verification_and_downstream(self) -> None:
        """The adapter sees the body for verification AND downstream gets it too."""
        adapter_saw_body = b""
        downstream_body = b""

        class BodyCapturingAdapter:
            @property
            def platform(self) -> str:
                return "capture"

            async def verify_signature(self, request: Any) -> bool:
                nonlocal adapter_saw_body
                adapter_saw_body = await request.body()
                return True

        async def downstream_app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal downstream_body
            msg = await receive()
            downstream_body = msg["body"]

        async def get_adapter(provider: str) -> BodyCapturingAdapter:
            return BodyCapturingAdapter()

        body = b'{"important":"payload"}'
        mw = SignatureVerificationMiddleware(downstream_app, get_adapter=get_adapter)
        scope = _make_scope()
        receive = _make_receive(body)
        send = AsyncMock()

        await mw(scope, receive, send)
        assert adapter_saw_body == body
        assert downstream_body == body


# ---------------------------------------------------------------------------
# Tests — Helper functions
# ---------------------------------------------------------------------------


class TestBuildRequest:
    def test_build_request_has_body(self) -> None:
        scope = _make_scope()
        req = _build_request(scope, b"test body")
        assert req is not None

    @pytest.mark.asyncio
    async def test_build_request_body_readable(self) -> None:
        scope = _make_scope()
        req = _build_request(scope, b"test body")
        body = await req.body()
        assert body == b"test body"

    @pytest.mark.asyncio
    async def test_build_request_preserves_headers(self) -> None:
        headers = [(b"x-custom", b"value")]
        scope = _make_scope(headers=headers)
        req = _build_request(scope, b"body")
        assert req.headers.get("x-custom") == "value"


class TestClientIp:
    def test_with_client(self) -> None:
        scope = _make_scope(client=("192.168.1.1", 9000))
        assert _client_ip(scope) == "192.168.1.1"

    def test_without_client(self) -> None:
        scope = _make_scope(client=None)
        assert _client_ip(scope) == "unknown"
