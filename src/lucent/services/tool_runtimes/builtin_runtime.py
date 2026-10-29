"""``builtin`` runtime: run a reviewed Lucent-native handler.

An owner does not write code for this kind — they select a handler from
:mod:`lucent.services.builtin_tool_catalog` by name, and the model-facing schema
comes from that catalog rather than from the definition. Approval therefore means
"this agent may call Lucent's reviewed web_search", not "this agent may run
arbitrary code an owner wrote".

Configuration (``runtime_config``)::

    {"handler": "web_search", "options": {"default_max_results": 5}}

The tool's own ``input_schema`` is copied from the catalog at creation time so the
model's contract does not drift silently, but the catalog is authoritative at call
time: an owner cannot widen a handler's schema by editing the row.

Network policy still applies. A handler that performs network I/O runs under the
definition's ``network_policy`` exactly like an ``http`` tool, so the same
allowlist governs both kinds.
"""

from __future__ import annotations

from typing import Any

from lucent.logging import get_logger
from lucent.services.builtin_tool_catalog import (
    UnavailableBuiltinToolError,
    get_builtin_tool,
    invoke_builtin_tool,
)
from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    RuntimeExecutionResult,
    ToolRuntimeBlockedError,
    ToolRuntimeError,
)
from lucent.services.tool_runtimes.http_policy import HTTPPolicyError, OutboundPolicy

logger = get_logger("services.tool_runtimes.builtin")


class BuiltinRuntime:
    """Execute a Lucent-native handler chosen from the reviewed catalog."""

    runtime_type = "builtin"
    requires_sandbox = False

    async def execute(self, context: RuntimeExecutionContext) -> RuntimeExecutionResult:
        config = context.runtime_config
        tool = config.get("_tool") or {}
        handler_name = str(config.get("handler") or "").strip()
        if not handler_name:
            raise ToolRuntimeError(
                "Built-in tool requires runtime_config.handler naming a catalog entry"
            )

        try:
            spec = get_builtin_tool(handler_name)
        except ToolRuntimeError as exc:
            raise ToolRuntimeError(str(exc)) from None

        # The catalog is authoritative at call time, so the definition cannot
        # widen a handler's contract by editing its stored input_schema.
        self._validate_against_catalog(spec, context.arguments)

        # A network-using handler must have a policy that permits its hosts. The
        # handler calls `send()` which enforces SSRF protection, but the
        # definition's network_mode is the operator's decision and must be
        # honored here too — otherwise a "no network" tool would still reach out.
        policy = self._resolve_policy(tool)
        if spec.uses_network:
            self._assert_network_permitted(spec, policy, tool)

        # Hand the resolved policy down so a handler cannot widen it, and so
        # web_fetch honors the definition's own allowlist instead of inventing
        # one.
        context.runtime_config = {**config, "_policy": policy}

        try:
            value = await invoke_builtin_tool(handler_name, context)
        except UnavailableBuiltinToolError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from None

        return RuntimeExecutionResult(
            ok=True,
            result=value,
            stdout=f"builtin:{handler_name}",
            stderr="",
        )

    @staticmethod
    def _resolve_policy(tool: dict[str, Any]) -> OutboundPolicy:
        try:
            return OutboundPolicy.from_tool(tool)
        except HTTPPolicyError as exc:
            raise ToolRuntimeBlockedError(str(exc)) from None

    @staticmethod
    def _validate_against_catalog(spec: Any, arguments: dict[str, Any]) -> None:
        from jsonschema import ValidationError, validate

        try:
            validate(instance=arguments, schema=spec.input_schema)
        except ValidationError as exc:
            raise ToolRuntimeBlockedError(
                f"Arguments do not match {spec.name}'s schema: {exc.message}"
            ) from None

    @staticmethod
    def _assert_network_permitted(
        spec: Any, policy: OutboundPolicy, tool: dict[str, Any]
    ) -> None:
        if policy.network_mode == "none":
            raise ToolRuntimeBlockedError(
                f"'{spec.name}' needs outbound network access, but this tool's "
                "network_policy.network_mode is 'none'. An owner must set it to "
                "'allowlist' or 'bridge'."
            )
        if policy.network_mode == "allowlist" and spec.network_hosts:
            missing = [host for host in spec.network_hosts if not policy.allows_host(host)]
            if missing:
                raise ToolRuntimeBlockedError(
                    f"network_policy.allowed_hosts does not include the host(s) "
                    f"'{spec.name}' requires: " + ", ".join(missing)
                )
