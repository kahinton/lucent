"""Run a Lucent agent session on a remote host through its own engine stack.

A remote is a machine Lucent does not control; the shipped client
(``scripts/lucent_remote_agent.py``) is stdlib-only and can only run commands.
That is the right default — a remote is untrusted hardware, so nothing about
Lucent's model credentials, tool surface, or prompt composition should be
exposed to it without an explicit, owner-level opt-in.

This module is that opt-in. It is the *remote side* of an "engine" executor:
the agent client calls :func:`run_task_session` in-process on the remote host
instead of shelling out. Everything Lucent-specific arrives in the claim
payload as a self-contained *execution plan* so the client needs no database,
no Lucent credentials beyond the task-scoped key it already holds, and no
knowledge of Lucent internals:

* the engine name and a backend descriptor (provider, base URL),
* the composed system prompt (agent definition + skills + managed tools +
  the same task framing the daemon produces),
* an MCP server map pointing at Lucent's own ``/mcp`` endpoint with a scoped
  bearer token,

The plan is built controller-side by :func:`build_execution_plan` so the
remote never chooses its own agent identity, model routing, or tool surface —
it executes what the controller decided. That keeps a compromised remote from
escalating: the worst it can do is misuse the scoped key it was already given,
which is restricted to one task and one user's memory scope.

Security boundary
-----------------
The engine runs with ``approve_permissions=True`` because a remote has no human
to approve a tool call, exactly as a daemon task does. That makes the remote's
own filesystem reachable by the model — which is the documented, deliberate
trade of running work on someone's machine at all. It is not a sandbox, and
``docs/remotes.md`` says so.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

TASK_SESSION_KIND = "task-session"
ENGINE_EXECUTOR = "engine"

#: Engines a remote may run. A remote session is unattended, so only engines
#: that bring their own agent loop and tool execution are useful here; the
#: general LLM layer has no file/shell access of its own.
REMOTE_ENGINES = frozenset({"copilot", "claude_code", "codex"})

#: Permission modes an unattended remote session may use.
#:
#: ``bypassPermissions`` is the default because a remote has no human to answer
#: a prompt: any mode that can block turns "the model wanted to run sw_vers" into
#: a denied tool call, and the task quietly does less than asked. That is the
#: same trust level the ``shell`` executor already carries (it runs code the task
#: authored), and the controller additionally requires an explicit
#: ``features.remote_engine_session_enabled`` opt-in before composing a session.
#: An operator who wants a narrower grant can set ``LUCENT_REMOTE_PERMISSION_MODE``
#: on the remote host.
DEFAULT_REMOTE_PERMISSION_MODE = "bypassPermissions"
REMOTE_PERMISSION_MODES = frozenset(
    {"bypassPermissions", "acceptEdits", "default", "plan", "dontAsk", "auto"}
)


class RemoteSessionError(RuntimeError):
    """The execution plan is missing or malformed."""


@dataclass(frozen=True)
class SessionPlan:
    """Everything a remote needs to run one agent session, and nothing more."""

    engine: str
    model: str
    system_message: str
    prompt: str
    backend: dict[str, Any] | None = None
    mcp_config: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None
    timeout: int = 3600
    task_id: str | None = None
    request_id: str | None = None
    approval_mode: str = "unattended"

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the claim response."""
        return {
            "kind": TASK_SESSION_KIND,
            "engine": self.engine,
            "model": self.model,
            "system_message": self.system_message,
            "prompt": self.prompt,
            "backend": self.backend,
            "mcp_config": self.mcp_config,
            "reasoning_effort": self.reasoning_effort,
            "timeout": self.timeout,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "approval_mode": self.approval_mode,
        }


def plan_from_payload(payload: Any) -> SessionPlan:
    """Parse a plan out of a claim payload, failing closed on anything odd.

    A remote treats the payload as untrusted input even though it comes from
    its own controller: a corrupted or truncated claim must not silently run a
    half-built session.
    """
    if not isinstance(payload, dict):
        raise RemoteSessionError("execution plan must be an object")
    if payload.get("kind") != TASK_SESSION_KIND:
        raise RemoteSessionError(
            f"unsupported execution plan kind {payload.get('kind')!r}"
        )

    engine = str(payload.get("engine") or "").strip().lower()
    if engine not in REMOTE_ENGINES:
        raise RemoteSessionError(
            f"engine {engine!r} cannot run on a remote; supported: "
            f"{sorted(REMOTE_ENGINES)}"
        )
    model = str(payload.get("model") or "").strip()
    if not model:
        raise RemoteSessionError("execution plan is missing a model")

    mcp_config = payload.get("mcp_config") or {}
    if not isinstance(mcp_config, dict):
        raise RemoteSessionError("mcp_config must be an object")

    backend = payload.get("backend")
    if backend is not None and not isinstance(backend, dict):
        raise RemoteSessionError("backend must be an object")

    try:
        timeout = int(payload.get("timeout") or 3600)
    except (TypeError, ValueError):
        timeout = 3600

    return SessionPlan(
        engine=engine,
        model=model,
        system_message=str(payload.get("system_message") or ""),
        prompt=str(payload.get("prompt") or ""),
        backend=backend,
        mcp_config=mcp_config,
        reasoning_effort=payload.get("reasoning_effort") or None,
        timeout=max(60, min(timeout, 24 * 3600)),
        task_id=str(payload.get("task_id") or "") or None,
        request_id=str(payload.get("request_id") or "") or None,
        approval_mode=str(payload.get("approval_mode") or "unattended"),
    )


async def run_task_session(plan: SessionPlan) -> str:
    """Run one agent session on this host and return its final text.

    The engine stack is imported lazily so a remote that only runs ``shell``
    tasks never pays for (or needs) the coding-engine dependencies.
    """
    import os

    from lucent.llm.engine_registry import get_engine

    engine = get_engine(plan.engine)
    permission_mode = (
        os.environ.get("LUCENT_REMOTE_PERMISSION_MODE", "").strip()
        or DEFAULT_REMOTE_PERMISSION_MODE
    )
    if permission_mode not in REMOTE_PERMISSION_MODES:
        permission_mode = DEFAULT_REMOTE_PERMISSION_MODE
    audit_context: dict[str, Any] = {
        "source": "remote.session",
        "task_id": plan.task_id,
        "request_id": plan.request_id,
        "engine": plan.engine,
        "model": plan.model,
        # A remote session is unattended: nobody is at that machine to answer a
        # permission prompt, so a mode that can block would silently turn the
        # model's intended work into denied tool calls.
        "permission_mode": permission_mode,
        # The controller already resolved this backend; passing it through
        # keeps provider injection (e.g. a local Ollama server) working on a
        # host that has no database to look it up in.
        "engine_backend": plan.backend,
    }
    result = await engine.run_session(
        model=plan.model,
        system_message=plan.system_message,
        prompt=plan.prompt,
        mcp_config=plan.mcp_config or None,
        timeout=plan.timeout,
        reasoning_effort=plan.reasoning_effort,
        audit_context=audit_context,
        approve_permissions=True,
    )
    return result or ""


# ── Controller side: build the plan the remote executes ────────────────────


def encode_plan_for_transport(plan: SessionPlan) -> dict[str, Any]:
    """Return a compact, transport-safe plan object.

    Kept as a dict (not a base64 blob) so a human reading a claim payload in the
    logs can see which model and engine a task will use.
    """
    payload = plan.to_payload()
    # The system prompt is large and contains an agent definition; base64 keeps
    # it from being mangled by any intermediate text handling while staying a
    # plain string field.
    payload["system_message_b64"] = base64.b64encode(
        plan.system_message.encode("utf-8")
    ).decode("ascii")
    return payload


def decode_plan_from_transport(payload: dict[str, Any]) -> SessionPlan:
    """Inverse of :func:`encode_plan_for_transport`."""
    encoded = payload.get("system_message_b64")
    if isinstance(encoded, str) and encoded:
        try:
            decoded = base64.b64decode(encoded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteSessionError(f"malformed system_message_b64: {exc}") from exc
        payload = {**payload, "system_message": decoded}
    return plan_from_payload(payload)


__all__ = [
    "ENGINE_EXECUTOR",
    "REMOTE_ENGINES",
    "RemoteSessionError",
    "SessionPlan",
    "TASK_SESSION_KIND",
    "decode_plan_from_transport",
    "encode_plan_for_transport",
    "plan_from_payload",
    "run_task_session",
]
