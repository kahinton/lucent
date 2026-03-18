"""ASGI middleware for webhook signature verification on /webhooks/{platform}.

Intercepts requests to ``/webhooks/slack`` and ``/webhooks/discord``, reads
the raw body and signature headers **before** any JSON parsing (per the
security review requirement), then delegates to the appropriate
``IntegrationAdapter.verify_signature()``.

Invalid signatures return 401.  Valid requests have their raw body cached
and replayed for downstream handlers so the body stream is not consumed.

Slack verification: HMAC-SHA256 with signing secret + timestamp replay
protection (reject timestamps >5 min old).

Discord verification: Ed25519 signature verification per Discord docs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from lucent.integrations.base import IntegrationAdapter
from lucent.logging import get_logger

logger = get_logger("integrations.webhooks")

# Matches /webhooks/slack or /webhooks/discord (with optional trailing slash)
_WEBHOOK_ROUTE = re.compile(r"^/webhooks/(?P<platform>slack|discord)/?$")

GetAdapterFn = Callable[[str], Awaitable[IntegrationAdapter | None]]


class WebhookSignatureMiddleware:
    """Pure ASGI middleware that verifies webhook signatures pre-parse.

    Uses pure ASGI (not ``BaseHTTPMiddleware``) to avoid thread-pool dispatch
    that would break ContextVar propagation — same rationale as the existing
    ``SignatureVerificationMiddleware`` and ``MCPAuthMiddleware``.

    Adapter lookup is done via a ``get_adapter`` callable supplied at
    construction time.  This keeps the middleware decoupled from any specific
    adapter registry.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        get_adapter: GetAdapterFn | None = None,
    ) -> None:
        self.app = app
        self._get_adapter = get_adapter or _default_get_adapter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        match = _WEBHOOK_ROUTE.match(path)
        if not match:
            await self.app(scope, receive, send)
            return

        platform = match.group("platform")

        # --- Adapter lookup ---
        adapter = await self._get_adapter(platform)
        if adapter is None:
            logger.warning(
                "Webhook received for unconfigured platform: platform=%s, path=%s",
                platform,
                path,
            )
            await _send_json(send, 404, {"error": "Unknown platform"})
            return

        # --- Buffer the raw body BEFORE any parsing ---
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        raw_body = b"".join(body_chunks)

        # Build a Starlette Request backed by the buffered body so
        # adapter.verify_signature() can read headers + body.
        request = _build_request(scope, raw_body)

        # --- Signature verification ---
        try:
            valid = await adapter.verify_signature(request)
        except Exception:
            logger.exception(
                "Signature verification error: platform=%s, path=%s",
                platform,
                path,
            )
            valid = False

        if not valid:
            client = _client_ip(scope)
            logger.warning(
                "Webhook signature verification failed: "
                "platform=%s, client=%s, path=%s",
                platform,
                client,
                path,
            )
            await _send_json(send, 401, {"error": "Invalid signature"})
            return

        logger.debug("Webhook signature verified: platform=%s", platform)

        # --- Cache raw body in scope for downstream handlers ---
        scope["_webhook_raw_body"] = raw_body

        # --- Replay the buffered body for downstream handlers ---
        body_sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _default_get_adapter(platform: str) -> IntegrationAdapter | None:
    """Default adapter lookup — returns None (no adapters registered).

    In production, ``main()`` in server.py supplies a real lookup function
    that queries the DB for active integrations and returns the matching
    adapter instance.
    """
    return None


def _build_request(scope: Scope, body: bytes) -> Request:
    """Create a Starlette Request backed by a pre-buffered body."""

    async def body_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=body_receive)


def _client_ip(scope: Scope) -> str:
    """Extract the client IP from the ASGI scope (best-effort)."""
    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"


async def _send_json(send: Send, status: int, data: dict[str, Any]) -> None:
    """Send a minimal JSON response at the raw ASGI level."""
    body = json.dumps(data).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
