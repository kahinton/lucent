"""Decision-step executor: dispatch decision-model questions without a session.

Third application of the System One decision client (
:mod:`lucent.llm.decision`): a task carrying ``decision_config`` (paired
with the built-in ``decision-step`` agent type) is claimed by the tracked-task
dispatcher and executed here — the state is built from the parent request,
the task itself, and completed sibling task results; the configured questions
are answered in one ``/v1/systemone`` call; the answers are stored as the
task's ``result`` / ``result_structured``.

No chat session, no sandbox, no engine — :func:`run_decision_step` talks to
the decision API directly. Availability is enforced twice: at creation (every
surface checks decision-model access for the requesting principal) and again
at dispatch (:func:`resolve_user_decision_model` re-resolves against the
owner's current grants, because they can change between creation and run).

Threshold policy is per-step (author decision): with no
``threshold_policy`` the answers are recorded as output, always completing;
with one, questions scoring below their min either fail the task
(``on_fail="fail_task"`` — a preventative gate that also blocks downstream
stages) or route it to review (``on_fail="needs_review"``). Usage is
recorded on every successful ``decide()`` call regardless of the threshold
outcome.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from daemon.runtime.module_proxy import runtime
from lucent.llm.decision import (
    DecisionError,
    DecisionQuestion,
    DecisionRequest,
    DecisionResponse,
    decide,
)

# Steps can carry up to the server's full 64-question budget (unlike the
# 16-question injection gate) and run in background dispatch, so the
# timeout is generous — the budget covers a cold model load (~7s measured
# for nimble on the reference machine) plus multi-question inference.
DECISION_TASK_TIMEOUT_SECONDS = float(os.environ.get("LUCENT_DECISION_TASK_TIMEOUT_SECONDS", "60"))
_DECISION_TASK_KEEP_ALIVE = "30m"

_REQUEST_STATE_BUDGET = 2_000
_TASK_STATE_BUDGET = 3_000
_SIBLING_ITEM_BUDGET = 2_000
_SIBLING_STRUCTURED_BUDGET = 1_500
_SIBLING_CONTEXT_BUDGET = 8_000


class ModelAccessDeniedError(PermissionError):
    """Decision dispatch denied — no decision-class model available."""


def _truncate(text: str, limit: int, *, label: str = "content") -> str:
    return runtime._truncate_for_context(str(text or ""), limit, label=label)


def _compact_json(value: Any, max_chars: int) -> str:
    try:
        text = runtime.json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return _truncate(text, max_chars, label="structured output JSON")


async def _fetch_request(request_id: str) -> dict[str, Any] | None:
    """Fetch the parent request (with raw sibling task rows) over the API."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{runtime.API_BASE}/requests/{request_id}",
                headers=runtime.API_HEADERS,
            )
            if resp.status_code == 200:
                return resp.json()
            runtime.log(
                f"Decision step request fetch returned {resp.status_code}: {resp.text[:200]}",
                "WARN",
            )
    except Exception as error:
        runtime.log(f"Decision step request fetch failed: {error}", "WARN")
    return None


def _sibling_context(sibling_tasks: list[dict[str, Any]], task_id: str) -> str:
    """Completed-sibling context blocks, newest-walk under the aggregate budget.

    Oldest→newest in the final text (execution order); when the budget
    forces trimming, the newest blocks that made it in are kept. A sibling's
    ``result_structured`` is preferred when present — that is how a decision
    step scores upstream step outputs ("did every result satisfy X?" as a
    ``noul`` question).
    """
    done = [
        sibling
        for sibling in sibling_tasks
        if sibling.get("status") in ("completed", "review")
        and str(sibling.get("id") or "") != task_id
    ]
    if not done:
        return ""
    kept: list[str] = []
    used = 0
    for sibling in reversed(done):
        result_source = sibling.get("result") or sibling.get("error") or ""
        lines = [f"[{sibling.get('status', 'unknown')}] {sibling.get('title', 'Untitled')}"]
        result = _truncate(result_source, _SIBLING_ITEM_BUDGET, label="task result")
        lines.append(f"result:\n{result}" if result else "result: (no output)")
        structured = sibling.get("result_structured")
        if structured:
            lines.append(
                f"structured result: {_compact_json(structured, _SIBLING_STRUCTURED_BUDGET)}"
            )
        block = "\n".join(lines)
        if used + len(block) + 1 > _SIBLING_CONTEXT_BUDGET:
            break
        used += len(block) + 1
        kept.append(block)
    return "\n\n".join(reversed(kept))


def build_task_decision_state(
    *,
    request_data: dict[str, Any],
    task: dict[str, Any],
    sibling_tasks: list[dict[str, Any]],
    config_state: dict[str, str] | None,
) -> dict[str, str]:
    """Build the named System One state for one decision-step task.

    Fields: ``request``, ``task`` (the primary judge target), ``context``
    (completed sibling blocks), then the author's ``config_state`` literals
    merged last so exact labels can be pinned. Empty fields are omitted.
    """
    state: dict[str, str] = {}

    title = str(request_data.get("title") or "")
    description = str(request_data.get("description") or "(none)")
    request_block = _truncate(
        f"Title: {title}\nDescription:\n{description}",
        _REQUEST_STATE_BUDGET,
        label="decision request",
    )
    if request_block:
        state["request"] = request_block

    task_block = _truncate(
        f"Title: {task.get('title', 'Untitled')}\nDescription:\n"
        f"{task.get('description') or task.get('title', 'Untitled')}",
        _TASK_STATE_BUDGET,
        label="decision task",
    )
    if task_block:
        state["task"] = task_block

    context = _sibling_context(sibling_tasks, str(task.get("id") or ""))
    if context:
        state["context"] = context

    for key, value in (config_state or {}).items():
        if isinstance(key, str) and key:
            state[key] = value
    return state


def build_task_decision_questions(
    decision_config: dict[str, Any],
) -> dict[str, DecisionQuestion]:
    """Ordered question set from the config's JSON array of questions."""
    questions: dict[str, DecisionQuestion] = {}
    for raw in decision_config.get("questions") or []:
        name = str(raw.get("name") or "")
        criteria = raw.get("criteria")
        if raw.get("type") == "noul":
            criteria = None
        questions[name] = DecisionQuestion(
            name=name,
            type=raw.get("type"),
            instructions=str(raw.get("instructions") or ""),
            criteria=criteria,
        )
    return questions


def _normalize_thresholds(decision_config: dict[str, Any]) -> dict[str, float]:
    """Normalized {question name → min} from an already-validated policy."""
    policy = decision_config.get("threshold_policy")
    if not isinstance(policy, dict):
        return {}
    thresholds: dict[str, float] = {}
    for name, minimum in (policy.get("questions") or {}).items():
        if isinstance(minimum, dict):
            minimum = minimum.get("min")
        try:
            thresholds[str(name)] = float(minimum)
        except (TypeError, ValueError):
            continue
    return thresholds


def _answer_score(answer: Any) -> float | None:
    """Thresholdable score for an answer: noul/score only, None otherwise.

    ``score`` answers are probability-weighted averages of rubric level
    indices — thresholds compare against those indices, not percentages.
    """
    if answer is None:
        return None
    noul = getattr(answer, "noul", None)
    if noul is not None:
        try:
            return float(noul)
        except (TypeError, ValueError):
            return None
    score = getattr(answer, "score", None)
    if score is not None:
        try:
            return float(score)
        except (TypeError, ValueError):
            return None
    return None


def _failed_threshold_names(
    response: DecisionResponse,
    thresholds: dict[str, float],
) -> list[str]:
    """Question names that failed their policy minimum (missing answer fails)."""
    failed: list[str] = []
    for name, minimum in thresholds.items():
        answer = (response.answers or {}).get(name) if response.answers else None
        score = _answer_score(answer)
        if score is None or score < minimum:
            failed.append(name)
    return failed


def decision_result_structured(
    response: DecisionResponse,
    decision_config: dict[str, Any],
    failed_names: list[str],
) -> dict[str, Any]:
    """Typed payload stored as result_structured (and read by downstream steps)."""
    answers: dict[str, Any] = {}
    scores: dict[str, float] = {}
    choices: dict[str, str] = {}
    for name, answer in (response.answers or {}).items():
        if hasattr(answer, "choice"):
            choices[name] = answer.choice
            answers[name] = {
                "choice": answer.choice,
                "probabilities": dict(answer.probabilities or {}),
                "confidence": getattr(answer, "confidence", None),
            }
        elif hasattr(answer, "noul") and answer.noul is not None:
            answers[name] = {"noul": answer.noul}
            scores[name] = float(answer.noul)
        elif getattr(answer, "score", None) is not None:
            answers[name] = {
                "score": answer.score,
                "legend": dict(getattr(answer, "legend", None) or {}),
                "probabilities": dict(getattr(answer, "probabilities", None) or {}),
                "confidence": getattr(answer, "confidence", None),
            }
            scores[name] = float(answer.score)
    policy = decision_config.get("threshold_policy") or {}
    payload: dict[str, Any] = {
        "answers": answers,
        "scores": scores,
        "choices": choices,
        "model": str(response.model or ""),
    }
    if policy:
        thresholds = _normalize_thresholds(decision_config)
        payload["thresholds"] = {
            name: {"min": minimum, "met": name not in failed_names}
            for name, minimum in thresholds.items()
        }
        payload["threshold_failed"] = list(failed_names)
        payload["on_fail"] = policy.get("on_fail", "fail_task")
    return payload


def render_decision_result(
    response: DecisionResponse,
    decision_config: dict[str, Any],
    failed_names: list[str],
) -> str:
    """Human-readable per-question summary stored as the task result."""
    lines = [
        f"Decision step completed (model: {response.model}, "
        f"{len(response.answers or {})} questions)."
    ]
    thresholds = _normalize_thresholds(decision_config)
    for name, answer in (response.answers or {}).items():
        if hasattr(answer, "choice"):
            lines.append(
                f"- {name}: chose {answer.choice!r} (confidence "
                f"{answer.confidence} across {len(answer.probabilities or {})} options)"
            )
        elif hasattr(answer, "noul") and answer.noul is not None:
            if name in thresholds:
                verdict = "PASS" if name not in failed_names else "FAIL"
                lines.append(
                    f"- {name}: probability {answer.noul} (min {thresholds[name]}) ({verdict})"
                )
            else:
                lines.append(f"- {name}: probability {answer.noul}")
        elif getattr(answer, "score", None) is not None:
            legend = dict(getattr(answer, "legend", None) or {})
            levels = (
                ", ".join(f"{index}={label}" for index, label in enumerate(legend))
                if legend
                else ""
            )
            if name in thresholds:
                verdict = "PASS" if name not in failed_names else "FAIL"
                lines.append(
                    f"- {name}: score {answer.score} over [{levels}] "
                    f"(min {thresholds[name]}) ({verdict})"
                )
            else:
                lines.append(f"- {name}: score {answer.score} over [{levels}]")
    return "\n".join(lines)


def _result_summary(structured: dict[str, Any]) -> str:
    """Compact summary for prompt-efficient sibling context passing."""
    choices = structured.get("choices") or {}
    scores = structured.get("scores") or {}
    parts = []
    if choices:
        parts.append(" ".join(f"{name}={value}" for name, value in choices.items()))
    if scores:
        parts.append(" ".join(f"{name}={value:.2f}" for name, value in scores.items()))
    detail = "; ".join(parts) or "no numeric answers"
    if structured.get("threshold_failed"):
        return (
            f"Decision answers: {detail}. Threshold policy failed for "
            f"{', '.join(structured['threshold_failed'])} "
            f"(on_fail={structured.get('on_fail', 'fail_task')})."
        )
    return f"Decision answers: {detail}."


async def record_decision_usage(
    *,
    org_id: str,
    user_id: str | None,
    model: str,
    response: DecisionResponse,
) -> None:
    """Persist decision-call token usage (engine='decision') to the ledger."""
    from lucent.db import get_pool
    from lucent.db.token_usage import TokenUsageRepository

    usage = response.usage
    try:
        await TokenUsageRepository(await get_pool()).record(
            organization_id=org_id,
            user_id=user_id or None,
            session_id=None,
            turn_id=None,
            message_id=None,
            model=model,
            engine="decision",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )
    except Exception as error:
        # Usage is observability: never let it break the step's outcome.
        runtime.log(f"Decision usage recording failed: {error}", "WARN")


async def _dispatch_outcome(
    *,
    task: dict[str, Any],
    decision_config: dict[str, Any],
    failed_names: list[str],
    on_complete: Callable[..., Any],
    on_fail: Callable[..., Any],
    on_needs_review: Callable[..., Any],
    result_text: str,
    structured: dict[str, Any],
) -> None:
    """Terminal dispatch for a decided step: complete, fail, or route to review."""
    if not failed_names:
        await on_complete(
            result_text,
            result_structured=structured,
            result_summary=_result_summary(structured),
            validation_status="valid",
        )
        return
    thresholds = _normalize_thresholds(decision_config)
    detail = ", ".join(f"{name} below min {thresholds[name]}" for name in failed_names)
    reason = f"Decision threshold policy failed: {detail}"
    policy = decision_config.get("threshold_policy") or {}
    task_id = str(task.get("id") or "")
    if policy.get("on_fail") == "needs_review":
        await on_needs_review(task_id, reason, result=result_text)
    else:
        await on_fail(task_id, reason, result=result_text)


async def run_decision_step(
    *,
    task: dict[str, Any],
    decision_config: dict[str, Any],
    model_id: str,
    org_id: str,
    requesting_user_id: str | None,
    on_complete: Callable[..., Any],
    on_fail: Callable[..., Any],
    on_needs_review: Callable[..., Any],
) -> None:
    """Run one decision-model step end to end; terminal callbacks own errors."""
    task_id = str(task.get("id") or "")
    request_data: dict[str, Any] = {}
    sibling_tasks: list[dict[str, Any]] = []
    request_id = str(task.get("request_id") or "")
    if request_id:
        data = await _fetch_request(request_id)
        if data:
            request_data = data
            sibling_tasks = list(data.get("tasks") or [])

    questions = build_task_decision_questions(decision_config)
    state = build_task_decision_state(
        request_data=request_data,
        task=task,
        sibling_tasks=sibling_tasks,
        config_state=decision_config.get("state"),
    )

    request = DecisionRequest(
        model_id=model_id,
        state=state,
        questions=questions,
        keep_alive=_DECISION_TASK_KEEP_ALIVE,
    )
    # One deterministic retry: a transient DecisionError (timeout, cold-load
    # hiccup) has no side effects on the task itself, so a single re-call is
    # safe — anything still failing is terminal below.
    response: DecisionResponse | None = None
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = await decide(request, timeout=DECISION_TASK_TIMEOUT_SECONDS)
            break
        except DecisionError as exc:
            last_error = exc
        except Exception as exc:  # unexpected — never retried
            await on_fail(task_id, f"Decision step failed: {exc}")
            return
    if response is None:
        await on_fail(task_id, f"Decision step failed: {last_error}")
        return

    await record_decision_usage(
        org_id=org_id,
        user_id=requesting_user_id,
        model=model_id,
        response=response,
    )

    # Free-form audit event (task event names need no migration); lazily
    # imported so this module never pulls daemon.daemon at import time.
    try:
        from daemon.daemon import RequestAPI

        await RequestAPI.add_event(
            task_id,
            "decision_completed",
            f"model={model_id}, questions={len(response.answers or {})}",
        )
    except Exception as error:
        runtime.log(f"Decision completion event failed: {error}", "WARN")

    thresholds = _normalize_thresholds(decision_config)
    failed_names = _failed_threshold_names(response, thresholds) if thresholds else []
    structured = decision_result_structured(response, decision_config, failed_names)
    result_text = render_decision_result(response, decision_config, failed_names)
    try:
        await _dispatch_outcome(
            task=task,
            decision_config=decision_config,
            failed_names=failed_names,
            on_complete=on_complete,
            on_fail=on_fail,
            on_needs_review=on_needs_review,
            result_text=result_text,
            structured=structured,
        )
    except Exception as error:
        await on_fail(task_id, f"Decision step completion failed: {error}")


async def resolve_user_decision_model(
    *,
    user_id: str,
    org_id: str,
    explicit_model: str | None,
) -> str:
    """Resolve the decision model for a decision-step dispatch.

    Mirrors the daemon's chat-model selection (_accessible_models_for_user)
    but without the tool-capability filter and restricted to
    ``model_class='decision'`` rows: the owner must be an active member, and
    usage is clearance-driven and default-deny. An explicitly configured
    model the owner can no longer use is a hard denial — a silent swap would
    quietly judge steps with a different model than authored.
    """
    from lucent.db import ModelRepository, UserRepository, get_pool
    from lucent.db.models import get_authorized_models_pool

    pool = await get_pool()
    user = await UserRepository(pool).get_by_id(user_id)
    if (
        not user
        or str(user.get("organization_id")) != str(org_id)
        or not user.get("is_active", False)
    ):
        raise ModelAccessDeniedError("Task owner is not an active user in this organization")

    model_pool = await get_authorized_models_pool(
        pool, {"id": str(user_id), "organization_id": str(org_id)}
    )
    accessible = await ModelRepository(model_pool).list_models_accessible_by(
        user_id,
        org_id,
        enabled_only=True,
        limit=500,
    )
    decision_ids = [
        str(row["id"]) for row in accessible["items"] if row.get("model_class") == "decision"
    ]
    if explicit_model:
        if explicit_model in decision_ids:
            return explicit_model
        if not decision_ids:
            # Lenient mode shared with resolve_decision_model_id: when the DB
            # registry carries no decision models at all, an unknown explicit
            # id is trusted (the daemon may run before the registry is loaded).
            return explicit_model
        raise ModelAccessDeniedError(
            f"Decision model '{explicit_model}' is not available to this user"
        )
    if not decision_ids:
        raise ModelAccessDeniedError("No enabled decision-class model is available to this user")
    return decision_ids[0]
