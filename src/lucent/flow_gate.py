"""Authoring-side vocabulary for flow-gate specifications.

A flow gate is declared inside ``output_contract``::

    "output_contract": {
        "json_schema": {...},
        "gate": {
            "path": "should_act",      # dotted path into result_structured
            "stop_value": false,       # the value that means "stop the flow"
            "action": "stop_flow"      # optional; stop_flow is the default
        }
    }

The runtime that evaluates a gate lives in ``daemon.dispatch.flow_gate``. This
module owns the *authoring* answer — what a gate specification may contain and
which task kinds may carry one — so every creation surface (repository,
REST, MCP, workflow/schedule actions) rejects the same malformed payload with
the same message instead of storing a gate that will never evaluate.

A configured gate that cannot be evaluated fails closed at runtime. Storing
one on a kind that cannot produce a structured result is the authoring-time
version of the same defect: the gate never evaluates, and a gate that never
evaluates is worse than no gate.
"""

from __future__ import annotations

from typing import Any, Mapping

# Key inside ``output_contract`` that carries the gate specification.
GATE_CONTRACT_KEY = "gate"

# The only action this mechanism implements. Runtime treats an unknown action
# as unevaluable (fail closed); authoring rejects it outright.
FLOW_STOP_ACTION = "stop_flow"
GATE_ACTIONS: tuple[str, ...] = (FLOW_STOP_ACTION,)


def validate_gate_spec(spec: Any) -> None:
    """Validate one gate specification, raising ``ValueError`` if malformed.

    The field checks mirror exactly what ``evaluate_gate`` needs to resolve a
    value, so a stored spec can never be silently ignored:

    * ``path`` must be a non-empty string (a dotted path into the result);
    * ``stop_value`` must be a boolean (the value that means "stop");
    * ``action``, when given, must be one of :data:`GATE_ACTIONS`.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("output_contract.gate must be an object")

    path = spec.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            "output_contract.gate.path must be a non-empty string"
        )

    if not isinstance(spec.get("stop_value"), bool):
        raise ValueError(
            "output_contract.gate.stop_value must be a boolean"
        )

    action = spec.get("action")
    if action is None:
        action = FLOW_STOP_ACTION
    if action not in GATE_ACTIONS:
        accepted = ", ".join(GATE_ACTIONS)
        raise ValueError(
            f"Invalid output_contract.gate.action {action!r}. "
            f"Must be one of: {accepted}"
        )


def validate_contract_gate(
    output_contract: Any,
    *,
    kind: str | None = None,
) -> None:
    """Validate the gate embedded in an output contract, if there is one.

    ``kind`` is the resolved task kind. When provided and that kind cannot
    produce a structured result, a gate is refused: the contract can never be
    satisfied, so the gate would never evaluate. Passing ``kind=None`` checks
    only the specification shape (used where the kind is validated separately).
    """
    if not isinstance(output_contract, Mapping):
        # Shape errors (a non-object contract) belong to the contract
        # validator; there is no gate to inspect here.
        return
    spec = output_contract.get(GATE_CONTRACT_KEY)
    if spec is None:
        return

    validate_gate_spec(spec)

    if kind is not None:
        # Imported lazily: task_kinds imports the repository validators, and
        # this module is imported by the repository.
        from lucent.task_kinds import task_kind_spec

        if not task_kind_spec(kind).output_contract_allowed:
            raise ValueError(
                f"Task kind '{kind}' cannot produce a structured result, so "
                "it cannot carry an output_contract.gate — a gate that never "
                "evaluates is worse than no gate"
            )
