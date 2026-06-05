"""ASGI middleware for webhook signature verification.

Verifies inbound webhook signatures BEFORE any request body parsing,
per the integration design principle. Only applies to webhook routes
matching ``/integrations/webhook/{provider}``.

Invalid signatures are rejected with 401 and an audit-level log entry.
Valid requests have their raw body buffered and replayed for downstream
handlers so the body stream is not consumed.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from lucent.integrations.base import IntegrationAdapter
from lucent.logging import get_logger

logger = get_logger("integrations.middleware")

# Matches /integrations/webhook/{provider} with optional trailing slash
_WEBHOOK_ROUTE = re.compile(r"^/integrations/webhook/(?P<provider>[a-z][a-z0-9_-]*)/?$")

GetAdapterFn = Callable[[str], Awaitable[IntegrationAdapter | None]]


class SignatureVerificationMiddleware:
    """Pure ASGI middleware that verifies webhook signatures before body parsing.

    Uses pure ASGI (not BaseHTTPMiddleware) to avoid thread-pool dispatch that
    would break ContextVar propagation — same rationale as MCPAuthMiddleware.

    Adapter lookup is done via a ``get_adapter`` callable supplied at
    construction time. This keeps the middleware decoupled from any specific
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

        provider = match.group("provider")

        # --- Adapter lookup ---
        adapter = await self._get_adapter(provider)
        if adapter is None:
            logger.warning(
                "Webhook received for unknown provider: provider=%s, path=%s",
                provider,
                path,
            )
            await _send_json(scope, send, 404, {"error": "Unknown provider"})
            return

        # --- Buffer the raw body before any parsing ---
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        raw_body = b"".join(body_chunks)

        # Build a Starlette Request with the buffered body so
        # adapter.verify_signature() can read headers + body.
        request = _build_request(scope, raw_body)

        # --- Signature verification ---
        start = time.monotonic()
        try:
            valid = await adapter.verify_signature(request)
        except Exception:
            logger.exception(
                "Signature verification error: provider=%s, path=%s",
                provider,
                path,
            )
            valid = False
        elapsed_ms = (time.monotonic() - start) * 1000

        if not valid:
            client = _client_ip(scope)
            logger.warning(
                "Webhook signature verification failed: "
                "provider=%s, client=%s, path=%s, elapsed_ms=%.1f",
                provider,
                client,
                path,
                elapsed_ms,
            )
            await _send_json(scope, send, 401, {"error": "Invalid signature"})
            return

        logger.debug(
            "Webhook signature verified: provider=%s, elapsed_ms=%.1f",
            provider,
            elapsed_ms,
        )

        # --- Replay the buffered body for downstream handlers ---
        body_sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            # After the body has been replayed, yield disconnect if asked again
            return await receive()

        await self.app(scope, replay_receive, send)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _default_get_adapter(provider: str) -> IntegrationAdapter | None:
    """Default adapter lookup — returns None (no adapters registered yet).

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


async def _send_json(
    scope: Scope,
    send: Send,
    status: int,
    data: dict[str, Any],
) -> None:
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
