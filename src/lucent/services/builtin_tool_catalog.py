"""Lucent-native tool handlers: a reviewed, no-install tool catalog.

These are the tools a new deployment can offer immediately. They are
**in-process, no-sandbox, no-container** handlers with fixed, reviewed
implementations — the equivalent of the "provider-native" tools Copilot, Claude
Code and Gemini ship. A model can never define one; an owner can only *select*
one by name, and the schema comes from this module rather than from user input.

Two tiers, because they have different supply-chain stories:

**Core (always available)** — implemented with the Python standard library plus
``httpx``, both already base dependencies. Nothing is installed at build time, so
enabling them cannot affect the image.

**Optional (build-flagged)** — needs a third-party package. These are registered
only when the corresponding ``LUCENT_TOOLS_ENABLE_*`` environment variable was set
at build time *and* the package actually imports. A missing package is reported as
"unavailable" rather than raising, so a deployment that did not opt in still has a
clean catalog instead of a broken one.

Every handler receives already-validated arguments and the resolved credential
map. Handlers must never accept a URL or path they were not designed for: outbound
network access is confined by :mod:`lucent.services.tool_runtimes.http_policy`, and
anything that reads bytes from the caller-supplied text does so in memory only.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from lucent.logging import get_logger
from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    ToolRuntimeBlockedError,
    ToolRuntimeError,
)
from lucent.services.tool_runtimes.http_policy import (
    HTTPPolicyError,
    OutboundPolicy,
    redact_url,
)
from lucent.services.tool_runtimes.outbound import OutboundRequest, send

logger = get_logger("services.builtin_tool_catalog")

__all__ = [
    "BUILTIN_TOOL_CATALOG",
    "BuiltinToolSpec",
    "UnavailableBuiltinToolError",
    "available_builtin_tools",
    "get_builtin_tool",
    "invoke_builtin_tool",
]

_MAX_TEXT_CHARS = 2_000_000


def _require_import(module: str, package: str) -> Any:
    """Import an optional dependency, raising a clear availability error."""
    try:
        return __import__(module, fromlist=["_"])
    except Exception as exc:  # noqa: BLE001 — any import failure means unavailable
        raise UnavailableBuiltinToolError(
            f"This tool needs the '{package}' package, which is not installed in this "
            f"image. Rebuild with the matching LUCENT_TOOLS_ENABLE_* build argument to "
            f"enable it."
        ) from exc


class UnavailableBuiltinToolError(ToolRuntimeError):
    """The handler exists but its optional dependency is absent."""


@dataclass(frozen=True)
class BuiltinToolSpec:
    """One native handler, as advertised to owners and models."""

    name: str
    label: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[RuntimeExecutionContext], Awaitable[Any]]
    #: ``None`` for core tools; otherwise the pip package the build must include.
    requires_package: str | None = None
    #: Build environment variable that turns the optional tier on.
    build_flag: str | None = None
    #: Default ``runtime_config`` values, e.g. a curated host allowlist.
    default_config: dict[str, Any] = field(default_factory=dict)
    #: True when the handler performs outbound network I/O.
    uses_network: bool = False
    #: Domains the tool contacts; used to seed a sane network policy.
    network_hosts: tuple[str, ...] = ()

    def descriptor(self, *, available: bool = True, reason: str | None = None) -> dict[str, Any]:
        data = {
            "handler": self.name,
            "label": self.label,
            "description": self.description,
            "input_schema": self.input_schema,
            "requires_package": self.requires_package,
            "build_flag": self.build_flag,
            "uses_network": self.uses_network,
            "network_hosts": list(self.network_hosts),
            "default_config": dict(self.default_config),
            "available": available,
        }
        if reason:
            data["unavailable_reason"] = reason
        return data


# ── handlers: pure computation ────────────────────────────────────────────


_SAFE_BINARY = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mult": lambda a, b: a * b,
    "div": lambda a, b: a / b,
    "floordiv": lambda a, b: a // b,
    "mod": lambda a, b: a % b,
    "pow": lambda a, b: a**b,
}
_SAFE_UNARY = {
    "usub": lambda a: -a,
    "uadd": lambda a: +a,
}
_SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
_SAFE_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _split_path(path: str) -> list[tuple[str | None, int | None]]:
    """Split ``a.b[0].c`` into ``[(a,None), (b,0), (c,None)]``.

    Strict on purpose: a path the reader cannot fully parse must be an error, not
    a prefix. ``a[-1]`` and ``a[0`` used to silently degrade to ``a`` and return a
    wrong value with ``found: true``.
    """
    segments: list[tuple[str | None, int | None]] = []
    for part in path.split("."):
        if not part:
            continue
        key_match = re.fullmatch(r"([^.\[\]]+)", part)
        if key_match:
            segments.append((key_match.group(1), None))
            continue
        # One or more trailing indices, optionally preceded by a key.
        index_only = re.fullmatch(r"(?:\[(\d+)\])+", part)
        if index_only:
            for index in re.findall(r"\[(\d+)\]", part):
                segments.append((None, int(index)))
            continue
        keyed = re.fullmatch(r"([^.\[\]]+)((?:\[\d+\])+)", part)
        if keyed:
            segments.append((keyed.group(1), None))
            for index in re.findall(r"\[(\d+)\]", keyed.group(2)):
                segments.append((None, int(index)))
            continue
        raise ToolRuntimeError(
            f"Could not parse path segment {part!r}; use keys and non-negative "
            "indices such as items[0].price"
        )
    if not segments:
        raise ToolRuntimeError(f"Could not parse path {path!r}")
    return segments


_QUANTIFIER_TAIL = re.compile(r"[+*?]|\{\d+,?\d*\}")
_QUANTIFIER_ANYWHERE = re.compile(r"[+*]|\{\d+,?\d*\}")


def _matching_open_paren(pattern: str, close_index: int) -> int | None:
    """Index of the ``(`` matching the ``)`` at *close_index*, ignoring escapes."""
    depth = 0
    index = close_index
    while index >= 0:
        char = pattern[index]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                # An escaped paren is a literal, not a group.
                backslashes = 0
                cursor = index - 1
                while cursor >= 0 and pattern[cursor] == "\\":
                    backslashes += 1
                    cursor -= 1
                if backslashes % 2 == 0:
                    return index
        index -= 1
    return None


def _assert_pattern_is_safe(pattern: str) -> None:
    """Reject the classic catastrophic-backtracking shapes up front.

    A timeout is not a real defence here. CPython's ``re`` engine is C code that
    holds the GIL while backtracking, so a runaway match cannot be cancelled: not
    by ``asyncio.wait_for`` on a ``to_thread`` task (the thread keeps running and
    the event loop starves), and not by a signal handler in the same process. The
    only reliable controls are to make the input small and to refuse the patterns
    whose worst case is exponential.

    The shapes below are what actually blow up — a quantified group whose body is
    itself unbounded, or whose alternatives differ in length:

    * ``(a+)+``  — nested quantifier over a character run
    * ``(a*)*``  — same, with a star
    * ``(a|aa)+`` — ambiguous alternatives
    * ``(.*)*``  — nested wildcard

    ``(abc)+`` and ``(a|b)+`` remain allowed: their bodies are a fixed-width
    literal and equally-sized single-character alternatives, so no partition of
    the input can match two different ways.

    This is a conservative static check, not a proof. A pathological pattern that
    slips past it is still bounded by the 2,000,000-character input cap above and
    the 2,000-character pattern cap; the residual risk is a slow call, not an
    unbounded one.
    """
    for match in re.finditer(r"\)", pattern):
        close = match.start()
        if not _QUANTIFIER_TAIL.match(pattern, close + 1):
            continue
        open_index = _matching_open_paren(pattern, close)
        if open_index is None:
            continue
        body = pattern[open_index + 1 : close]
        if not body or body.startswith("?"):
            # `(?:...)`, `(?P<n>...)`, lookarounds — the group prefix is syntax,
            # not a matchable atom. Strip it before inspecting the body.
            body = re.sub(r"^\?[P<>=!]*[A-Za-z_]*[:>]?", "", body)
        reason = None
        if _QUANTIFIER_ANYWHERE.search(body):
            reason = "a group containing a quantifier is itself quantified"
        elif "|" in body:
            alternatives = [alt for alt in body.split("|")]
            widths = {_literal_width(alt) for alt in alternatives}
            if None in widths or len(widths) > 1 or max(widths, default=0) > 1:
                reason = "a group with ambiguous alternatives is quantified"
        if reason:
            raise ToolRuntimeError(
                f"Pattern rejected: {reason} in {pattern[open_index:close + 1]!r}. "
                "This shape can take exponential time to evaluate (catastrophic "
                "backtracking). Rewrite it with a single quantifier, or match the "
                "text in smaller pieces."
            )


def _literal_width(alternative: str) -> int | None:
    """Fixed width of a plain-literal alternative, or ``None`` if not literal."""
    if not alternative:
        return 0
    width = 0
    for char in alternative:
        if char in ".^$*+?{}[]()|\\":
            return None
        width += 1
    return width


_UNIT_TABLE: dict[str, tuple[str, float]] = {
    # length (base: metre)
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0),
    "km": ("length", 1000.0), "in": ("length", 0.0254), "ft": ("length", 0.3048),
    "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    # mass (base: kilogram)
    "mg": ("mass", 1e-6), "g": ("mass", 0.001), "kg": ("mass", 1.0),
    "oz": ("mass", 0.028349523125), "lb": ("mass", 0.45359237),
    "t": ("mass", 1000.0),
    # volume (base: litre)
    "ml": ("volume", 0.001), "l": ("volume", 1.0), "gal": ("volume", 3.785411784),
    "qt": ("volume", 0.946352946), "pt": ("volume", 0.473176473),
    "cup": ("volume", 0.2365882365), "floz": ("volume", 0.0295735295625),
    # temperature handled separately
    "c": ("temperature", 1.0), "f": ("temperature", 1.0), "k": ("temperature", 1.0),
    # data (base: byte)
    "b": ("data", 1.0), "kb": ("data", 1024.0), "mb": ("data", 1024.0**2),
    "gb": ("data", 1024.0**3), "tb": ("data", 1024.0**4),
    # time (base: second)
    "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0),
    "day": ("time", 86400.0), "week": ("time", 604800.0),
}


_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (compatible; Lucent/1.0; +https://github.com/kahinton/lucent)"


async def _web_search(context: RuntimeExecutionContext) -> Any:
    """Keyword web search with no API key required.

    Uses DuckDuckGo's HTML endpoint by default. A deployment that sets
    ``LUCENT_SEARCH_ENDPOINT`` + ``LUCENT_SEARCH_API_KEY`` is routed through its
    own provider instead, and the key is injected as a header so it never appears
    in the model-visible result.
    """
    from urllib.parse import urlparse

    import httpx

    args = context.arguments
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolRuntimeError("query is required")
    if len(query) > 500:
        raise ToolRuntimeError("query is too long (limit 500 characters)")
    try:
        max_results = max(1, min(int(args.get("max_results", 5) or 5), 10))
    except (TypeError, ValueError):
        max_results = 5

    custom_endpoint = (os.environ.get("LUCENT_SEARCH_ENDPOINT") or "").strip()
    custom_key = (os.environ.get("LUCENT_SEARCH_API_KEY") or "").strip()
    endpoint = custom_endpoint or _DDG_ENDPOINT
    host = urlparse(endpoint).hostname or "html.duckduckgo.com"
    # The definition's policy governs first; the search endpoint is then checked
    # against it so a narrow allowlist cannot be bypassed by the env default.
    base_policy = _context_policy(context)
    if base_policy.network_mode == "none":
        raise ToolRuntimeBlockedError(
            "web_search needs outbound network access; set network_policy to "
            "'allowlist' with html.duckduckgo.com, or to 'bridge'."
        )
    if base_policy.network_mode == "allowlist" and not base_policy.allows_host(host):
        raise ToolRuntimeBlockedError(
            f"network_policy.allowed_hosts does not include the search host {host!r}"
        )
    policy = base_policy

    if custom_endpoint and custom_key:
        sep = "&" if "?" in endpoint else "?"
        request = OutboundRequest(
            method="GET",
            url=f"{endpoint}{sep}{httpx.QueryParams({'q': query})}",
            headers={"Authorization": f"Bearer {custom_key}", "User-Agent": _UA},
            timeout_seconds=20,
        )
        response = await send(request, policy=policy, purpose="web search")
        if response.status_code >= 400:
            raise ToolRuntimeError(f"Search provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except Exception:
            # Fall through to HTML parsing: the provider may be DDG-compatible.
            payload = None
        if isinstance(payload, dict):
            results = _normalize_provider_results(payload, max_results)
            if results:
                return {"query": query, "provider": "configured", "results": results}
        text = response.text()
    else:
        request = OutboundRequest(
            method="POST",
            url=endpoint,
            headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"},
            form_body={"q": query},
            timeout_seconds=20,
        )
        response = await send(request, policy=policy, purpose="web search")
        if response.status_code >= 400:
            raise ToolRuntimeError(f"Search endpoint returned HTTP {response.status_code}")
        text = response.text()

    results = _parse_ddg_html(text, max_results)
    if not results:
        return {
            "query": query,
            "provider": "duckduckgo",
            "results": [],
            "note": (
                "The search endpoint returned no parseable results. It may be rate "
                "limiting this deployment, or the result markup changed."
            ),
        }
    return {"query": query, "provider": "duckduckgo", "results": results}


def _parse_ddg_html(text: str, max_results: int) -> list[dict[str, str]]:
    import html as html_lib
    from urllib.parse import parse_qs, unquote, urlparse

    results: list[dict[str, str]] = []
    # Each result anchor carries the target in a `uddg` query parameter and the
    # title in the anchor text; the snippet follows in a result__snippet block.
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'(?:.*?class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>)?',
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        href = match.group("href")
        if href.startswith("//"):
            href = f"https:{href}"
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc:
            target = parse_qs(parsed.query).get("uddg")
            if target:
                href = unquote(target[0])
        title = re.sub(r"<[^>]+>", "", match.group("title") or "")
        snippet = re.sub(r"<[^>]+>", "", match.group("snippet") or "")
        results.append(
            {
                "title": html_lib.unescape(title).strip(),
                "url": href,
                "snippet": html_lib.unescape(snippet).strip(),
            }
        )
        if len(results) >= max_results:
            break
    return results


def _normalize_provider_results(payload: dict[str, Any], max_results: int) -> list[dict[str, str]]:
    """Accept the common ``{"results": [{title,url,description}]}`` shape."""
    raw = payload.get("results") or payload.get("organic_results") or payload.get("web")
    if not isinstance(raw, list):
        return []
    results: list[dict[str, str]] = []
    for item in raw[:max_results]:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or item.get("href")
        if not isinstance(url, str) or not url:
            continue
        results.append(
            {
                "title": str(item.get("title") or item.get("name") or url),
                "url": url,
                "snippet": str(item.get("description") or item.get("snippet") or ""),
            }
        )
    return results


def _context_policy(context: RuntimeExecutionContext, fallback: str = "bridge") -> OutboundPolicy:
    """The resolved policy for this call, or a conservative fallback.

    ``BuiltinRuntime`` injects ``_policy`` after resolving the definition's own
    ``network_policy``. Handlers must use it rather than inventing a policy, or a
    tool configured with a narrow allowlist would silently reach any public host.
    """
    policy = context.runtime_config.get("_policy")
    if isinstance(policy, OutboundPolicy):
        return policy
    return OutboundPolicy(network_mode=fallback)


async def _web_fetch(context: RuntimeExecutionContext) -> Any:
    """Fetch one public page and return readable text.

    Uses the definition's resolved policy, so a tool with an allowlist can only
    reach the hosts it named. HTML is reduced to text rather than returned raw: a
    page of markup costs model context and carries no extra meaning.
    """
    import html as html_lib

    args = context.arguments
    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolRuntimeError("url is required")
    try:
        max_chars = max(500, min(int(args.get("max_chars", 20000) or 20000), 100_000))
    except (TypeError, ValueError):
        max_chars = 20000

    policy = _context_policy(context)
    request = OutboundRequest(
        method="GET",
        url=url,
        headers={"User-Agent": _UA, "Accept": "text/html,application/json,text/plain,*/*"},
        timeout_seconds=25,
    )
    response = await send(request, policy=policy, purpose="web_fetch")
    if response.status_code >= 400:
        # Upstream text is an unfiltered injection channel; report the status only.
        raise ToolRuntimeError(
            f"Page returned HTTP {response.status_code} for {redact_url(response.final_url)}"
        )

    content_type = response.headers.get("content-type", "")
    body = response.text()
    title = ""
    if "html" in content_type or body.lstrip().lower().startswith("<!doctype html"):
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE)
        if title_match:
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        body = _html_to_text(body)

    return {
        "url": response.final_url,
        "status": response.status_code,
        "content_type": content_type,
        "title": title,
        "truncated": len(body) > max_chars or response.truncated,
        "text": body[:max_chars],
    }


def _html_to_text(markup: str) -> str:
    """Reduce HTML to text without pulling in a parser dependency."""
    import html as html_lib

    cleaned = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", markup)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(
        r"(?i)</(p|div|li|tr|h[1-6]|section|article|header|footer|blockquote)>",
        "\n\n",
        cleaned,
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    cleaned = re.sub(r"[ \t\x0b\f\r]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


async def _weather_forecast(context: RuntimeExecutionContext) -> Any:
    """Open-Meteo forecast, no API key required."""
    args = context.arguments
    try:
        latitude = float(args.get("latitude"))
        longitude = float(args.get("longitude"))
    except (TypeError, ValueError):
        raise ToolRuntimeError("latitude and longitude must be numeric") from None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ToolRuntimeError("latitude or longitude out of range")
    try:
        days = max(1, min(int(args.get("forecast_days", 3) or 3), 16))
    except (TypeError, ValueError):
        days = 3
    units = str(args.get("units") or "imperial").strip().lower()
    timezone = str(args.get("timezone") or "UTC").strip() or "UTC"

    metric = units == "metric"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
        "forecast_days": days,
        "temperature_unit": "celsius" if metric else "fahrenheit",
        "wind_speed_unit": "kmh" if metric else "mph",
        "precipitation_unit": "mm" if metric else "inch",
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,"
                   "wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max,uv_index_max,weather_code,"
                 "sunrise,sunset",
    }
    import httpx

    base = "https://api.open-meteo.com/v1/forecast"
    request = OutboundRequest(
        method="GET",
        url=f"{base}?{httpx.QueryParams(params)}",
        headers={"User-Agent": _UA},
        timeout_seconds=20,
    )
    response = await send(
        request,
        policy=OutboundPolicy(network_mode="allowlist", allowed_hosts=("api.open-meteo.com",)),
        purpose="weather forecast",
    )
    if response.status_code >= 400:
        raise ToolRuntimeError(f"Open-Meteo returned HTTP {response.status_code}")
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        raise ToolRuntimeError(
            "Open-Meteo returned a response that is not JSON; the endpoint may be "
            "rate limiting or intercepting this deployment."
        ) from None
    return {
        "provider": "open-meteo",
        "units": "metric" if metric else "imperial",
        "location": {"latitude": latitude, "longitude": longitude, "timezone": timezone},
        "current": data.get("current"),
        "daily": data.get("daily"),
        "weather_code_legend": _WMO_CODES,
    }


_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


# ── handlers: optional (package-gated) ────────────────────────────────────


async def _pdf_extract(context: RuntimeExecutionContext) -> Any:
    args = context.arguments
    encoded = str(args.get("pdf_base64") or "")
    if not encoded:
        raise ToolRuntimeError("pdf_base64 is required")
    if len(encoded) > 20_000_000:
        raise ToolRuntimeError("pdf payload is too large (limit 20MB base64)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ToolRuntimeError("pdf_base64 is not valid base64") from None

    pypdf = _require_import("pypdf", "pypdf")
    reader = pypdf.PdfReader(io.BytesIO(raw))
    pages: list[dict[str, Any]] = []
    limit = args.get("max_pages", 50)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50

    total_chars = 0
    for index, page in enumerate(reader.pages[:limit]):
        text = page.extract_text() or ""
        total_chars += len(text)
        if total_chars > _MAX_TEXT_CHARS:
            pages.append({"page": index + 1, "text": "[truncated: document exceeds text limit]"})
            break
        pages.append({"page": index + 1, "text": text})
    metadata = {}
    try:
        info = reader.metadata or {}
        metadata = {str(key).lstrip("/"): str(value) for key, value in info.items()}
    except Exception:  # noqa: BLE001 — metadata is best effort
        metadata = {}
    return {
        "page_count": len(reader.pages),
        "returned_pages": len(pages),
        "metadata": metadata,
        "pages": pages,
    }


async def _docx_extract(context: RuntimeExecutionContext) -> Any:
    encoded = str(context.arguments.get("docx_base64") or "")
    if not encoded:
        raise ToolRuntimeError("docx_base64 is required")
    if len(encoded) > 20_000_000:
        raise ToolRuntimeError("docx payload is too large (limit 20MB base64)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ToolRuntimeError("docx_base64 is not valid base64") from None
    docx = _require_import("docx", "python-docx")
    document = docx.Document(io.BytesIO(raw))
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs)
    return {
        "paragraphs": len(paragraphs),
        "tables": len(document.tables),
        "truncated": len(text) > _MAX_TEXT_CHARS,
        "text": text[:_MAX_TEXT_CHARS],
    }


async def _xlsx_read(context: RuntimeExecutionContext) -> Any:
    args = context.arguments
    encoded = str(args.get("xlsx_base64") or "")
    if not encoded:
        raise ToolRuntimeError("xlsx_base64 is required")
    if len(encoded) > 20_000_000:
        raise ToolRuntimeError("xlsx payload is too large (limit 20MB base64)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ToolRuntimeError("xlsx_base64 is not valid base64") from None
    openpyxl = _require_import("openpyxl", "openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        limit = max(1, min(int(args.get("max_rows", 200) or 200), 5000))
    except (TypeError, ValueError):
        limit = 200
    sheet_name = str(args.get("sheet") or "").strip()
    names = list(workbook.sheetnames)
    if sheet_name:
        if sheet_name not in names:
            raise ToolRuntimeError(
                f"Sheet {sheet_name!r} not found; available: " + ", ".join(names)
            )
        target = sheet_name
    else:
        target = names[0] if names else ""
    if not target:
        return {"sheets": [], "rows": []}
    sheet = workbook[target]
    rows: list[list[Any]] = []
    for index, row in enumerate(sheet.iter_rows(values_only=True)):
        if index >= limit:
            break
        rows.append([value for value in row])
    return {"sheets": names, "sheet": target, "row_count": sheet.max_row,
            "returned_rows": len(rows), "rows": rows}


async def _image_info(context: RuntimeExecutionContext) -> Any:
    encoded = str(context.arguments.get("image_base64") or "")
    if not encoded:
        raise ToolRuntimeError("image_base64 is required")
    if len(encoded) > 30_000_000:
        raise ToolRuntimeError("image payload is too large (limit 30MB base64)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ToolRuntimeError("image_base64 is not valid base64") from None
    pil_image = _require_import("PIL.Image", "Pillow")
    image = pil_image.open(io.BytesIO(raw))
    return {
        "format": image.format,
        "mode": image.mode,
        "width": image.width,
        "height": image.height,
        "aspect_ratio": round(image.width / image.height, 4) if image.height else None,
        "bytes": len(raw),
    }


# ── catalog ──────────────────────────────────────────────────────────────

_OBJECT = "object"


BUILTIN_TOOL_CATALOG: dict[str, BuiltinToolSpec] = {}


def _register(spec: BuiltinToolSpec) -> None:
    BUILTIN_TOOL_CATALOG[spec.name] = spec


_register(BuiltinToolSpec(
    name="web_search",
    label="Web search",
    description=(
        "Search the public web and return titles, URLs, and snippets. No API key "
        "required. Use it for current facts, prices, news, or anything the model "
        "cannot know from training."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {
            "query": {"type": "string", "description": "Search terms"},
            "max_results": {
                "type": "integer", "minimum": 1, "maximum": 10, "default": 5,
                "description": "How many results to return",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_web_search,
    uses_network=True,
    network_hosts=("html.duckduckgo.com",),
    default_config={"timeout_seconds": 25},
))

_register(BuiltinToolSpec(
    name="web_fetch",
    label="Fetch web page",
    description=(
        "Fetch one public web page and return its readable text. HTML is converted "
        "to plain text; JSON is returned as text. Private and internal addresses are "
        "blocked."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL"},
            "max_chars": {
                "type": "integer", "minimum": 500, "maximum": 100000, "default": 20000,
                "description": "Maximum characters of text to return",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    handler=_web_fetch,
    uses_network=True,
    network_hosts=(),
    default_config={"timeout_seconds": 30},
))

_register(BuiltinToolSpec(
    name="weather_forecast",
    label="Weather forecast",
    description=(
        "Current conditions and a multi-day forecast for a latitude/longitude, from "
        "Open-Meteo. No API key required."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "forecast_days": {
                "type": "integer", "minimum": 1, "maximum": 16, "default": 3,
            },
            "units": {"type": "string", "enum": ["imperial", "metric"], "default": "imperial"},
            "timezone": {"type": "string", "default": "UTC"},
        },
        "required": ["latitude", "longitude"],
        "additionalProperties": False,
    },
    handler=_weather_forecast,
    uses_network=True,
    network_hosts=("api.open-meteo.com",),
    default_config={"timeout_seconds": 25},
))

_register(BuiltinToolSpec(
    name="pdf_extract_text",
    label="Extract PDF text",
    description=(
        "Extract text per page from a base64-encoded PDF, plus document metadata. "
        "Optional: requires the pypdf package in the image."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {
            "pdf_base64": {"type": "string"},
            "max_pages": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
        "required": ["pdf_base64"],
        "additionalProperties": False,
    },
    handler=_pdf_extract,
    requires_package="pypdf",
    build_flag="LUCENT_TOOLS_ENABLE_DOCUMENTS",
))

_register(BuiltinToolSpec(
    name="docx_extract_text",
    label="Extract DOCX text",
    description=(
        "Extract paragraph text from a base64-encoded .docx file. Optional: requires "
        "the python-docx package in the image."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {"docx_base64": {"type": "string"}},
        "required": ["docx_base64"],
        "additionalProperties": False,
    },
    handler=_docx_extract,
    requires_package="python-docx",
    build_flag="LUCENT_TOOLS_ENABLE_DOCUMENTS",
))

_register(BuiltinToolSpec(
    name="xlsx_read",
    label="Read spreadsheet",
    description=(
        "Read rows from a base64-encoded .xlsx workbook. Optional: requires the "
        "openpyxl package in the image."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {
            "xlsx_base64": {"type": "string"},
            "sheet": {"type": "string"},
            "max_rows": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 200},
        },
        "required": ["xlsx_base64"],
        "additionalProperties": False,
    },
    handler=_xlsx_read,
    requires_package="openpyxl",
    build_flag="LUCENT_TOOLS_ENABLE_DOCUMENTS",
))

_register(BuiltinToolSpec(
    name="image_info",
    label="Image info",
    description=(
        "Report format, dimensions, aspect ratio, and byte size of a base64-encoded "
        "image. Optional: requires the Pillow package in the image."
    ),
    input_schema={
        "type": _OBJECT,
        "properties": {"image_base64": {"type": "string"}},
        "required": ["image_base64"],
        "additionalProperties": False,
    },
    handler=_image_info,
    requires_package="Pillow",
    build_flag="LUCENT_TOOLS_ENABLE_IMAGES",
))


# ── availability ─────────────────────────────────────────────────────────

_PACKAGE_IMPORT_NAMES = {
    "pypdf": "pypdf",
    "python-docx": "docx",
    "openpyxl": "openpyxl",
    "Pillow": "PIL.Image",
}


def _package_available(package: str) -> tuple[bool, str | None]:
    import importlib.util

    module = _PACKAGE_IMPORT_NAMES.get(package, package)
    try:
        found = importlib.util.find_spec(module.split(".")[0]) is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return True, None
    flag = next(
        (
            spec.build_flag
            for spec in BUILTIN_TOOL_CATALOG.values()
            if spec.requires_package == package
        ),
        "the matching build argument",
    )
    return False, (
        f"Needs the '{package}' package. Rebuild the image with {flag}=1."
    )


def available_builtin_tools() -> list[dict[str, Any]]:
    """Every catalog entry with a live availability verdict."""
    catalog: list[dict[str, Any]] = []
    for spec in BUILTIN_TOOL_CATALOG.values():
        if spec.requires_package is None:
            catalog.append(spec.descriptor())
            continue
        available, reason = _package_available(spec.requires_package)
        catalog.append(spec.descriptor(available=available, reason=reason))
    return catalog


def get_builtin_tool(name: str) -> BuiltinToolSpec:
    key = str(name or "").strip()
    try:
        return BUILTIN_TOOL_CATALOG[key]
    except KeyError:
        raise ToolRuntimeError(
            f"No built-in tool named {name!r}. Available: "
            + ", ".join(sorted(BUILTIN_TOOL_CATALOG))
        ) from None


async def invoke_builtin_tool(
    name: str,
    context: RuntimeExecutionContext,
) -> Any:
    """Run one catalog handler, translating policy errors to blocked errors."""
    spec = get_builtin_tool(name)
    if spec.requires_package is not None:
        available, reason = _package_available(spec.requires_package)
        if not available:
            raise UnavailableBuiltinToolError(reason or "required package is not installed")
    try:
        return await spec.handler(context)
    except HTTPPolicyError as exc:
        from lucent.services.tool_runtimes.base import ToolRuntimeBlockedError

        raise ToolRuntimeBlockedError(str(exc)) from exc
