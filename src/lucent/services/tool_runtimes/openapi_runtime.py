"""``openapi`` runtime: one operation derived from an OpenAPI document.

The document is stored on the definition (``runtime_config.spec``) rather than
fetched at call time. That is a deliberate security decision:

* a *fetch-at-call-time* spec makes the tool's behaviour depend on whatever the
  upstream served this second, so neither the approver nor the model's schema can
  be trusted across calls;
* it turns every invocation into an outbound request to an endpoint chosen by the
  tool author, which is an SSRF pivot that bypasses the operation's own policy.

Approving a tool therefore means approving a *fixed* operation. Refreshing a spec
is an explicit edit, which re-enters review.

Configuration (``runtime_config``)::

    {
      "spec": "<raw OpenAPI JSON or YAML>",     # or "spec_url" + "spec" cached
      "spec_url": "https://api.example.com/openapi.json",  # optional provenance
      "operation_id": "getWeather",
      "base_url": "https://api.example.com",   # only to override the spec default
      "response_path": "data",
      "response_format": "json",
      "allowed_status": [200, 201],
      "timeout_seconds": 30,
      "query": {"appid": "{{env:api_key}}"},   # static + credential params
      "headers": {"Authorization": "Bearer {{env:api_key}}"}
    }

``query`` and ``headers`` are merged with the spec-derived values so a definition
can attach a credential the operation declares as ``security`` without teaching
the model a secret-shaped argument.
"""

from __future__ import annotations

from typing import Any

from lucent.logging import get_logger
from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    RuntimeExecutionResult,
    ToolRuntimeBlockedError,
    ToolRuntimeError,
)
from lucent.services.tool_runtimes.http_policy import HTTPPolicyError, OutboundPolicy
from lucent.services.tool_runtimes.http_runtime import (
    HttpRuntime,
    _allowed_status_set,
    _bounded_timeout,
)
from lucent.services.tool_runtimes.openapi_spec import (
    SpecError,
    build_request,
    join_url,
    normalize_base_url_for_storage,
    parse_spec,
    resolve_operation,
)
from lucent.services.tool_runtimes.outbound import (
    OutboundRequest,
    credential_header_names,
    send,
)
from lucent.services.tool_runtimes.templating import (
    TemplateError,
    render_header_value,
    render_scalar,
)

logger = get_logger("services.tool_runtimes.openapi")


class OpenAPIRuntime:
    """Execute one operation from a stored OpenAPI document."""

    runtime_type = "openapi"
    requires_sandbox = False

    async def execute(self, context: RuntimeExecutionContext) -> RuntimeExecutionResult:
        config = context.runtime_config
        tool = config.get("_tool") or {}

        raw_spec = config.get("spec")
        if not raw_spec:
            raise ToolRuntimeError(
                "OpenAPI tool requires runtime_config.spec. A spec is stored at approval "
                "time so an approved tool cannot change behaviour underneath the model."
            )
        try:
            document = parse_spec(raw_spec)
            operation = resolve_operation(
                document,
                operation_id=config.get("operation_id"),
                method=config.get("method"),
                path=config.get("path"),
            )
        except SpecError as exc:
            raise ToolRuntimeError(str(exc)) from exc

        if config.get("base_url"):
            try:
                override = normalize_base_url_for_storage(str(config["base_url"]))
            except SpecError as exc:
                raise ToolRuntimeError(f"runtime_config.base_url is invalid: {exc}") from None
            spec_scheme = (operation.get("base_url") or "").split(":", 1)[0].lower()
            if spec_scheme == "https" and override.lower().startswith("http://"):
                raise ToolRuntimeError(
                    "runtime_config.base_url may not downgrade an HTTPS server URL to HTTP"
                )
            operation = {**operation, "base_url": override}

        try:
            derived = build_request(document, operation, context.arguments)
        except SpecError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from exc

        url = join_url(derived["base_url"], derived["path"])

        args = context.arguments
        creds = context.credentials
        try:
            extra_query = self._render_mapping(config.get("query"), args, creds)
            extra_headers = self._render_headers(config.get("headers"), args, creds)
        except TemplateError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from exc

        query = dict(derived["query"])
        query.update(extra_query)
        headers = dict(derived["headers"])
        # Auth headers the definition supplies win over anything the spec
        # generated, so a credential reference cannot be shadowed by a parameter.
        headers.update(extra_headers)

        media_type = str(derived.get("media_type") or "application/json")
        body = derived["body"]
        form_body = derived.get("form_body")
        raw_body = None
        if body is not None and form_body is None:
            if media_type == "application/x-www-form-urlencoded":
                form_body = (
                    {str(key): str(value) for key, value in body.items()}
                    if isinstance(body, dict)
                    else {"body": str(body)}
                )
                body = None
        elif form_body is not None:
            # A form operation may also carry a JSON body in the spec; a form
            # encoding wins because that is the media type the document
            # declared for this operation.
            body = None

        if query:
            from urllib.parse import urlencode

            encoded = urlencode(
                [(key, item) for key, value in query.items()
                 for item in (value if isinstance(value, list) else [value])],
                doseq=True,
            )
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{encoded}"

        try:
            policy = OutboundPolicy.from_tool(tool)
        except HTTPPolicyError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from exc

        timeout_seconds = _bounded_timeout(config.get("timeout_seconds"))

        request = OutboundRequest(
            method=derived["method"],
            url=url,
            headers=headers,
            json_body=body,
            form_body=form_body,
            raw_body=raw_body,
            timeout_seconds=timeout_seconds,
            credential_headers=credential_header_names(
                headers, config.get("headers"), config.get("credential_headers")
            ),
        )

        try:
            response = await send(
                request,
                policy=policy,
                purpose=f"OpenAPI tool {tool.get('name')}",
            )
        except HTTPPolicyError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from exc

        allowed_status = _allowed_status_set(config.get("allowed_status"))
        if response.status_code not in allowed_status:
            snippet = response.text()[:500]
            return RuntimeExecutionResult(
                ok=False,
                stdout=snippet,
                error=(
                    f"{operation['operation_id']} returned HTTP {response.status_code}, "
                    f"which this tool treats as a failure "
                    f"(allowed: {sorted(allowed_status)})."
                ),
            )

        payload = HttpRuntime._decode_payload(response, config)
        payload = HttpRuntime._select_path(payload, config.get("response_path"))
        return RuntimeExecutionResult(
            ok=True,
            result=payload,
            stdout=HttpRuntime._render_stdout(response),
        )

    @staticmethod
    def _render_mapping(
        mapping: Any,
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> dict[str, str]:
        if mapping is None:
            return {}
        if not isinstance(mapping, dict):
            raise ToolRuntimeError("runtime_config query/headers must be JSON objects")
        rendered: dict[str, str] = {}
        for key, value in mapping.items():
            if not isinstance(value, str):
                raise ToolRuntimeError("Static query/header values must be strings")
            rendered[str(key)] = render_scalar(value, arguments, credentials)
        return rendered

    @staticmethod
    def _render_headers(
        mapping: Any,
        arguments: dict[str, Any],
        credentials: dict[str, str],
    ) -> dict[str, str]:
        if mapping is None:
            return {}
        if not isinstance(mapping, dict):
            raise ToolRuntimeError("runtime_config.headers must be a JSON object")
        rendered: dict[str, str] = {}
        for key, value in mapping.items():
            if not isinstance(value, str):
                raise ToolRuntimeError("Header values must be strings")
            rendered[str(key)] = render_header_value(value, arguments, credentials)
        return rendered
