"""Execution runtimes for managed tool definitions.

A managed tool is a *declaration* with a security envelope:

* who owns it (``owner_user_id`` / ``owner_group_id`` / ``scope``),
* who may call it (capability grant on an agent, per-user consent, ``auth_policy``),
* what the model sees (``input_schema`` / ``output_schema``),
* what it may touch (``env_vars`` references, ``network_policy``,
  ``resource_limits``, ``timeout_seconds``).

That envelope is deliberately **kind-agnostic**. Only the last step — actually
producing a result — differs between a sandboxed Python entrypoint, a
declarative HTTPS call, an OpenAPI operation, and a Lucent-native handler.

This package therefore splits execution in two:

* :class:`~lucent.services.managed_tools.ManagedToolExecutor` keeps every
  authorization, consent, schema, and ledger decision in one place, exactly as
  it did when ``python`` was the only kind.
* A :class:`ToolRuntime` is a small, side-effect-scoped executor that is handed
  an already-authorized request and returns a result. Runtimes must never widen
  their own permissions: they receive resolved credentials and an already
  enforced network policy, and they re-validate nothing about the caller.

Adding a runtime kind is consequently a matter of registering a handler that
respects the contract in this module — not of adding a second permission model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "RuntimeExecutionContext",
    "RuntimeExecutionResult",
    "ToolRuntime",
    "ToolRuntimeError",
    "ToolRuntimeBlockedError",
]


class ToolRuntimeError(Exception):
    """A runtime failed while executing an authorized request."""


class ToolRuntimeBlockedError(ToolRuntimeError):
    """A runtime refused the request on policy grounds.

    Distinct from a plain failure because the executor records it with the
    ``blocked`` outcome and surfaces it as a 403-class error rather than a 500.
    """


@dataclass
class RuntimeExecutionContext:
    """Everything a runtime needs, already authorized and resolved.

    ``arguments`` have passed ``input_schema`` validation. ``credentials`` holds
    only the values this specific call needs, resolved just in time from
    ``secret://`` / ``credential://`` references; a runtime never sees the raw
    reference strings and never touches the secret store itself.
    """

    org_id: str
    user_id: str
    user_role: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    # Resolved runtime_config with credential references *not* yet substituted
    # into URLs/headers; the runtime performs substitution so it can validate
    # the substituted value (header injection, host allowlist) first.
    runtime_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeExecutionResult:
    """What a runtime reports back to the executor."""

    ok: bool = True
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    # Only sandbox-backed runtimes set this; the executor tears it down.
    sandbox_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ok": self.ok,
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "sandbox_id": self.sandbox_id,
        }
        if self.error:
            data["error"] = self.error
        return data


@runtime_checkable
class ToolRuntime(Protocol):
    """An executor for one runtime kind.

    Implementations are stateless and safe to share across calls. They must be
    side-effect free with respect to the database: the executor owns run
    records, ledger rows, and audit entries.
    """

    #: Value stored in ``managed_tool_definitions.runtime_type``.
    runtime_type: str

    #: Whether a deployment must have sandboxes enabled to run this kind.
    requires_sandbox: bool

    async def execute(self, context: RuntimeExecutionContext) -> RuntimeExecutionResult:
        """Run the tool. Raise :class:`ToolRuntimeBlockedError` to deny."""
        ...
