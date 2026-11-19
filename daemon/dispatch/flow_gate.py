"""Shared flow-gate mechanism for structured task results.

A task can act as a gate for the rest of its request's flow. The gate is
declared inside ``output_contract``::

    "output_contract": {
        "json_schema": {...},
        "gate": {
            "path": "should_act",      # dotted path into result_structured
            "stop_value": false,       # the value that means "stop the flow"
            "action": "stop_flow"      # optional; stop_flow is the default
        }
    }

Only two entry points exist, so every kind that can produce a structured
result shares one implementation instead of forking the decision per kind:

* :func:`evaluate_gate` turns a validated result into a :class:`GateOutcome`.
* :func:`propagate_flow_stop` short-circuits the remaining tasks of the
  request and records the decision on the gating task's event trail.

A gate stop is a successful decision, not an error: the gating task completes
normally with the outcome in its summary, and downstream pending/planned tasks
become ``short_circuited`` — never ``cancelled``, because under a strict
dependency policy a cancelled level has no completed row and blocks every
later stage forever. The post-completion review task is never short-circuited
here; it must still run, and its prompt explains the gated flow so the request
closes green.

When the gate evaluates true, nothing is recorded and nothing is propagated:
the completion is byte-identical to the pre-gate behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from daemon.runtime.module_proxy import runtime

# One vocabulary shared with the authoring surfaces (lucent.flow_gate) so what
# is accepted at creation is exactly what this runtime can evaluate.
from lucent.flow_gate import (
    FLOW_STOP_ACTION,
    GATE_ACTIONS,
    GATE_CONTRACT_KEY,
)


_GATE_EVENT_TYPE = "flow_gate_stopped"


@dataclass(slots=True)
class GateOutcome:
    """What a gate specification decided for one task's terminal result."""

    configured: bool = False
    evaluated: bool = False
    stop: bool = False
    action: str = FLOW_STOP_ACTION
    path: str | None = None
    value: Any = None
    stop_value: Any = None
    reason: str | None = None

    @property
    def decision(self) -> str:
        """The recorded decision: the stop action, or ``continue``."""
        return self.action if self.stop else "continue"

    def summary(self, existing: str | None = None) -> str:
        """Terminal summary for the gating task (prepended to any existing one)."""
        if not self.stop:
            # A passing gate must leave the completion text byte-identical.
            return existing or ""
        if self.evaluated:
            note = (
                f"Flow gate stopped the flow ({self.action}): "
                f"'{self.path}' resolved to {self.value!r}, the configured "
                f"stop value ({self.stop_value!r}). Remaining tasks were "
                "short-circuited and no downstream action was taken."
            )
        else:
            note = (
                f"Flow gate stopped the flow ({self.action}): {self.reason}. "
                "Remaining tasks were short-circuited and no downstream action "
                "was taken."
            )
        if existing:
            return f"{note}\n{existing}"
        return note

    def detail(self) -> str:
        """Human-readable event text: the value seen and the decision."""
        if self.evaluated:
            seen = (
                f"'{self.path}' resolved to {self.value!r} "
                f"(stop value {self.stop_value!r})"
            )
        else:
            seen = self.reason or "gate could not be evaluated"
        return f"Flow gate decision: {self.decision} — {seen}."

    def metadata(self) -> dict[str, Any]:
        return {
            "gate_configured": self.configured,
            "gate_evaluated": self.evaluated,
            "gate_path": self.path,
            "gate_value": self.value,
            "gate_stop_value": self.stop_value,
            "gate_action": self.action,
            "gate_decision": self.decision,
            "gate_reason": self.reason,
        }


def evaluate_gate(
    task: Mapping[str, Any],
    result_structured: Mapping[str, Any] | None,
    *,
    spec: Mapping[str, Any] | None = None,
) -> GateOutcome:
    """Evaluate a task's gate against its validated structured result.

    The specification is read from ``task['output_contract']['gate']``. A
    caller whose configuration lives elsewhere (the decision kind's
    ``threshold_policy``) passes ``spec`` explicitly, so the path resolution and
    comparison stay in this one implementation.

    A configured gate that cannot be evaluated fails closed (``stop=True``)
    rather than silently letting the flow continue: a gate that never evaluates
    is worse than no gate.
    """
    if spec is None:
        contract = task.get("output_contract")
        if isinstance(contract, str):
            try:
                contract = json.loads(contract)
            except (TypeError, ValueError):
                contract = None
        if isinstance(contract, Mapping):
            spec = contract.get(GATE_CONTRACT_KEY)
    if not isinstance(spec, Mapping):
        return GateOutcome()

    raw_path = spec.get("path")
    action = str(spec.get("action") or FLOW_STOP_ACTION)
    path = raw_path.strip() if isinstance(raw_path, str) else ""
    if not path:
        return GateOutcome(
            configured=True,
            stop=True,
            action=action,
            reason="gate path is missing or is not a non-empty string",
        )
    if action not in GATE_ACTIONS:
        accepted = ", ".join(GATE_ACTIONS)
        return GateOutcome(
            configured=True,
            stop=True,
            action=action,
            path=path,
            reason=f"unsupported gate action {action!r} (accepted: {accepted})",
        )
    if not isinstance(spec.get("stop_value"), bool):
        return GateOutcome(
            configured=True,
            stop=True,
            action=action,
            path=path,
            reason="gate stop_value must be a boolean",
        )
    stop_value = spec["stop_value"]

    if not isinstance(result_structured, Mapping):
        return GateOutcome(
            configured=True,
            stop=True,
            action=action,
            path=path,
            stop_value=stop_value,
            reason="the terminal result has no structured payload to evaluate",
        )

    # Dotted lookup; numeric segments index lists as well as mapping keys.
    cursor: Any = result_structured
    for segment in path.split("."):
        if isinstance(cursor, Mapping) and segment in cursor:
            cursor = cursor[segment]
        elif isinstance(cursor, (list, tuple)) and segment.isdigit():
            index = int(segment)
            if index >= len(cursor):
                return GateOutcome(
                    configured=True,
                    stop=True,
                    action=action,
                    path=path,
                    stop_value=stop_value,
                    reason=f"gate path {path!r} does not exist in the result",
                )
            cursor = cursor[index]
        else:
            return GateOutcome(
                configured=True,
                stop=True,
                action=action,
                path=path,
                stop_value=stop_value,
                reason=f"gate path {path!r} does not exist in the result",
            )

    # The specification declares the boolean value that means stop, so the
    # resolved value is compared by truthiness: a gate path of
    # ``"threshold_failed"`` with ``stop_value=True`` stops on a non-empty
    # failure list, while a script gate of ``"should_act"`` stops on False.
    return GateOutcome(
        configured=True,
        evaluated=True,
        stop=bool(cursor) is stop_value,
        action=action,
        path=path,
        value=cursor,
        stop_value=stop_value,
    )


async def propagate_flow_stop(
    *,
    task: Mapping[str, Any],
    outcome: GateOutcome,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """Short-circuit every remaining pending/planned task in the request.

    Returns the settled task rows (``id``/``title``/``sequence_order``). The
    gating task is skipped by construction because it is already completed,
    and the post-completion review task is exempt server-side so review still
    runs. One event on the gating task records the value seen, the decision,
    and exactly which tasks were short-circuited.
    """
    if not outcome.stop:
        return []

    task_id = str(task.get("id") or "")
    request_id = request_id or str(task.get("request_id") or "")
    if not task_id:
        runtime.log(
            "Flow gate: cannot propagate a flow stop without a gating task id",
            "ERROR",
        )
        return []

    settled = await runtime.RequestAPI.short_circuit_pending_tasks(
        task_id,
        reason=outcome.detail(),
        metadata=outcome.metadata(),
    )
    if settled is None:
        # The decision itself is already recorded on the task's summary; make
        # the propagation failure loud rather than pretending the flow stopped.
        await runtime.RequestAPI.add_event(
            task_id,
            "flow_gate_propagation_failed",
            "Flow gate decided to stop, but the remaining tasks could not be "
            "short-circuited.",
            outcome.metadata(),
        )
        return []

    short_circuited = list(settled)
    titles = [str(item.get("title") or item.get("id") or "?") for item in short_circuited]
    detail = outcome.detail()
    if short_circuited:
        detail += (
            f" Short-circuited {len(short_circuited)} remaining task(s): "
            + ", ".join(titles)
            + "."
        )
    else:
        detail += " No remaining pending or planned tasks needed short-circuiting."
    metadata = {
        **outcome.metadata(),
        "short_circuited_count": len(short_circuited),
        "short_circuited_task_ids": [str(item.get("id") or "") for item in short_circuited],
        "short_circuited_titles": titles,
        "request_id": request_id,
    }
    await runtime.RequestAPI.add_event(
        task_id, _GATE_EVENT_TYPE, detail, metadata
    )
    return short_circuited
