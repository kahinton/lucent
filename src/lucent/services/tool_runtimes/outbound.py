"""Shared outbound request execution for declarative runtimes.

`http` and `openapi` differ only in *how* the request is described; the rules
governing the request that actually leaves the process are identical. Both build
a :class:`OutboundRequest` and hand it to :func:`send`, so there is exactly one
place where redirects are followed, host policy is applied, credentials are
attached or withheld, and bodies are bounded.

Security properties this function is responsible for:

* every hop is checked against the tool's host allowlist *and* SSRF protection;
* HTTPS may not be downgraded to HTTP by a redirect;
* credential headers are dropped when a redirect leaves the original origin;
* the request body and the response body are both capped;
* timeouts are absolute, not per-hop, so a slow-drip endpoint cannot occupy a
  worker indefinitely.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from lucent.logging import get_logger
from lucent.services.tool_runtimes.http_policy import (
    MAX_REDIRECTS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    HTTPPolicyError,
    OutboundPolicy,
    _origin,
    redact_url,
)

logger = get_logger("services.tool_runtimes.outbound")

__all__ = [
    "CREDENTIAL_HEADERS",
    "DEFAULT_TIMEOUT_SECONDS",
    "OutboundRequest",
    "OutboundResponse",
    "credential_header_names",
    "send",
]

DEFAULT_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 120

# Headers the runtime owns. A tool author may not set these because doing so
# would change how the request is framed rather than what it says.
_RESERVED_HEADERS = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "trailer",
})

# Headers that carry a caller credential and must not survive a cross-origin
# redirect. Public because every non-sandbox runtime needs the same set, and a
# second copy is a second thing to forget to update.
CREDENTIAL_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-access-token",
    "x-client-secret",
})

# Statuses whose semantics require following a `Location`. 300 and 304 are
# *not* redirects in any useful sense: 304 has no body by definition and 300 is
# a client choice, so following them silently would turn a cache probe into a
# second request.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})



@dataclass
class OutboundRequest:
    """A fully rendered request awaiting policy checks and transmission."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any = None
    form_body: dict[str, str] | None = None
    raw_body: bytes | None = None
    content_type: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    #: Header names that carry a caller credential; dropped when a redirect
    #: leaves the origin that credential was configured for.
    credential_headers: frozenset[str] = CREDENTIAL_HEADERS


@dataclass
class OutboundResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]
    final_url: str
    duration_ms: int
    truncated: bool = False

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8", errors="replace"))

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _validate_reserved_headers(headers: dict[str, str]) -> None:
    for name in headers:
        if name.strip().lower() in _RESERVED_HEADERS:
            raise HTTPPolicyError(
                f"Header {name!r} is reserved; the runtime sets it automatically"
            )


def credential_header_names(
    rendered_headers: dict[str, str],
    header_templates: Any,
    declared: Any = None,
) -> frozenset[str]:
    """Which of *rendered_headers* must be dropped on a cross-origin redirect.

    A header counts as credential-bearing when any of these hold:

    * it is in :data:`CREDENTIAL_HEADERS`;
    * the tool author listed it in ``credential_headers``;
    * its template referenced a credential (``{{env:...}}``);
    * its name conventionally carries a token (``token``/``key``/``auth``/``secret``).

    Every non-sandbox runtime calls this so the three of them cannot drift into
    three different answers for the same security question.
    """
    names = {name.lower() for name in CREDENTIAL_HEADERS}
    if isinstance(declared, str):
        # Tolerate a comma-separated string, which is what a plain HTML input
        # produces; silently dropping the author's intent is the worse outcome.
        declared = [part for part in declared.split(",") if part.strip()]
    if isinstance(declared, (list, tuple, set, frozenset)):
        names.update(str(name).strip().lower() for name in declared if str(name).strip())

    templates = header_templates if isinstance(header_templates, dict) else {}
    template_by_lower = {str(key).lower(): value for key, value in templates.items()}
    for name in rendered_headers:
        lowered = name.lower()
        template = template_by_lower.get(lowered)
        if isinstance(template, str) and "{{env:" in template:
            names.add(lowered)
        elif any(token in lowered for token in ("token", "key", "auth", "secret")):
            names.add(lowered)
    return frozenset(names)


def _encode_body(request: OutboundRequest) -> tuple[bytes | None, str | None]:
    """Return (payload, content_type) for the request body."""
    if request.raw_body is not None:
        if len(request.raw_body) > MAX_REQUEST_BYTES:
            raise HTTPPolicyError(
                f"Request body exceeds the {MAX_REQUEST_BYTES} byte limit"
            )
        return request.raw_body, request.content_type
    if request.form_body is not None:
        from urllib.parse import urlencode

        encoded = urlencode(request.form_body, doseq=True).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise HTTPPolicyError(
                f"Request body exceeds the {MAX_REQUEST_BYTES} byte limit"
            )
        return encoded, request.content_type or "application/x-www-form-urlencoded"
    if request.json_body is not None:
        encoded = json.dumps(request.json_body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise HTTPPolicyError(
                f"Request body exceeds the {MAX_REQUEST_BYTES} byte limit"
            )
        return encoded, request.content_type or "application/json"
    return None, None


async def _read_capped(response: Any) -> tuple[bytes, bool]:
    """Read at most ``MAX_RESPONSE_BYTES`` from a *streamed* response.

    The caller must open the response with ``client.stream(...)``. Reading a
    buffered response here would bound only the copy, not the process memory the
    upstream had already consumed.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        remaining = MAX_RESPONSE_BYTES - total
        if remaining <= 0:
            return b"".join(chunks), True
        chunks.append(chunk[:remaining])
        total += len(chunk[:remaining])
    return b"".join(chunks), False


async def send(
    request: OutboundRequest,
    *,
    policy: OutboundPolicy,
    purpose: str = "tool request",
) -> OutboundResponse:
    """Send a rendered request under ``policy``. Raises on policy violation."""
    import httpx

    method = str(request.method or "GET").strip().upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise HTTPPolicyError(f"Unsupported HTTP method {method!r}")

    timeout = _bounded_timeout(request.timeout_seconds)
    _validate_reserved_headers(request.headers)

    payload, content_type = _encode_body(request)
    base_headers = dict(request.headers)
    if content_type and not any(k.lower() == "content-type" for k in base_headers):
        base_headers["Content-Type"] = content_type
    base_headers.setdefault("User-Agent", "Lucent-ToolRuntime/1.0")

    origin = _origin(request.url)
    current_url = request.url
    current_method = method
    current_payload = payload
    # Per-hop header state, mutated across redirects. Deriving each hop from the
    # immutable ``base_headers`` would resurrect a credential (or a Content-Type)
    # that a previous hop deliberately dropped.
    current_headers = dict(base_headers)

    deadline = time.monotonic() + timeout
    started = time.monotonic()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        # Declarative tools validate the destination themselves; httpx must not
        # consult proxy env vars and silently route through an unvalidated hop.
        trust_env=False,
        # A shared cookie jar turns any `Set-Cookie` from hop 1 into a credential
        # that httpx re-attaches to hop 2, defeating the cross-origin header drop.
        cookies=None,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPPolicyError(
                    f"{purpose} timed out after {timeout}s before completing"
                )

            policy.validate_destination(current_url, purpose=purpose)

            try:
                async with client.stream(
                    current_method,
                    current_url,
                    headers=current_headers,
                    content=current_payload,
                    timeout=httpx.Timeout(remaining),
                ) as response:
                    if (
                        response.status_code in _REDIRECT_STATUSES
                        and response.headers.get("location")
                    ):
                        next_url = str(
                            httpx.URL(current_url).join(response.headers["location"])
                        )
                        # Refuse an HTTPS -> HTTP downgrade: a redirect must not be
                        # able to strip transport security from a request the user
                        # configured as TLS.
                        if (
                            _origin(current_url)[0] == "https"
                            and _origin(next_url)[0] != "https"
                        ):
                            raise HTTPPolicyError(
                                f"{purpose} refused an HTTPS->HTTP redirect to "
                                f"{redact_url(next_url)!r}"
                            )

                        if _origin(next_url) == origin:
                            next_headers = dict(current_headers)
                        else:
                            next_headers = {
                                name: value
                                for name, value in current_headers.items()
                                if name.lower() not in request.credential_headers
                            }

                        # 303, and 301/302 on POST, change the method to GET and
                        # drop the body — matching browser and httpx defaults so an
                        # upstream that redirects to a signed URL behaves as its
                        # author expects.
                        if response.status_code == 303 or (
                            response.status_code in (301, 302)
                            and current_method == "POST"
                        ):
                            current_method = "GET"
                            current_payload = None
                            next_headers = {
                                name: value
                                for name, value in next_headers.items()
                                if name.lower() != "content-type"
                            }
                        await response.aclose()
                        current_headers = next_headers
                        current_url = next_url
                        continue

                    body, truncated = await _read_capped(response)
                    return OutboundResponse(
                        status_code=response.status_code,
                        content=body,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        final_url=current_url,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        truncated=truncated,
                    )
            except httpx.HTTPError as exc:
                # Redact before it reaches the model or the run ledger: the query
                # string is where a credential substitution commonly lands.
                raise HTTPPolicyError(
                    f"{purpose} failed for {redact_url(current_url)}: "
                    f"{type(exc).__name__}"
                ) from exc

    raise HTTPPolicyError(f"{purpose} exceeded {MAX_REDIRECTS} redirects")


def _bounded_timeout(value: Any) -> int:
    """Validate a timeout, refusing absurd values instead of silently clamping."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise HTTPPolicyError("timeout_seconds must be an integer") from None
    if seconds < 1 or seconds > _MAX_TIMEOUT_SECONDS:
        raise HTTPPolicyError(
            f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}; got {seconds}"
        )
    return seconds
