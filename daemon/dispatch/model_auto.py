"""Auto model selection for daemon task dispatch.

Fifth application of the System One decision client
(:mod:`lucent.llm.decision`): when ``models.auto_selection_enabled`` is on
and a task either carries the reserved ``model="auto"`` sentinel or names
no model at all, the daemon asks a decision-class model ONE ``choice``
question — which accessible, tools-capable model best fits the work —
after the shared rule filters
(:func:`lucent.llm.model_auto.apply_rule_filters`) narrow the pool.

Degradation mirrors fusion: any resolution failure (no decision model,
endpoint error, below-floor confidence, tie with the default pick, unknown
option) falls back to the exact pre-auto default chain the caller passes as
``fallback_pick`` — so a degraded arbiter is behaviorally identical to the
flag being off. The daemon records the arbiter's token spend
(``engine='decision'``) and emits ``model_auto_selected`` events the
dispatch loop surfaces as task observability.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from daemon.runtime.module_proxy import runtime
from lucent.llm.model_auto import AutoChoice


def model_auto_should_engage(task_model: str | None, org_id: str) -> bool:
    """Whether dispatch should route this task through the arbiter.

    The flag gates both triggers (sentinel or model-unset). Fail-open: a
    settings read failure keeps the old chain.
    """
    from lucent.llm.model_auto import is_auto_model

    if task_model and not is_auto_model(task_model):
        # An explicit concrete model is the author's instruction, never an
        # arbiter question.
        return False
    try:
        from lucent import settings

        return bool(settings.auto_selection_enabled(organization_id=org_id))
    except Exception as error:  # noqa: BLE001 — flag read fails open
        runtime.log(f"Auto-selection flag read failed: {error}", "WARN")
        return False


async def select_task_model(
    *,
    user_id: str,
    org_id: str,
    task: dict[str, Any],
    agent_type: str | None,
    title: str | None,
    description: str | None,
    accessible_models: list[Any],
    fallback_pick: Callable[[], tuple[str, str]],
) -> AutoChoice:
    """Run the auto-selection arbiter over the owner's accessible models.

    ``accessible_models`` is the daemon's already-ACL-filtered,
    tools-capable catalog for the task owner. Usage for the arbiter call is
    recorded here (``engine='decision'``, swallow-failure observability).
    """
    from lucent.llm.model_auto import select_model_auto

    choice = await select_model_auto(
        models=accessible_models,
        agent_type=agent_type,
        title=title,
        description=description,
        request_title=task.get("request_title"),
        org_id=org_id,
        fallback_pick=fallback_pick,
    )
    await _record_auto_usage(org_id=org_id, user_id=user_id, choice=choice)
    return choice


async def _record_auto_usage(
    *, org_id: str, user_id: str | None, choice: AutoChoice
) -> None:
    """Arbiter token spend lands in the ledger like every decision call."""
    if not choice.usage:
        return
    try:
        from lucent.db import get_pool
        from lucent.db.token_usage import TokenUsageRepository

        await TokenUsageRepository(await get_pool()).record(
            organization_id=org_id,
            user_id=user_id or None,
            session_id=None,
            turn_id=None,
            message_id=None,
            model=choice.arbiter_model_id or "",
            engine="decision",
            usage={
                "input_tokens": choice.usage[0],
                "output_tokens": choice.usage[1],
            },
        )
    except Exception as error:  # noqa: BLE001 — usage is observability
        runtime.log(f"Auto-selection usage recording failed: {error}", "WARN")


def selection_event_metadata(choice: AutoChoice, *, trigger: str) -> dict[str, Any]:
    """Metadata for the ``model_auto_selected`` task event."""
    metadata: dict[str, Any] = {
        "chosen": choice.model_id,
        "source": choice.source,
        "trigger": trigger,
        "survivors": list(choice.survivors),
        "dropped": [[model_id, reason] for model_id, reason in choice.dropped],
        "confidence": choice.confidence,
        "arbiter_model": choice.arbiter_model_id,
        "options_truncated": choice.options_truncated,
    }
    if choice.fallback_reason:
        metadata["fallback_reason"] = choice.fallback_reason
    if choice.probabilities:
        metadata["probabilities"] = dict(choice.probabilities)
    if choice.usage:
        metadata["usage"] = {
            "input_tokens": choice.usage[0],
            "output_tokens": choice.usage[1],
        }
    return metadata


def selection_event_detail(choice: AutoChoice) -> str:
    """One-line summary for the ``model_auto_selected`` event."""
    if choice.source == "arbiter":
        return (
            f"Auto selection chose {choice.model_id} via decision arbiter "
            f"{choice.arbiter_model_id} (confidence {choice.confidence:.2f})"
        )
    return (
        f"Auto selection fell back to the default chain ({choice.fallback_reason}) "
        f"→ {choice.model_id}"
    )
