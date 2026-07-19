"""Model routing and completion requirements for dispatched daemon tasks."""

from __future__ import annotations

from daemon.observability.tools import (
    _HANDOFF_TOOL_REQUIRED_PATTERNS,
    _HANDOFF_TOOL_REQUIRED_SIGNALS,
)


class TaskValidationMixin:
    """Completion validation policy composed into the daemon dispatcher."""

    def _validate_task_result(
        self,
        result: str,
        *,
        task: dict | None = None,
        tool_counts: dict[str, int] | None = None,
    ) -> tuple[bool, str]:
        from daemon.validation.output import validate_consolidation_execution

        if not result:
            return False, "no output"
        if task:
            valid, reason = validate_consolidation_execution(
                result_text=result,
                task_title=str(task.get("title", "")),
                task_description=str(task.get("description", "")),
                tool_counts=tool_counts,
            )
            if not valid:
                return False, reason
        stripped = result.strip()
        if len(stripped) < 100:
            return False, f"output too short ({len(stripped)} chars)"
        lowered = stripped.lower()
        blocking = (
            "status: blocked", "blocked — missing required tooling",
            "blocked - missing required tooling", "missing required tooling",
            "cannot complete this task", "i cannot complete this task",
            "unable to complete this task", "no github mcp tool is available",
            "neither is exposed to this sub-agent", "tooling is not available",
            "permission response",
        )
        if any(indicator in lowered for indicator in blocking):
            return False, "output reports task is blocked or missing required tooling"
        if len(stripped) >= 1000:
            return True, "ok"
        failures = (
            "couldn't find", "could not find", "unable to", "failed to",
            "i don't have", "i do not have", "no context", "cannot complete",
            "couldn't complete", "could not complete", "task not completed",
            "error occurred", "exception occurred",
        )
        for indicator in failures:
            if indicator in lowered:
                return False, f"failure indicator found: '{indicator}'"
        return True, "ok"


def resolve_default_model(preferred_model: str | None = None) -> str:
    """Resolve an enabled default model without inventing a fallback."""
    from daemon.runtime.module_proxy import runtime
    from lucent.model_registry import get_default_model_id

    return get_default_model_id(
        preferred_model=(preferred_model or runtime.MODEL or None)
    )


def select_model_for_task(
    *,
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    explicit_model: str | None = None,
) -> tuple[str, str]:
    """Select a tool-capable model and reasoning policy for a task."""
    from daemon.runtime.module_proxy import runtime

    if explicit_model:
        return explicit_model, "explicit model on task"
    try:
        from lucent.model_registry import select_model_for_task as select

        selection = select(
            agent_type=agent_type,
            title=title,
            description=description,
            preferred_default=runtime.MODEL or None,
            require_tools=True,
        )
        return selection.model_id, selection.reason
    except Exception as error:
        fallback = resolve_default_model()
        return fallback, f"model selector unavailable ({error}); using default"


def task_skips_tool_validation(agent_type: str | None) -> bool:
    """Return whether task success must never depend on tool calls."""
    return (agent_type or "").strip().lower() == "request-review"


def required_task_tool_names(
    agent_type: str | None,
    title: str | None = None,
    description: str | None = None,
) -> set[str]:
    """Return tools explicitly required by the requested deliverable."""
    if task_skips_tool_validation(agent_type):
        return set()
    text = f"{title or ''} {description or ''}".lower()
    required: set[str] = set()
    if any(signal in text for signal in _HANDOFF_TOOL_REQUIRED_SIGNALS) or any(
        pattern.search(text) for pattern in _HANDOFF_TOOL_REQUIRED_PATTERNS
    ):
        required.add("send_handoff")
    return required


def task_requires_mcp_tool_usage(
    agent_type: str | None,
    title: str | None = None,
    description: str | None = None,
) -> bool:
    """Return whether successful execution requires observable MCP activity."""
    agent = (agent_type or "").strip().lower()
    text = f"{title or ''} {description or ''}".lower()
    if task_skips_tool_validation(agent):
        return False
    mutating_memory_signals = (
        "create_memory", "update_memory", "delete_memory", "soft-delete",
        "delete retired", "consolidat", "deduplicat", "merge duplicate",
        "retire", "retirement", "transfer durable", "curate",
    )
    if agent == "memory":
        return any(signal in text for signal in mutating_memory_signals)
    tool_required_signals = (
        "create_memory", "update_memory", "delete_memory", "create_task",
        "create_request", "must call an mcp tool", "must use memory tools",
    )
    return any(signal in text for signal in tool_required_signals) or bool(
        required_task_tool_names(agent_type, title, description)
    )
