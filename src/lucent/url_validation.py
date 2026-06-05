"""URL validation for SSRF protection.

Validates URLs before making server-side HTTP requests to prevent
Server-Side Request Forgery (SSRF) attacks. Blocks requests to:
- Private IP ranges (RFC 1918: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
- Loopback addresses (127.0.0.0/8, ::1)
- Link-local addresses (169.254.0.0/16, fe80::/10)
- Cloud metadata endpoints (169.254.169.254)
- Non-HTTP(S) schemes

An optional allowlist permits specific hosts that would otherwise be blocked
(e.g., internal MCP servers that legitimately run on private networks).

See: OWASP Finding 13 — SSRF risk in MCP bridge URL
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from lucent.logging import get_logger

logger = get_logger("security.url_validation")


class SSRFError(ValueError):
    """Raised when a URL fails SSRF validation."""


# Networks that must never be contacted via user-supplied URLs.
_BLOCKED_NETWORKS = [
    # RFC 1918 private ranges
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    # Link-local
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # Cloud metadata endpoint (AWS, GCP, Azure IMDS)
    ipaddress.ip_network("169.254.169.254/32"),
    # IPv4-mapped IPv6 loopback
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
]

_ALLOWED_SCHEMES = {"http", "https"}


def _get_allowlist() -> set[str]:
    """Read the URL host allowlist from LUCENT_MCP_URL_ALLOWLIST.

    The env var is a comma-separated list of hostnames or IPs that are
    permitted even when they resolve to private addresses.  Entries are
    lowered and stripped.

    Example::

        LUCENT_MCP_URL_ALLOWLIST=internal-mcp.corp.local,10.0.1.5
    """
    raw = os.environ.get("LUCENT_MCP_URL_ALLOWLIST", "")
    if not raw.strip():
        return set()
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def _is_ip_blocked(ip_str: str) -> bool:
    """Return True if *ip_str* falls within any blocked network."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # If we can't parse it, block it defensively.
        return True
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_url(url: str, *, purpose: str = "MCP server") -> str:
    """Validate a URL for safe server-side use.

    Checks:
    1. Scheme is http or https.
    2. Hostname is present and resolvable.
    3. Resolved IP is not in a blocked (private/link-local/loopback) range
       — *unless* the hostname appears in the ``LUCENT_MCP_URL_ALLOWLIST``.

    Args:
        url: The URL to validate.
        purpose: Human-readable label for log messages (e.g. "MCP server").

    Returns:
        The validated URL (unchanged).

    Raises:
        SSRFError: When the URL fails any check.
    """
    if not url or not isinstance(url, str):
        raise SSRFError(f"Empty or invalid URL for {purpose}")

    parsed = urlparse(url)

    # --- scheme ---
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(
            f"Invalid URL scheme '{parsed.scheme}' for {purpose}. "
            f"Only {sorted(_ALLOWED_SCHEMES)} are allowed."
        )

    # --- hostname ---
    hostname = parsed.hostname
    if not hostname:
        raise SSRFError(f"Missing hostname in URL for {purpose}: {url}")

    # Check allowlist *before* DNS resolution — if the host is explicitly
    # permitted the operator has accepted the risk.
    allowlist = _get_allowlist()
    if hostname.lower() in allowlist:
        logger.debug(
            "URL host %s is in allowlist, skipping SSRF checks for %s",
            hostname,
            purpose,
        )
        return url

    # --- DNS resolution & IP check ---
    try:
        # Resolve ALL addresses (IPv4 + IPv6) and check each.
        infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFError(
            f"Cannot resolve hostname '{hostname}' for {purpose}: {exc}"
        ) from exc

    if not infos:
        raise SSRFError(f"No DNS results for hostname '{hostname}' for {purpose}")

    for family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        if _is_ip_blocked(ip_str):
            raise SSRFError(
                f"URL for {purpose} resolves to blocked address {ip_str} "
                f"(hostname: {hostname}). Private, loopback, and link-local "
                f"addresses are not permitted. To allow this host, add it to "
                f"LUCENT_MCP_URL_ALLOWLIST."
            )

    return url
