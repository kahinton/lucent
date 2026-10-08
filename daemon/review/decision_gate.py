"""Decision-model pre-review gate for the request review lifecycle.

When ``requests.decision_gate_enabled`` is on, the request review lifecycle
asks a cheap decision-class model (Ollama 0.35+ System One) a fixed set of
``noul`` questions about the completed work — including one per task with an
``output_contract`` — in a single ``/v1/systemone`` call. Everything passing
at or above the configured threshold auto-completes the request without an
LLM review session; anything below escalates to the ordinary review task with
the findings prepended as explicit feedback.

The gate is fail-open: flag off, no decision model available, an endpoint
error, or a timeout all return ``skipped`` and the standard review runs.
Never auto-approves on a skipped outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from daemon.runtime.module_proxy import runtime
from lucent.llm.decision import DecisionError, DecisionRequest, decide

Verdict = Literal["passed", "failed", "skipped"]

# Structural limit on per-contract questions so one request with many
# contracted tasks cannot blow the 64-question System One cap (the aggregate
# questions leave room well below it).
DECISION_GATE_MAX_CONTRACT_TASK_QUESTIONS = 16

# noul probability at or above which an asked question counts as passing.
DECISION_GATE_TIMEOUT_SECONDS = 30.0

_DECISION_GATE_KEEP_ALIVE = "30m"

_PRECONDITION_BLOCKED_STATUSES = ("failed", "cancelled", "needs_review")


@dataclass(slots=True)
class DecisionGateOutcome:
    """Result of one decision-model pre-screen of a request's work."""

    verdict: Verdict = "skipped"
    model_id: str | None = None
    threshold: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    question_count: int = 0
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # True when the decision check passed but a conservative precondition
    # (failed tasks, updatable linked memories, target repo) forces the
    # ordinary LLM review anyway.
    auto_approve_blocked: bool = False
    blocked_reason: str | None = None

    @property
    def auto_approve(self) -> bool:
        return self.verdict == "passed" and not self.auto_approve_blocked

    def metadata(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "model": self.model_id,
            "threshold": self.threshold,
            "scores": {name: round(score, 4) for name, score in self.scores.items()},
            "failed_questions": sorted(self.failed),
            "question_count": self.question_count,
            "auto_approve_blocked": self.auto_approve_blocked,
            "blocked_reason": self.blocked_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }


def _warn(message: str) -> None:
    runtime.log(message, "WARN")


async def resolve_decision_model(daemon, org_id: str | None) -> str | None:
    """Pick the decision-class model for the gate.

    Explicit ``requests.decision_gate_model`` wins. Resolution is shared
    with the memory injection gate; the review gate is a background path so
    it keeps the DB-first pick (DB ordering by provider, name → stable
    selection), with the in-memory registry as fallback.
    """
    from lucent.llm.decision import resolve_decision_model_id
    from lucent.settings import decision_gate_model_id

    return await resolve_decision_model_id(
        explicit_model_id=decision_gate_model_id(organization_id=org_id or None),
        organization_id=org_id or None,
        prefer_db=True,
    )


def _terminal_tasks(daemon, request_data: dict) -> list[dict]:
    tasks = request_data.get("tasks") or []
    non_review = [t for t in tasks if not daemon._is_request_review_task(t)]
    return [t for t in non_review if t.get("status") in ("completed", "failed", "cancelled")]


def _has_contract(task: dict) -> bool:
    contract = task.get("output_contract")
    return isinstance(contract, dict) and bool(contract.get("json_schema"))


def _precondition_blockers(
    daemon, request_data: dict, updatable_memories: list[dict]
) -> str | None:
    """Why the request must still face the full LLM review, or None.

    The decision model cannot call ``update_memory`` or write to a target
    repo, and failed/needs_review tasks mean something already went wrong —
    auto-approving over any of those would silently skip mandatory work.
    """
    tasks = [t for t in request_data.get("tasks") or [] if not daemon._is_request_review_task(t)]
    blocked = [t for t in tasks if t.get("status") in _PRECONDITION_BLOCKED_STATUSES]
    if blocked:
        titles = ", ".join(str(t.get("title", "?"))[:60] for t in blocked[:3])
        return f"failed or blocked task(s) present: {titles}"
    if updatable_memories:
        return f"{len(updatable_memories)} linked memory/memories require review-agent updates"
    if request_data.get("target_repo") or request_data.get("target_paths"):
        return "request has a target repo/paths (durable-delivery review required)"
    return None


def _compact_json(value: Any, max_chars: int) -> str:
    try:
        text = runtime.json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return runtime._truncate_for_context(text, max_chars, label="structured output JSON")


def _recorded_outputs_lines(task: dict) -> str:
    outputs = task.get("outputs") or []
    if not outputs:
        return "  recorded outputs: (none)"
    lines = []
    for output in outputs[:10]:
        lines.append(
            f"  - [{output.get('output_type', 'link')}] "
            f"{output.get('title', 'Untitled output')} "
            f"url={output.get('url') or 'n/a'} "
            f"external_id={output.get('external_id') or 'n/a'}"
        )
    return "  recorded outputs:\n" + "\n".join(lines)


def _task_state_block(daemon, task: dict, item_budget: int) -> str:
    result_source = task.get("result") or task.get("error") or ""
    result = (
        runtime._truncate_for_context(result_source, item_budget, label="task result")
        or "(no output)"
    )
    lines = [
        f"[{task.get('status', 'unknown')}] {task.get('title', 'Untitled')}",
        f"task_id: {task.get('id', '')}",
        f"result:\n{result}",
    ]
    if _has_contract(task):
        schema = task["output_contract"].get("json_schema")
        lines.append(f"output contract (json_schema): {_compact_json(schema, 4000)}")
    structured = task.get("result_structured")
    if structured:
        lines.append(f"structured result: {_compact_json(structured, 4000)}")
    lines.append(_recorded_outputs_lines(task))
    return "\n".join(lines)


def _pre_screen_pass_text(outcome: DecisionGateOutcome) -> str:
    min_score = min(outcome.scores.values()) if outcome.scores else float("nan")
    return (
        f"A decision-model pre-screen (model: {outcome.model_id}, "
        f"{outcome.question_count} questions, threshold "
        f"{outcome.threshold:.2f}) approved this request: lowest "
        f"probability {min_score:.2f}."
    )


def pre_screen_text(outcome: DecisionGateOutcome | None) -> str:
    """Render the pre-screen section for the review description.

    '' when the gate did not run or the request was auto-approved (the
    description is never built in those cases). Otherwise a pass note when
    the screen passed but full review is still required, or a findings
    escalation block when it failed.
    """
    if outcome is None or outcome.verdict == "skipped":
        return ""
    if outcome.verdict == "passed" and not outcome.auto_approve_blocked:
        return ""
    lines: list[str] = []
    if outcome.verdict == "passed":
        lines.append("=== DECISION PRE-SCREEN RESULTS ===")
        lines.append(
            _pre_screen_pass_text(outcome)
            + (
                f" However, the full review is still required: {outcome.blocked_reason}."
                if outcome.blocked_reason
                else ""
            )
        )
    else:
        lines.append("=== DECISION PRE-SCREEN FINDINGS ===")
        lines.append(
            f"A cheap decision-model pre-screen ran before this review "
            f"(model: {outcome.model_id}, threshold {outcome.threshold:.2f}) and "
            "flagged the items below. Verify each explicitly; where an item is "
            "a real problem, address it in your NEEDS_REWORK feedback."
        )
        for name in sorted(outcome.failed):
            lines.append(
                f"- {name} (probability {outcome.scores.get(name, 0.0):.2f}): "
                f"{outcome.failed[name]}"
            )
    lines.append("=== END DECISION PRE-SCREEN ===")
    return "\n".join(lines)


# Kept as the documented entry point; ``pre_screen_text`` is the implementation.
def render_findings_block(outcome: DecisionGateOutcome) -> str:
    """Escalation text inserted into the review description."""
    return pre_screen_text(outcome)


def pass_note(outcome: DecisionGateOutcome) -> str:
    """One-line pass summary for the audit review-task record."""
    return _pre_screen_pass_text(outcome)


def build_decision_state(
    daemon,
    request_data: dict,
    *,
    char_budget: int,
    item_budget: int,
) -> tuple[dict[str, str], list[tuple[int, dict]]]:
    """Build the System One state payload.

    Returns the named-state mapping plus the ``[(task_index, task), ...]``
    list of contracted tasks (capped at 16) whose per-task state field made
    it in. Contracted tasks are always represented — past the budget their
    result text is trimmed harder so the structured payload stays visible —
    because their per-task questions need the payload answerable.
    """
    state: dict[str, str] = {}

    def add(key: str, block: str) -> int:
        state[key] = block
        return len(block)

    title = str(request_data.get("title") or "")
    description = str(request_data.get("description") or "(none)")
    used = add(
        "request",
        runtime._truncate_for_context(
            f"Title: {title}\nDescription:\n{description}",
            4000,
            label="request",
        ),
    )

    done = _terminal_tasks(daemon, request_data)
    contracted: list[tuple[int, dict]] = []
    for index, task in enumerate(done, 1):
        if _has_contract(task) and len(contracted) < DECISION_GATE_MAX_CONTRACT_TASK_QUESTIONS:
            budget = item_budget if used < char_budget else min(item_budget, 500)
            used += add(f"task_{index}", _task_state_block(daemon, task, budget))
            contracted.append((index, task))
            continue
        if used >= char_budget:
            continue
        used += add(f"task_{index}", _task_state_block(daemon, task, item_budget))
    return state, contracted


def build_decision_questions(
    daemon,
    request_data: dict,
    contracted: list[tuple[int, dict]],
) -> dict[str, Any]:
    """Build the fixed ``noul`` question set for the pre-screen.

    ``noul`` only, so the pass rule is a plain probability threshold rather
    than a sampled argmax.
    """
    from lucent.llm.decision import DecisionQuestion

    del daemon  # reserved for future per-agent-type question variants

    questions: dict[str, Any] = {
        "request_fulfilled": DecisionQuestion(
            name="request_fulfilled",
            type="noul",
            instructions=(
                "Taken together, the completed task outputs in the task fields "
                "accomplish the original request's goal described in the "
                "request field."
            ),
        ),
        "not_blocked": DecisionQuestion(
            name="not_blocked",
            type="noul",
            instructions=(
                "None of the task outputs describe an unresolved blocker, "
                "missing credentials or tooling, or incomplete work."
            ),
        ),
    }
    all_tasks = request_data.get("tasks") or []
    if any(_has_contract(t) for t in all_tasks):
        questions["contracts_satisfied"] = DecisionQuestion(
            name="contracts_satisfied",
            type="noul",
            instructions=(
                "Every structured payload shown in the task fields satisfies "
                "its output contract in both shape and meaning: the values "
                "are real, correct, and fulfill the contract's stated intent, "
                "not merely schema-valid."
            ),
        )
    if (
        request_data.get("target_repo")
        or request_data.get("target_paths")
        or any(t.get("outputs") for t in all_tasks)
    ):
        questions["deliverables_durable"] = DecisionQuestion(
            name="deliverables_durable",
            type="noul",
            instructions=(
                "Every deliverable the request required is durably captured: "
                "recorded task outputs show concrete artifacts (urls or "
                "external ids), and where a target repo is named, changes are "
                "evidenced by recorded paths or commits."
            ),
        )
    for index, task in contracted:
        questions[f"contract_task_{index}_satisfied"] = DecisionQuestion(
            name=f"contract_task_{index}_satisfied",
            type="noul",
            instructions=(
                f"Task '{task.get('title', 'Untitled')}' (task field "
                f"task_{index}) produced the structured payload shown there; "
                "it fulfills the stated intent of that task's output "
                "contract, not merely its schema."
            ),
        )
    return questions


def evaluate_decision_response(
    response: Any, questions: dict, threshold: float
) -> tuple[dict[str, float], dict[str, str]]:
    """Split a decision response into (scores, failed) per asked question.

    A question whose answer is missing fails the screen (conservative); the
    ``noul`` probability is compared against the threshold directly since
    every pre-screen question is a ``noul``.
    """
    scores: dict[str, float] = {}
    failed: dict[str, str] = {}
    for name, question in questions.items():
        answer = response.answers.get(name)
        if answer is None:
            scores[name] = 0.0
            failed[name] = question.instructions
            continue
        probability = float(answer.noul)
        scores[name] = probability
        if probability < threshold:
            failed[name] = question.instructions
    return scores, failed


async def maybe_decision_gate(
    daemon, request_id: str, request_data: dict
) -> DecisionGateOutcome:
    """Run the decision-model pre-screen, or return ``skipped``.

    Called from the review lifecycle exactly where an LLM review task is
    about to be created. Anything preventing a run (flag off, no model,
    endpoint error, timeout) lands in ``skipped`` so the standard path
    proceeds — the gate never auto-approves when it did not run.
    """
    from lucent.settings import (
        decision_gate_enabled,
        decision_gate_threshold,
    )

    def skipped(error: str | None = None, warn: str | None = None) -> DecisionGateOutcome:
        if warn:
            _warn(warn)
        return DecisionGateOutcome(verdict="skipped", error=error)

    org_id = str(request_data.get("organization_id", "")) or None
    try:
        enabled = decision_gate_enabled(organization_id=org_id)
        threshold = float(decision_gate_threshold(organization_id=org_id))
    except Exception as exc:
        return skipped(error=str(exc))
    if not enabled:
        return skipped()

    done = _terminal_tasks(daemon, request_data)
    if not done:
        return skipped(warn=(
            f"Request {request_id[:8]} decision pre-screen skipped: no "
            "terminal tasks to screen"
        ))

    try:
        model_id = await resolve_decision_model(daemon, org_id)
        if not model_id:
            return skipped(warn=(
                f"Request {request_id[:8]} decision pre-screen skipped: no "
                "decision-class model available"
            ))

        state, contracted = build_decision_state(
            daemon,
            request_data,
            char_budget=runtime.DECISION_GATE_STATE_CHAR_BUDGET,
            item_budget=runtime.DECISION_GATE_TASK_RESULT_CHAR_BUDGET,
        )
        questions = build_decision_questions(daemon, request_data, contracted)
        decision_request = DecisionRequest(
            model_id=model_id,
            state=state,
            questions=questions,
            keep_alive=_DECISION_GATE_KEEP_ALIVE,
        )
        response = await decide(decision_request, timeout=DECISION_GATE_TIMEOUT_SECONDS)
        scores, failed = evaluate_decision_response(response, questions, threshold)
        outcome = DecisionGateOutcome(
            verdict="failed" if failed else "passed",
            model_id=model_id,
            threshold=threshold,
            scores=scores,
            failed=failed,
            question_count=len(questions),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
    except DecisionError as exc:
        return skipped(error=str(exc), warn=(
            f"Request {request_id[:8]} decision pre-screen unavailable: {exc}; "
            "falling back to standard review"
        ))
    except Exception as exc:
        return skipped(error=str(exc), warn=(
            f"Request {request_id[:8]} decision pre-screen failed: {exc}; "
            "falling back to standard review"
        ))

    if outcome.verdict == "passed":
        linked_memories = await runtime.RequestAPI.get_request_memories(request_id)
        from daemon.review.lifecycle import _review_updatable_memories

        blockers = _precondition_blockers(
            daemon, request_data, _review_updatable_memories(linked_memories)
        )
        if blockers:
            outcome.auto_approve_blocked = True
            outcome.blocked_reason = blockers
            runtime.log(
                f"Request {request_id[:8]} decision pre-screen passed but full "
                f"review still required: {blockers}"
            )
    _log_summary(request_id, outcome)
    return outcome


def _log_summary(request_id: str, outcome: DecisionGateOutcome) -> None:
    if outcome.verdict == "failed":
        runtime.log(
            f"Request {request_id[:8]} decision pre-screen failed "
            f"(model={outcome.model_id}, {len(outcome.failed)}/"
            f"{outcome.question_count} questions below {outcome.threshold:.2f}); "
            "escalating to review"
        )
        return
    if outcome.auto_approve_blocked:
        return  # already logged where the blocker was computed
    min_score = min(outcome.scores.values()) if outcome.scores else 0.0
    runtime.log(
        f"Request {request_id[:8]} decision pre-screen passed "
        f"(model={outcome.model_id}, {outcome.question_count} questions, "
        f"min probability {min_score:.2f} >= {outcome.threshold:.2f}); "
        "auto-completing without LLM review"
    )


async def _auto_resolve_review_task(
    daemon, review_task: dict, request_id: str, outcome: DecisionGateOutcome
) -> bool:
    """Claim and complete the review task with a synthetic APPROVED result.

    Returns False (leaving the task pending for the ordinary LLM review) if
    any lifecycle step fails — e.g. the dispatcher won the claim race.
    """
    task_id = str(review_task.get("id", ""))
    if not task_id:
        return False
    claimed = await runtime.RequestAPI.claim_task(task_id, daemon.instance_id)
    if not claimed:
        return False
    result = (
        "REQUEST_REVIEW_DECISION: APPROVED\n"
        "TASK_IDS_TO_REWORK: none\n"
        f"FEEDBACK: {_pre_screen_pass_text(outcome)}\n"
        "MEMORIES_UPDATED: none\n"
        "HANDOFF_SENT: none\n"
    )
    completed = await runtime.RequestAPI.complete_task(
        task_id, result, instance_id=daemon.instance_id
    )
    if not completed:
        return False
    await daemon._process_request_review_task(dict(completed), result)
    return True
