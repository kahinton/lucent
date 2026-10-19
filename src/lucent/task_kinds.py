"""Task-kind registry: what a task *is*, independent of who runs it.

A task has two orthogonal axes:

* ``agent_type`` / ``agent_definition_id`` — *who* does the work (persona,
  skills, tools). Only meaningful for the ``agent`` kind.
* ``task_kind`` + ``execution_config`` — *how* the work executes (an agent
  session, a decision-model call, a fusion run, and in future a command or
  webhook).

Historically the kind was inferred from the presence of ``decision_config`` /
``fusion_config`` and a magic ``agent_type='decision-step'``. That convention is
still accepted for backward compatibility, but every new kind should be a
:class:`TaskKindSpec` registered here. Creation surfaces validate through this
module and ``list_task_kinds`` projects it to agents, so adding a kind does not
require teaching every surface a new field.

The registry deliberately describes **capabilities and config**, not the
executor implementation. Dispatch lives in the daemon; a future executor
registers the same kind and consumes the normalized config. See
``docs/task-kinds-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from lucent.db.requests import validate_decision_config


class UnknownTaskKindError(ValueError):
    """Raised when a task names a kind that is not registered."""


@dataclass(frozen=True, slots=True)
class TaskKindSpec:
    """Declarative description of one task execution kind."""

    kind: str
    display_name: str
    description: str
    config_field: str | None = None
    """Legacy/compat argument name for the kind's config (e.g. decision_config).

    New kinds should use the generic ``execution_config`` field; this is kept
    so existing callers and prompts continue to work byte-for-byte.
    """
    config_schema: dict[str, Any] | None = None
    config_required: bool = False
    aliases: tuple[str, ...] = ()
    requires_agent_definition: bool = True
    agent_type_const: str | None = None
    model_class: str | None = "chat"  # "chat" | "decision" | None
    model_requires_tools: bool = True
    model_auto_allowed: bool = True
    backend_allowed: bool = True
    output_contract_allowed: bool = True
    sandbox: str = "optional"  # "optional" | "forbidden" | "required"
    examples: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    validate: Callable[[dict[str, Any] | None], None] | None = None

    def as_catalog_entry(self) -> dict[str, Any]:
        """Project the spec to the JSON shape returned by ``list_task_kinds``."""
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "description": self.description,
            "aliases": list(self.aliases),
            "config_field": self.config_field,
            "config_required": self.config_required,
            "config_schema": self.config_schema,
            "requirements": {
                "agent_definition": self.requires_agent_definition,
                "agent_type_const": self.agent_type_const,
                "model_class": self.model_class,
                "model_requires_tools": self.model_requires_tools,
                "model_auto_allowed": self.model_auto_allowed,
                "backend_allowed": self.backend_allowed,
                "output_contract_allowed": self.output_contract_allowed,
                "sandbox": self.sandbox,
            },
            "examples": list(self.examples),
        }


_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "required": ["name", "type", "instructions"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"enum": ["noul", "choice", "score"]},
                    "instructions": {"type": "string"},
                    "criteria": {},
                },
            },
        },
        "state": {"type": "object"},
        "model": {"type": "string"},
        "threshold_policy": {"type": "object"},
    },
}

_FUSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["mode"],
    "properties": {
        "mode": {"enum": ["best_of_n", "synthesize"]},
        "candidates": {"type": "object"},
        "arbiter": {"type": "object"},
        "synthesis": {"type": "object"},
        "refine": {"type": "object"},
    },
}

_TASK_KINDS: dict[str, TaskKindSpec] = {}
_TASK_KIND_ALIASES: dict[str, str] = {}


def register_task_kind(spec: TaskKindSpec) -> None:
    """Register a task kind (and its aliases). Last registration wins."""
    if not spec.kind or spec.kind != spec.kind.strip().lower():
        raise ValueError("Task kind names must be non-empty lowercase strings")
    _TASK_KINDS[spec.kind] = spec
    for alias in spec.aliases:
        _TASK_KIND_ALIASES[alias.strip().lower()] = spec.kind


def _normalize(kind: str | None) -> str:
    name = (kind or "").strip().lower()
    return _TASK_KIND_ALIASES.get(name, name)


def task_kind_names() -> list[str]:
    """Registered canonical kind names, in registration order."""
    return list(_TASK_KINDS)


def task_kind_spec(kind: str | None) -> TaskKindSpec:
    """Look up a registered kind, resolving aliases."""
    normalized = _normalize(kind)
    spec = _TASK_KINDS.get(normalized)
    if spec is None:
        known = ", ".join(sorted(_TASK_KINDS)) or "none"
        raise UnknownTaskKindError(
            f"Unknown task kind {kind!r}. Registered kinds: {known}."
        )
    return spec


def resolve_task_kind(
    *,
    kind: str | None = None,
    agent_type: str | None = None,
    decision_config: dict[str, Any] | None = None,
    fusion_config: dict[str, Any] | None = None,
) -> str:
    """Resolve the kind for a task, including legacy inference.

    Precedence: explicit ``kind`` → ``decision_config`` or the legacy
    ``decision-step`` agent sentinel → ``fusion_config`` → ``agent``.
    """
    normalized = _normalize(kind)
    if normalized:
        return task_kind_spec(normalized).kind
    if decision_config or (agent_type or "").strip().lower() == "decision-step":
        return "decision"
    if fusion_config:
        return "fusion"
    return "agent"


def task_kind_catalog() -> list[dict[str, Any]]:
    """JSON-safe projection of every registered kind."""
    return [spec.as_catalog_entry() for spec in _TASK_KINDS.values()]


def validate_task_kind_payload(
    kind: str,
    config: dict[str, Any] | None,
) -> None:
    """Validate one kind's config (requiredness + kind-specific rules)."""
    spec = task_kind_spec(kind)
    if spec.config_required and not config:
        raise ValueError(
            f"Task kind '{spec.kind}' requires {spec.config_field or 'execution_config'}"
        )
    if spec.config_field is None and config is not None:
        raise ValueError(
            f"Task kind '{spec.kind}' does not accept execution_config"
        )
    if spec.validate is not None:
        spec.validate(config)


# ── Built-in kinds ─────────────────────────────────────────────────────────


register_task_kind(
    TaskKindSpec(
        kind="agent",
        display_name="Agent session",
        description=(
            "Run an approved agent definition in a chat session with MCP tools. "
            "This is the default kind."
        ),
        config_field=None,
        requires_agent_definition=True,
        model_class="chat",
        model_requires_tools=True,
        model_auto_allowed=True,
        backend_allowed=True,
        output_contract_allowed=True,
        sandbox="optional",
        examples=(
            {
                "agent_type": "code",
                "model": "<optional chat model>",
                "backend_id": "<optional coding backend>",
            },
        ),
    )
)

register_task_kind(
    TaskKindSpec(
        kind="decision",
        display_name="Decision step",
        description=(
            "Answer a typed questions list with one System One decision-model "
            "call. No chat session, engine, sandbox, or backend."
        ),
        config_field="decision_config",
        config_schema=_QUESTION_SCHEMA,
        config_required=True,
        aliases=("decision-step",),
        requires_agent_definition=True,
        agent_type_const="decision-step",
        model_class="decision",
        model_requires_tools=False,
        model_auto_allowed=False,
        backend_allowed=False,
        output_contract_allowed=False,
        sandbox="forbidden",
        examples=(
            {
                "agent_type": "decision-step",
                "decision_config": {
                    "questions": [
                        {
                            "name": "ready",
                            "type": "noul",
                            "instructions": "All blocking checks pass.",
                        }
                    ],
                    "threshold_policy": {
                        "questions": {"ready": {"min": 0.8}},
                        "on_fail": "needs_review",
                    },
                },
            },
        ),
        validate=validate_decision_config,
    )
)

register_task_kind(
    TaskKindSpec(
        kind="fusion",
        display_name="Fusion",
        description=(
            "Run several candidate chat models and settle on the best answer "
            "(best_of_n) or a synthesized answer, using a decision-model arbiter."
        ),
        config_field="fusion_config",
        config_schema=_FUSION_SCHEMA,
        config_required=True,
        requires_agent_definition=True,
        model_class="chat",
        model_requires_tools=True,
        model_auto_allowed=True,
        backend_allowed=True,
        output_contract_allowed=True,
        sandbox="forbidden",
        examples=(
            {
                "fusion_config": {
                    "mode": "best_of_n",
                    "candidates": {"models": ["<chat-model>"]},
                },
            },
        ),
    )
)
