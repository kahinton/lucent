"""Declarative ``http`` runtime: a tool defined entirely by a request template.

This is the kind that makes an agent platform useful without a sandbox. It is the
same shape OpenAI calls a function tool backed by your own endpoint, Anthropic
calls a client tool, Bedrock calls an action group, and every "OpenAPI → tools"
product compiles down to. Nothing is executed as code; the server renders one
request from the model's arguments and sends it.

Configuration (``runtime_config``)::

    {
      "url": "https://api.example.com/v1/weather/{city}",
      "method": "GET",
      "query": {"units": "{units}", "appid": "{{env:api_key}}"},
      "headers": {"X-Request-Id": "{request_id}"},
      "body": {"q": "{query}"},              # object => JSON, string => raw/text
      "form": {"q": "{query}"},              # mutually exclusive with body
      "content_type": "application/json",    # optional override for raw bodies
      "response_path": "data.items",         # dot path into the JSON response
      "response_format": "json",             # json | text
      "timeout_seconds": 30,
      "allowed_status": [200, 201, 204]
    }

Arguments are the only thing the model controls. Anything the definition needs
from a credential must arrive as ``{{env:NAME}}``, where ``NAME`` is a key in the
tool's ``env_vars`` mapping — and that mapping is required to use
``secret://``/``credential://`` references for sensitive names, already enforced
by the repository on write.

The runtime trusts the executor completely: authorization, consent, and input
schema validation have already happened. It enforces only outbound policy, which
is the one thing the executor deliberately delegates.
"""

from __future__ import annotations

import json
from typing import Any

from lucent.logging import get_logger
from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    RuntimeExecutionResult,
    ToolRuntimeBlockedError,
    ToolRuntimeError,
)
from lucent.services.tool_runtimes.http_policy import (
    HTTPPolicyError,
    OutboundPolicy,
    redact_url,
)
from lucent.services.tool_runtimes.outbound import (
    DEFAULT_TIMEOUT_SECONDS,
    OutboundRequest,
    credential_header_names,
    send,
)
from lucent.services.tool_runtimes.templating import (
    TemplateError,
    render_header_value,
    render_path_value,
    render_scalar,
)

logger = get_logger("services.tool_runtimes.http")

_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
_MAX_RESPONSE_CHARS = 30_000


def _allowed_status_set(value: Any) -> set[int]:
    """Parse ``allowed_status`` defensively.

    A malformed value is a definition bug, but it must produce a clear tool error
    rather than a 500 from a set comprehension deep inside a call.
    """
    if value is None:
        return {200}
    if not isinstance(value, (list, tuple)):
        raise ToolRuntimeError("runtime_config.allowed_status must be a JSON array")
    allowed: set[int] = set()
    for entry in value:
        try:
            code = int(entry)
        except (TypeError, ValueError):
            raise ToolRuntimeError(
                f"runtime_config.allowed_status contains a non-numeric entry: {entry!r}"
            ) from None
        if code < 100 or code > 599:
            raise ToolRuntimeError(
                f"runtime_config.allowed_status entry {code} is not an HTTP status code"
            )
        allowed.add(code)
    return allowed or {200}


def _bounded_timeout(value: Any) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ToolRuntimeError(
            "runtime_config.timeout_seconds must be an integer"
        ) from None
    if seconds < 1:
        raise ToolRuntimeError("runtime_config.timeout_seconds must be at least 1")
    return seconds


def _merge_query(base: str, pairs: list[tuple[str, str]]) -> str:
    """Append query pairs without corrupting an existing query or fragment."""
    if not pairs:
        return base
    from urllib.parse import urlencode, urlsplit, urlunsplit

    split = urlsplit(base)

    encoded = urlencode(pairs, doseq=True)
    combined = f"{split.query}&{encoded}" if split.query else encoded
    return urlunsplit((split.scheme, split.netloc, split.path, combined, split.fragment))


class HttpRuntime:
    """Execute a declarative HTTPS request described by the tool definition."""

    runtime_type = "http"
    requires_sandbox = False

    async def execute(self, context: RuntimeExecutionContext) -> RuntimeExecutionResult:
        config = context.runtime_config
        tool = config.get("_tool") or {}

        url_template = config.get("url")
        if not isinstance(url_template, str) or not url_template.strip():
            raise ToolRuntimeError("HTTP tool requires runtime_config.url")

        method = str(config.get("method") or "GET").strip().upper()
        if method not in _ALLOWED_METHODS:
            raise ToolRuntimeError(
                f"Unsupported HTTP method {method!r}; expected one of "
                + ", ".join(sorted(_ALLOWED_METHODS))
            )

        args = context.arguments
        creds = context.credentials
        try:
            path = render_path_value(url_template, args, creds)
        except TemplateError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from exc

        query_pairs = self._render_query(config.get("query"), args, creds)
        headers = self._render_headers(config.get("headers"), args, creds)
        body, form, raw = self._render_body(config, args, creds)
        # URL templates may already carry a query string; merge rather than
        # replace so a static API version or format parameter is preserved.
        url = _merge_query(path, query_pairs)
        content_type = config.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            raise ToolRuntimeError("runtime_config.content_type must be a string")

        policy = self._resolve_policy(tool)
        timeout_seconds = _bounded_timeout(config.get("timeout_seconds"))

        request = OutboundRequest(
            method=method,
            url=url,
            headers=headers,
            json_body=body,
            form_body=form,
            raw_body=raw,
            content_type=content_type or None,
            timeout_seconds=timeout_seconds,
            credential_headers=credential_header_names(
                headers, config.get("headers"), config.get("credential_headers")
            ),
        )

        try:
            response = await send(request, policy=policy, purpose=f"HTTP tool {tool.get('name')}")
        except HTTPPolicyError as exc:
            # A policy refusal is an authorization-shaped failure, not a bug in
            # the tool: surface it as blocked so retries/telemetry distinguish it.
            raise ToolRuntimeBlockedError(str(exc)) from exc

        allowed_status = _allowed_status_set(config.get("allowed_status"))
        if response.status_code not in allowed_status:
            snippet = response.text()[:500]
            return RuntimeExecutionResult(
                ok=False,
                stdout=snippet,
                stderr="",
                error=(
                    f"Upstream returned HTTP {response.status_code}, which this tool treats "
                    f"as a failure (allowed: {sorted(allowed_status)})."
                ),
            )

        payload = self._decode_payload(response, config)
        payload = self._select_path(payload, config.get("response_path"))
        return RuntimeExecutionResult(
            ok=True,
            result=payload,
            stdout=self._render_stdout(response),
            stderr="",
        )

    # -- configuration helpers ------------------------------------------------

    @staticmethod
    def _resolve_policy(tool: dict[str, Any]) -> OutboundPolicy:
        try:
            return OutboundPolicy.from_tool(tool)
        except HTTPPolicyError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from exc

    @staticmethod
    def _render_query(
        query: Any,
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> list[tuple[str, str]]:
        if query is None:
            return []
        if not isinstance(query, dict):
            raise ToolRuntimeError("runtime_config.query must be a JSON object")
        pairs: list[tuple[str, str]] = []
        for key, value in query.items():
            name = str(key)
            if isinstance(value, list):
                for item in value:
                    pairs.append((name, HttpRuntime._render_scalar(item, arguments, credentials)))
            elif value is None:
                continue
            else:
                pairs.append((name, HttpRuntime._render_scalar(value, arguments, credentials)))
        return pairs

    @staticmethod
    def _render_scalar(
        value: Any,
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> str:
        if isinstance(value, str):
            try:
                return render_scalar(value, arguments, credentials)
            except TemplateError as exc:
                raise ToolRuntimeBlockedError(str(exc)) from exc
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        raise ToolRuntimeError(
            "Query values must be strings, numbers, booleans, or arrays of those"
        )

    @staticmethod
    def _render_headers(
        headers: Any,
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> dict[str, str]:
        if headers is None:
            return {}
        if not isinstance(headers, dict):
            raise ToolRuntimeError("runtime_config.headers must be a JSON object")
        rendered: dict[str, str] = {}
        for key, value in headers.items():
            if not isinstance(value, str):
                raise ToolRuntimeError("Header values must be strings")
            try:
                rendered[str(key)] = render_header_value(value, arguments, credentials)
            except TemplateError as exc:
                raise ToolRuntimeBlockedError(str(exc)) from exc
        return rendered

    @staticmethod
    def _render_body(
        config: dict[str, Any],
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> tuple[Any, dict[str, str] | None, bytes | None]:
        has_body = "body" in config and config["body"] is not None
        has_form = "form" in config and config["form"] is not None
        if has_body and has_form:
            raise ToolRuntimeError(
                "runtime_config may set 'body' or 'form', not both"
            )
        if has_form:
            form_spec = config["form"]
            if not isinstance(form_spec, dict):
                raise ToolRuntimeError("runtime_config.form must be a JSON object")
            form: dict[str, str] = {}
            for key, value in form_spec.items():
                form[str(key)] = HttpRuntime._render_scalar(value, arguments, credentials)
            return None, form, None
        if not has_body:
            return None, None, None

        body_spec = config["body"]
        if isinstance(body_spec, dict):
            return HttpRuntime._render_json_tree(body_spec, arguments, credentials), None, None
        if isinstance(body_spec, str):
            # A string body is sent verbatim after substitution. Authors use this
            # for XML/SOAP/GraphQL, which have no JSON encoding.
            try:
                rendered = render_scalar(body_spec, arguments, credentials)
            except TemplateError as exc:
                raise ToolRuntimeBlockedError(str(exc)) from exc
            return None, None, rendered.encode("utf-8")
        raise ToolRuntimeError("runtime_config.body must be a JSON object or a string")

    @staticmethod
    def _render_json_tree(
        node: Any,
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> Any:
        if isinstance(node, dict):
            return {
                str(key): HttpRuntime._render_json_tree(value, arguments, credentials)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [HttpRuntime._render_json_tree(item, arguments, credentials) for item in node]
        if isinstance(node, str):
            try:
                return render_scalar(node, arguments, credentials)
            except TemplateError as exc:
                raise ToolRuntimeBlockedError(str(exc)) from exc
        return node

    @staticmethod
    def _credential_header_names(
        headers: dict[str, str], config: dict[str, Any]
    ) -> frozenset[str]:
        """Retained for callers that used the old private helper."""
        return credential_header_names(
            headers, config.get("headers"), config.get("credential_headers")
        )

    @staticmethod
    def _decode_payload(response: Any, config: dict[str, Any]) -> Any:
        fmt = str(config.get("response_format") or "json").strip().lower()
        if fmt == "text":
            return response.text()
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            # Declaring JSON and receiving HTML is common when an upstream
            # returns an error page with status 200. Returning the text is more
            # useful to the model than raising, and the caller still sees ok=True
            # only because the status was allowed.
            return response.text()

    @staticmethod
    def _select_path(payload: Any, path: Any) -> Any:
        if not path or not isinstance(path, str):
            return payload
        current = payload
        for segment in path.split("."):
            key = segment.strip()
            if not key:
                continue
            if isinstance(current, dict):
                if key not in current:
                    raise ToolRuntimeError(
                        f"response_path {path!r} did not match the upstream response "
                        f"(missing key {key!r})"
                    )
                current = current[key]
            elif isinstance(current, list):
                try:
                    current = current[int(key)]
                except (ValueError, IndexError):
                    raise ToolRuntimeError(
                        f"response_path {path!r} expected an index at {key!r}"
                    ) from None
            else:
                raise ToolRuntimeError(
                    f"response_path {path!r} cannot descend into a "
                    f"{type(current).__name__} at {key!r}"
                )
        return current

    @staticmethod
    def _render_stdout(response: Any) -> str:
        note = ""
        if response.truncated:
            note = "\n[response truncated at the runtime byte limit]"
        # The query string is where a credential substitution commonly lands, so
        # only the redacted URL reaches the model and the run ledger.
        return (
            f"HTTP {response.status_code} ({redact_url(response.final_url)}){note}"
        )[:_MAX_RESPONSE_CHARS]
