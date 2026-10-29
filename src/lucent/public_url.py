"""Resolve the URL this deployment broadcasts to clients.

Several surfaces hand a person a URL to paste somewhere else: the API-Access
settings page, the VS Code MCP snippet, invite links created by external signup
sites, and the post-registration setup page. All of them need the *public*
address, not whatever reverse proxy happened to pass through as ``Host``.

The rule, in order:

1. ``LUCENT_PUBLIC_URL`` — the deployment's configured public origin. Docker
   Compose passes this from ``.env`` and it is the only value that is correct
   behind a proxy, in a container network, or when the person is not on the same
   machine as the server.
2. The request's own scheme/host — correct for a bare local run (``localhost``)
   and for a direct connection with no configured domain.
3. A loopback fallback — only reached when there is no request at all.

The bug this fixes (2026-10-29): the API-Access page built its URLs from the
request ``Host`` header, so a deployment with ``LUCENT_PUBLIC_URL`` set to the
real domain still displayed ``http://localhost:8766`` to every user.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import Request

#: Last-resort origin when there is no request and no configuration.
_LOCAL_FALLBACK = "http://localhost:8766"


def configured_public_url() -> str | None:
    """The deployment's configured public origin, or ``None`` when unset.

    Trailing slashes are stripped so callers can append a path directly.
    """
    raw = (os.environ.get("LUCENT_PUBLIC_URL") or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        # A bare host in the env file is a common operator mistake; assume https
        # rather than emitting a scheme-less URL that clients cannot parse.
        raw = f"https://{raw}"
    return raw.rstrip("/")


def request_origin(request: Request | None) -> str | None:
    """The origin implied by the request, honoring proxy headers.

    ``X-Forwarded-Proto``/``Host`` are only trusted when uvicorn is configured
    with ``--proxy-headers`` and a trusted peer; when they are present we prefer
    them because otherwise a proxied request reports the internal scheme/host.
    """
    if request is None:
        return None
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme or "http"
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    if not host:
        return None
    return f"{scheme}://{host}"


def public_base_url(request: Request | None = None) -> str:
    """The origin clients should use, configured value first.

    Pass the request when one is available so a bare local run still reports the
    host the person is actually browsing.
    """
    return configured_public_url() or request_origin(request) or _LOCAL_FALLBACK


def mcp_endpoint_url(request: Request | None = None) -> str:
    """The public MCP endpoint (``<origin>/mcp``)."""
    return f"{public_base_url(request)}/mcp"


def api_endpoint_url(request: Request | None = None) -> str:
    """The public REST API root (``<origin>/api``)."""
    return f"{public_base_url(request)}/api"


def is_localhost_origin(url: str | None) -> bool:
    """Whether *url* points at loopback (used to flag a likely-broken setting)."""
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def origin_host(url: str | None) -> str | None:
    """The host[:port] part of *url*, for display next to a full URL."""
    if not url:
        return None
    parts = urlsplit(url if "://" in url else f"//{url}")
    return parts.netloc or None


__all__ = [
    "api_endpoint_url",
    "configured_public_url",
    "is_localhost_origin",
    "mcp_endpoint_url",
    "origin_host",
    "public_base_url",
    "request_origin",
]
