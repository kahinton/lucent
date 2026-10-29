"""Outbound HTTP policy for server-executed tool runtimes.

`python` tools run inside a container, so the sandbox backend enforces their
network policy. `http`, `openapi`, and `builtin` tools execute **in the server
process**, which means the server is the only thing standing between a
model-supplied argument and an arbitrary internal address. This module is that
boundary, and every non-sandbox runtime must go through it.

The rules mirror how Lucent already treats MCP endpoints:

1. Every URL — including **each redirect hop** — is validated with
   :func:`lucent.url_validation.validate_url`, which blocks loopback, RFC 1918,
   link-local, and cloud-metadata ranges unless the operator has explicitly
   allowlisted the host deployment-wide (``LUCENT_MCP_URL_ALLOWLIST``).
2. The tool's own ``network_policy.allowed_hosts`` is an **additional**
   restriction, never a bypass. A tool that lists ``example.com`` does not also
   gain access to ``10.0.0.5`` merely because an operator allowlisted a private
   host for MCP. Both must agree.
3. Declarative tools are pinned to their configured scheme; a redirect may not
   downgrade HTTPS to HTTP, and cross-origin redirects drop the caller's
   credential headers so an approved host cannot harvest another host's token.
4. Request/response bodies are bounded so a hostile endpoint cannot exhaust
   server memory or flood the model context.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from lucent.logging import get_logger
from lucent.url_validation import SSRFError, validate_url

logger = get_logger("services.tool_runtimes.http_policy")

__all__ = [
    "HTTPPolicyError",
    "OutboundPolicy",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_REDIRECTS",
    "redact_url",
]

# A tool request body should describe an operation, not upload a file.
MAX_REQUEST_BYTES = 1_000_000
# Responses are fed to a model; keep them small enough to be usable.
# Note this is a hard read cap, not the model-facing truncation limit.
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class HTTPPolicyError(Exception):
    """Raised when an outbound request violates configured policy."""


#: Ports a declarative tool may reach when its policy does not name a port.
#: Restricting to the HTTP(S) ports stops `network_mode='bridge'` from becoming
#: "reach Redis/SSH/SMTP on any public address".
_DEFAULT_PORTS_BY_SCHEME = {"http": 80, "https": 443}
_ALLOWED_DEFAULT_PORTS = frozenset(_DEFAULT_PORTS_BY_SCHEME.values())


@dataclass(frozen=True)
class OutboundPolicy:
    """The resolved network policy for one tool invocation.

    ``network_mode``:
        * ``none`` — no outbound traffic at all (the default, and the safe one).
        * ``allowlist`` — only ``allowed_hosts``.
        * ``bridge`` — any host that passes SSRF validation, on the standard
          HTTP(S) ports.
    """

    network_mode: str = "none"
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_tool(cls, tool: dict[str, Any]) -> OutboundPolicy:
        raw = tool.get("network_policy") or {}
        if not isinstance(raw, dict):
            raise HTTPPolicyError("network_policy must be a JSON object")
        mode = str(raw.get("network_mode") or "none").strip().lower()
        if mode not in {"none", "allowlist", "bridge"}:
            raise HTTPPolicyError(
                f"Unsupported network_mode {mode!r}; expected none, allowlist, or bridge"
            )
        hosts = raw.get("allowed_hosts") or []
        if not isinstance(hosts, list):
            raise HTTPPolicyError("network_policy.allowed_hosts must be a JSON array")
        normalized: list[str] = []
        for entry in hosts:
            if not isinstance(entry, str):
                raise HTTPPolicyError("network_policy.allowed_hosts entries must be strings")
            value = entry.strip().lower()
            if value:
                normalized.append(value)
        return cls(
            network_mode=mode,
            allowed_hosts=tuple(dict.fromkeys(normalized)),
        )

    def assert_host_allowed(self, url: str) -> None:
        """Enforce ``network_mode``, the host allowlist, and the port for *url*."""
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise HTTPPolicyError(
                f"URL scheme {parsed.scheme!r} is not permitted; use http or https"
            )
        host = _normalize_host(parsed.hostname or "")
        if not host:
            raise HTTPPolicyError("URL has no hostname")
        port = _effective_port(parsed)

        if self.network_mode == "none":
            raise HTTPPolicyError(
                "This tool has no outbound network access (network_policy.network_mode "
                "is 'none'). An owner must set 'allowlist' and list the hosts it needs."
            )

        if self.network_mode == "allowlist":
            if not self.allowed_hosts:
                raise HTTPPolicyError(
                    "network_mode is 'allowlist' but no allowed_hosts are configured"
                )
            if not self._host_matches(host, port):
                shown = f"{host}:{port}" if port else host
                raise HTTPPolicyError(
                    f"Host {shown!r} is not in this tool's allowed_hosts"
                )
            return

        # network_mode == "bridge" still falls through to SSRF validation, so
        # "any host" means "any public host". Keep it off arbitrary ports so a
        # bridge tool cannot speak Redis or SSH to a public address.
        if port is not None and port not in _ALLOWED_DEFAULT_PORTS:
            raise HTTPPolicyError(
                f"Port {port} is not permitted for a bridge-mode tool; only 80 and 443 "
                "are reachable. Use network_mode='allowlist' with an explicit host:port "
                "entry to reach another port."
            )

    def _host_matches(self, host: str, port: int | None) -> bool:
        for entry in self.allowed_hosts:
            allowed_host, allowed_port = _split_host_port(entry)
            allowed_host = _normalize_host(allowed_host)
            if not allowed_host:
                continue
            if allowed_host != host:
                # Support a leading-dot suffix form for subdomains: ".example.com"
                if not (allowed_host.startswith(".") and host.endswith(allowed_host)):
                    continue
            if allowed_port is not None and allowed_port != port:
                continue
            if allowed_port is None and port not in _ALLOWED_DEFAULT_PORTS:
                # A bare `api.example.com` entry authorises the web ports, not
                # every port on that host. Otherwise "this tool may call
                # api.example.com" would also permit api.example.com:6379, and an
                # allowlisted API host that happens to share a machine with Redis
                # becomes an SSRF pivot to it.
                continue
            return True
        return False

    def allows_host(self, host: str) -> bool:
        """Whether the allowlist names *host*, ignoring ports.

        Used by the built-in catalog to prove a network-capable handler's hosts
        are declared, without having to know which port the handler will use.
        """
        normalized = _normalize_host(host)
        if not normalized:
            return False
        for entry in self.allowed_hosts:
            allowed_host, _ = _split_host_port(entry)
            allowed_host = _normalize_host(allowed_host)
            if not allowed_host:
                continue
            if allowed_host == normalized:
                return True
            if allowed_host.startswith(".") and normalized.endswith(allowed_host):
                return True
        return False

    def validate_destination(self, url: str, *, purpose: str) -> None:
        """Apply both the tool allowlist and deployment SSRF protection."""
        self.assert_host_allowed(url)
        try:
            validate_url(url, purpose=purpose)
        except SSRFError as exc:
            raise HTTPPolicyError(str(exc)) from exc

    def allows_credentials_for(self, original_url: str, next_url: str) -> bool:
        """May caller-supplied credential headers survive a redirect?

        Only same-origin redirects keep them. Anything else would let a
        compromised or malicious endpoint on the allowlist redirect to a peer
        and collect the credential the user attached for the first host.
        """
        return _origin(original_url) == _origin(next_url)


def _split_host_port(entry: str) -> tuple[str, int | None]:
    """Split ``host`` / ``host:port`` / ``[v6]`` / ``[v6]:port`` allowlist entries."""
    value = entry.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if value.count(":") == 1:
        host, _, maybe_port = value.partition(":")
        if maybe_port.isdigit():
            return host, int(maybe_port)
    return value, None


def _normalize_host(host: str) -> str:
    """Lower-case, strip a trailing FQDN dot, and IDNA-encode *host*.

    Comparison must happen on one canonical spelling, otherwise ``example.com.``
    never matches ``example.com`` and a punycode entry never matches its Unicode
    spelling. A leading dot (the subdomain wildcard form) is preserved.
    """
    value = (host or "").strip().lower()
    if value.startswith("."):
        return "." + _normalize_host(value[1:])
    value = value.rstrip(".")
    if not value:
        return ""
    try:
        # Bare IPs must not be IDNA-encoded; encode("idna") rejects them anyway.
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    try:
        return value.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return value


def _effective_port(parsed: Any) -> int | None:
    """The port a URL will actually connect to, so :443 and *no port* agree."""
    if parsed.port is not None:
        return parsed.port
    scheme = (parsed.scheme or "").lower()
    return _DEFAULT_PORTS_BY_SCHEME.get(scheme)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    return scheme, _normalize_host(parsed.hostname or ""), _effective_port(parsed)


def redact_url(url: str) -> str:
    """Return ``scheme://host/path`` with the query and fragment removed.

    Query strings are the common carrier for a `{{env:TOKEN}}` substitution, and
    this function's output is what gets written to the run ledger and handed back
    to the model. Redacting here is what keeps a credential out of both.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "(unparseable URL)"
    if not parsed.scheme:
        return "(unparseable URL)"
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path or ''}"
