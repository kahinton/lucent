"""Execution of managed tool definitions across runtime kinds.

This module owns the security boundary, not the execution mechanism. Every
managed tool — sandboxed Python, declarative HTTP, an OpenAPI operation, or a
Lucent-native handler — passes the same gates here:

1. the caller's organization must have the capability enabled for this kind;
2. the tool must be active;
3. the caller's role must satisfy ``auth_policy``;
4. when invoked from an agent, the tool must be granted to that agent;
5. input and output must satisfy the declared JSON Schemas;
6. each invocation is recorded in the run table and the usage ledger.

Only step 7 — actually running it — differs, and that is delegated to
:mod:`lucent.services.tool_runtimes`. Keeping authorization here rather than in
the runtimes is what makes a new runtime kind a small, reviewable change instead
of a second permission model that can drift.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jsonschema import ValidationError, validate

from lucent.auth import set_current_user
from lucent.db.definitions import DefinitionRepository
from lucent.logging import get_logger
from lucent.sandbox.manager import get_sandbox_manager
from lucent.secrets import SecretRegistry, resolve_env_vars
from lucent.services.entitlements import (
    CAPABILITY_MANAGED_TOOLS,
    CAPABILITY_SANDBOXES,
    capability_is_granted,
)
from lucent.services.tool_runtimes import (
    RuntimeExecutionContext,
    get_runtime,
    runtime_requires_sandbox,
)
from lucent.services.tool_runtimes.base import (
    ToolRuntimeBlockedError as RuntimePolicyBlockedError,
)
from lucent.services.tool_runtimes.base import (
    ToolRuntimeError as RuntimeInvocationError,
)
from lucent.settings import custom_tooling_enabled

logger = get_logger("services.managed_tools")

_MCP_PROXY_CONFIG_KEY = "mcp_proxy"
_MCP_PROXY_DISCOVERY_PAYLOAD = {"_lucent_mcp_operation": "list_tools"}


class ManagedToolError(Exception):
    """Base error for managed tool execution failures."""


class ManagedToolBlockedError(ManagedToolError):
    """Raised when auth or policy blocks execution."""


class ManagedToolArgumentError(ManagedToolError):
    """Raised when ``arguments`` cannot be interpreted as a JSON object."""


class ManagedToolDefinitionError(ManagedToolError):
    """Raised when a stored definition is structurally unusable.

    Distinct from :class:`ManagedToolBlockedError`: nothing about the caller is
    wrong, the definition row itself cannot be read. Callers map this to a
    server-side error with the precise field named, never an opaque crash.
    """


def _entitlement_message(capability: str) -> str:
    """The refusal text naming the capability and the organization scope.

    Identical to what :class:`lucent.services.entitlements.CapabilityDisabledError`
    carries, so every envelope (MCP JSON, HTTP 403, sandbox failure detail)
    reports the same sentence.
    """
    from lucent.services.entitlements import CapabilityDisabledError

    return CapabilityDisabledError(capability, None).args[0]


# jsonb columns read by the executor. asyncpg returns them as text unless a
# codec is installed, and not every repository path that produces a definition
# row decodes them — so decode at this boundary, which every runtime shares.
_JSON_OBJECT_FIELDS = (
    "input_schema",
    "output_schema",
    "runtime_config",
    "env_vars",
    "auth_policy",
    "network_policy",
    "resource_limits",
)
_JSON_ARRAY_FIELDS = ("requirements",)


def _coerce_json_field(tool: dict[str, Any], field: str, expected: type) -> None:
    value = tool.get(field)
    if value is None or isinstance(value, expected):
        return
    label = "object" if expected is dict else "array"
    tool_name = tool.get("name") or tool.get("id") or "<unknown>"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ManagedToolDefinitionError(
                f"Managed tool {tool_name!r} has an unreadable {field}: "
                f"stored value is not valid JSON ({exc})"
            ) from exc
        if isinstance(parsed, expected):
            tool[field] = parsed
            return
    raise ManagedToolDefinitionError(
        f"Managed tool {tool_name!r} has an invalid {field}: "
        f"expected a JSON {label}, got {type(value).__name__}"
    )


def _normalize_tool_definition(tool: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a definition row with its jsonb fields decoded."""
    normalized = dict(tool)
    for field in _JSON_OBJECT_FIELDS:
        _coerce_json_field(normalized, field, dict)
    for field in _JSON_ARRAY_FIELDS:
        _coerce_json_field(normalized, field, list)
    return normalized


def _normalize_arguments(arguments: dict[str, Any] | str | None) -> dict[str, Any]:
    """Accept a mapping or a JSON-object string; reject anything else clearly."""
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ManagedToolArgumentError(f"arguments must be a JSON object: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ManagedToolArgumentError(
            f"arguments must be a JSON object, got {type(arguments).__name__}"
        )
    return arguments


@dataclass
class ManagedToolExecutionResult:
    """Structured result returned by the managed tool executor."""

    ok: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    sandbox_id: str | None = None
    duration_ms: int | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ok": self.ok,
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "sandbox_id": self.sandbox_id,
            "duration_ms": self.duration_ms,
            "run_id": self.run_id,
        }
        if self.error:
            data["error"] = self.error
        return data


class ManagedToolExecutor:
    """Execute approved managed tool definitions inside sandbox containers.

    The executor intentionally owns the security-sensitive path instead of
    letting generated tool code run as host subprocesses. Policy is enforced in
    this order:
    1. caller must be in the same org and able to access the tool definition;
    2. tool must be active;
    3. when invoked from an agent context, the tool must be granted to that agent;
    4. input/output schemas are validated outside the container;
    5. network/resource/secrets policy are applied before code execution.
    """

    def __init__(self, repo: DefinitionRepository):
        self.repo = repo

    async def execute(
        self,
        *,
        tool: dict[str, Any],
        arguments: dict[str, Any] | str | None,
        org_id: str,
        user_id: str,
        user_role: str | None = None,
        agent_id: str | None = None,
        enforce_agent_grant: bool = True,
    ) -> ManagedToolExecutionResult:
        """Execute a managed tool with arguments supplied by an agent or user."""
        return await self._execute(
            tool=tool,
            arguments=arguments,
            org_id=org_id,
            user_id=user_id,
            user_role=user_role,
            agent_id=agent_id,
            enforce_agent_grant=enforce_agent_grant,
            validate_input=True,
            validate_output=True,
        )

    async def discover_mcp_proxy_tools(
        self,
        *,
        tool: dict[str, Any],
        org_id: str,
        user_id: str,
        user_role: str | None = None,
        agent_id: str | None = None,
        enforce_agent_grant: bool = True,
    ) -> list[dict[str, Any]]:
        """Warm a managed MCP proxy and return its native tool descriptors."""
        runtime_config = tool.get("runtime_config") or {}
        if not runtime_config.get(_MCP_PROXY_CONFIG_KEY):
            raise ManagedToolBlockedError("Managed tool is not configured as an MCP proxy")

        result = await self._execute(
            tool=tool,
            arguments=_MCP_PROXY_DISCOVERY_PAYLOAD,
            org_id=org_id,
            user_id=user_id,
            user_role=user_role,
            agent_id=agent_id,
            enforce_agent_grant=enforce_agent_grant,
            validate_input=False,
            validate_output=False,
        )
        if not result.ok:
            raise ManagedToolError(result.error or "MCP proxy tool discovery failed")
        return normalize_discovered_mcp_tools(result.result)

    async def _execute(
        self,
        *,
        tool: dict[str, Any],
        arguments: dict[str, Any] | str | None,
        org_id: str,
        user_id: str,
        user_role: str | None = None,
        agent_id: str | None = None,
        enforce_agent_grant: bool = True,
        validate_input: bool,
        validate_output: bool,
    ) -> ManagedToolExecutionResult:
        # Definition rows arrive from several repository paths and asyncpg hands
        # jsonb back as text unless a codec is installed; decode once here, at
        # the boundary every runtime shares, so a raw row is a typed error
        # naming the field instead of an AttributeError on ``.get``.
        tool = _normalize_tool_definition(tool)
        # Gate 1, the managed_tools entitlement: an organization without the
        # row runs no managed tool at all. Checked here — in the code path the
        # invocation flows through — rather than in a router dependency,
        # because the executor is the single point every caller funnels into
        # (API run route, MCP run_managed_tool, MCP discovery, the LLM engine).
        # An unreachable pool denies rather than unlocking: the gate must be
        # able to prove the entitlement (services.entitlements.capability_is_granted).
        pool = getattr(self.repo, "pool", None)
        if not await capability_is_granted(pool, org_id, CAPABILITY_MANAGED_TOOLS):
            raise ManagedToolBlockedError(
                _entitlement_message(CAPABILITY_MANAGED_TOOLS)
            )
        # Which runtime kind executes this definition. Sandboxes are only
        # required for some kinds, so the feature gate has to be per-kind rather
        # than a blanket check: a deployment with sandboxes disabled can still
        # offer declarative HTTP and Lucent-native tools.
        runtime_type = str(tool.get("runtime_type") or "python").strip().lower()
        requires_sandbox = runtime_requires_sandbox(runtime_type)
        if requires_sandbox and not custom_tooling_enabled(organization_id=org_id):
            raise ManagedToolBlockedError(
                "Custom tooling is disabled for this organization because "
                "features.sandboxes_enabled is off"
            )
        # Gate 2, additive for sandbox-backed kinds only: a python tool runs in
        # a sandbox container, so an organization without the
        # custom_sandboxes entitlement cannot execute it even when its
        # managed_tools entitlement is present.
        if requires_sandbox and not await capability_is_granted(
            pool, org_id, CAPABILITY_SANDBOXES
        ):
            raise ManagedToolBlockedError(_entitlement_message(CAPABILITY_SANDBOXES))
        if tool.get("status") != "active":
            raise ManagedToolBlockedError("Managed tool is not active")

        auth_policy = tool.get("auth_policy") or {}
        allowed_roles = auth_policy.get("roles")
        if allowed_roles and user_role not in set(allowed_roles):
            raise ManagedToolBlockedError("Caller role is not allowed to use this managed tool")

        if agent_id and enforce_agent_grant:
            granted = await self.repo.is_managed_tool_granted_to_agent(agent_id, str(tool["id"]), org_id)
            if not granted:
                raise ManagedToolBlockedError("Managed tool is not granted to this agent")

        payload = _normalize_arguments(arguments)
        if validate_input:
            self._validate_input(tool, payload)

        run = await self.repo.create_managed_tool_run(
            tool_id=str(tool["id"]),
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            input_payload=payload,
        )
        run_id = str(run["id"])
        started = time.monotonic()
        # Wall-clock start for the usage ledger. `started` is monotonic and
        # cannot be turned into a timestamp; the two measure different things
        # (elapsed vs. when), and the ledger needs the latter.
        run_started_at = datetime.now(UTC)
        sandbox_id: str | None = None
        # Set by every completion path so the ledger write in `finally` records
        # the real result rather than guessing from elapsed time.
        run_outcome = "failed"

        try:
            # Authorization is complete: the caller may run this tool. Hand the
            # already-authorized request to the kind's runtime. A runtime never
            # re-derives permission and never sees the caller's credential store,
            # only the credential values this one call needs.
            runtime = get_runtime(runtime_type)
            context = RuntimeExecutionContext(
                org_id=org_id,
                user_id=user_id,
                user_role=user_role,
                agent_id=agent_id,
                run_id=run_id,
                arguments=payload,
                credentials=await self._resolve_credentials(
                    tool, org_id=org_id, user_id=user_id
                ),
                # `_tool` is the resolved definition the runtime needs for policy
                # (network allowlist, timeout) and for kind-specific config.
                runtime_config={**(tool.get("runtime_config") or {}), "_tool": tool},
            )
            outcome = await runtime.execute(context)
            sandbox_id = outcome.sandbox_id
            duration_ms = int((time.monotonic() - started) * 1000)

            if not outcome.ok:
                run_outcome = "failed"
                await self.repo.complete_managed_tool_run(
                    run_id,
                    status="failed",
                    output_payload=outcome.to_dict(),
                    error=outcome.error or "Tool execution failed",
                    sandbox_id=sandbox_id,
                    duration_ms=duration_ms,
                    org_id=org_id,
                )
                return ManagedToolExecutionResult(
                    ok=False,
                    stdout=outcome.stdout[:12000],
                    stderr=outcome.stderr[:12000],
                    error=outcome.error,
                    sandbox_id=sandbox_id,
                    duration_ms=duration_ms,
                    run_id=run_id,
                )

            result_value = outcome.result
            if validate_output:
                self._validate_output(tool, result_value)
            run_outcome = "completed"
            await self.repo.complete_managed_tool_run(
                run_id,
                status="completed",
                output_payload=outcome.to_dict(),
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                org_id=org_id,
            )
            return ManagedToolExecutionResult(
                ok=True,
                result=result_value,
                stdout=outcome.stdout[:12000],
                stderr=outcome.stderr[:12000],
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                run_id=run_id,
            )
        except RuntimePolicyBlockedError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            run_outcome = "blocked"
            await self.repo.complete_managed_tool_run(
                run_id,
                status="blocked",
                error=str(exc),
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                org_id=org_id,
            )
            raise ManagedToolBlockedError(str(exc)) from exc
        except RuntimeInvocationError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self.repo.complete_managed_tool_run(
                run_id,
                status="failed",
                error=str(exc),
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                org_id=org_id,
            )
            raise ManagedToolError(str(exc)) from exc
        except ManagedToolBlockedError:
            duration_ms = int((time.monotonic() - started) * 1000)
            run_outcome = "blocked"
            await self.repo.complete_managed_tool_run(
                run_id,
                status="blocked",
                error="Execution blocked by policy",
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                org_id=org_id,
            )
            raise
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self.repo.complete_managed_tool_run(
                run_id,
                status="failed",
                error=str(exc),
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                org_id=org_id,
            )
            raise
        finally:
            # Record the invocation (not the compute — that lives on the sandbox
            # row this run created) before the sandbox is torn down, so a final
            # invoice can still count tool calls after the tenant is offboarded.
            await self._record_run_usage(
                run_id=run_id,
                tool=tool,
                org_id=org_id,
                started_at=run_started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                outcome=run_outcome,
            )
            if sandbox_id:
                try:
                    await get_sandbox_manager().destroy(sandbox_id)
                except Exception:
                    logger.warning("Failed to destroy managed-tool sandbox %s", sandbox_id,
                                   exc_info=True)

    async def _record_run_usage(
        self,
        *,
        run_id: str,
        tool: dict[str, Any],
        org_id: str,
        started_at: datetime,
        duration_ms: int,
        outcome: str,
    ) -> None:
        """Write this invocation to the usage ledger. Best effort.

        The ledger records the *invocation* only; the CPU and memory for a tool
        run live on the sandbox row it created (``source='managed_tool'``), and
        duplicating them here would double-bill any tenant that sums compute.
        """
        try:
            from lucent.db import get_pool
            from lucent.services.usage import record_managed_tool_usage

            await record_managed_tool_usage(
                await get_pool(),
                managed_tool_run_id=run_id,
                tool_id=str(tool.get("id")) if tool.get("id") else None,
                organization_id=org_id,
                started_at=started_at,
                ended_at=started_at + timedelta(milliseconds=max(0, duration_ms)),
                outcome=outcome,
                task_id=tool.get("_task_id"),
            )
        except Exception:  # noqa: BLE001 — must never change how a run is reported
            logger.warning(
                "Failed to record usage for tool run %s", run_id, exc_info=True
            )

    def _validate_input(self, tool: dict[str, Any], payload: dict[str, Any]) -> None:
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        try:
            validate(instance=payload, schema=schema)
        except ValidationError as exc:
            raise ManagedToolBlockedError(
                f"Input does not match tool schema: {exc.message}"
            ) from exc

    def _validate_output(self, tool: dict[str, Any], value: Any) -> None:
        schema = tool.get("output_schema")
        if not schema:
            return
        try:
            validate(instance=value, schema=schema)
        except ValidationError as exc:
            raise ManagedToolError(f"Output does not match tool schema: {exc.message}") from exc

    async def _resolve_credentials(
        self, tool: dict[str, Any], *, org_id: str, user_id: str
    ) -> dict[str, str]:
        """Resolve this tool's ``env_vars`` into the values its runtime may use.

        Only sensitive-looking keys are required by the repository to hold a
        `secret://` or `credential://` reference, but every value is resolved the
        same way so a runtime never has to distinguish "literal" from "reference".

        Credential references are user-scoped, so the current user is set for the
        duration of resolution and cleared immediately after — resolution must
        never be able to read another user's secret because of leaked context.
        """
        env_vars = tool.get("env_vars") or {}
        if not isinstance(env_vars, dict) or not env_vars:
            return {}
        provider = SecretRegistry.get()
        set_current_user(
            {"id": user_id, "organization_id": org_id, "role": "member"}
        )
        try:
            return dict(await resolve_env_vars(env_vars, provider))
        finally:
            set_current_user(None)

def normalize_discovered_mcp_tools(value: Any) -> list[dict[str, Any]]:
    """Validate proxy discovery output before it becomes a model tool schema."""
    if not isinstance(value, dict) or not isinstance(value.get("tools"), list):
        raise ManagedToolError("MCP proxy discovery did not return a tools list")

    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in value["tools"]:
        if not isinstance(item, dict):
            raise ManagedToolError("MCP proxy discovery returned an invalid tool descriptor")
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 128:
            raise ManagedToolError("MCP proxy discovery returned a tool with an invalid name")
        if name in seen_names:
            raise ManagedToolError(f"MCP proxy discovery returned duplicate tool '{name}'")
        seen_names.add(name)

        description = item.get("description")
        input_schema = item.get("input_schema") or item.get("inputSchema") or {
            "type": "object", "properties": {},
        }
        if not isinstance(description, str) or not isinstance(input_schema, dict):
            raise ManagedToolError(f"MCP proxy discovery returned an invalid descriptor for '{name}'")
        normalized.append({
            "name": name,
            "description": description,
            "input_schema": input_schema,
        })
    return normalized
