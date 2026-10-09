"""Fusion dispatch executor: N candidate sessions + decision-model arbiter.

Fourth application of the System One decision client
(:mod:`lucent.llm.decision`): a task carrying ``fusion_config`` runs N
candidate chat sessions sequentially inside ONE dispatch cycle, then the
decision arbiter scores every candidate's answer against the authored
questions — questions are re-asked per candidate with ``cand{index}_``
prefixed names so one ``decide()`` call (or one per batch when the
64-question budget forces it) yields comparable per-candidate scores — and
the best candidate (or a synthesis, or a refine pass) is finalized through
the daemon's normal completion tail.

The fan-out never leaves the tracked-task loop — no sibling tasks, no new
task states, no queue changes. Degradation is deliberate: a flag turned off
mid-flight fails open to the normal single-model path (``fusion_disabled``,
added by the daemon branch before this executor runs); unsupported shapes
(sandbox, decision-step, required tools) decline with
``fusion_unsupported`` there too; zero successful candidates is a real
failure and exactly one continues degraded but still scored.

Token accounting mirrors the normal path (per-candidate ledger rows still
land in ``llm_token_usage`` via ``record_stream_usage``, which also sums
into ``daemon._session_usage_totals`` for this executor to aggregate) with
the arbiter adding engine='decision' rows on top via
:func:`daemon.dispatch.decision_step.record_decision_usage`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from daemon.dispatch.decision_step import (
    _fetch_request,
    _truncate,
    build_task_decision_questions,
    record_decision_usage,
)
from daemon.runtime.module_proxy import runtime
from lucent.llm.decision import (
    ChoiceAnswer,
    DecisionError,
    DecisionQuestion,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    ScoreAnswer,
    decide,
)

# The arbiter call mirrors the decision-step timeout (cold model load plus
# multi-question inference); candidate sessions borrow run_session's own
# total/idle timeouts like any tracked task.
ARBITER_TIMEOUT_SECONDS = float(os.environ.get("LUCENT_FUSION_ARBITER_TIMEOUT_SECONDS", "60"))
_KEEP_ALIVE = "30m"

# Arbiter state budgets: request intro, task brief, then one bounded excerpt
# per candidate, hard-capped in aggregate so a wide fan-out cannot blow the
# decision model's context.
_REQUEST_STATE_BUDGET = 1_500
_TASK_STATE_BUDGET = 2_000
_CANDIDATE_STATE_BUDGET = 3_000
_CANDIDATE_STATE_TOTAL = 12_000

# Refine and synthesis prompts carry candidate text; bounded so a wide
# fan-out cannot blow the refinement model's context.
_RESULT_PROMPT_BUDGET = 8_000
_CRITIQUE_ITEM_BUDGET = 400
_SYNTHESIS_CANDIDATE_BUDGET = 3_000
_SYNTHESIS_TOTAL_BUDGET = 12_000

# Telemetry excerpt in fusion_completed metadata: enough text to eyeball
# candidate differences without bloating task events.
_EXCERPT_BUDGET = 2_000

# Hard client-side cap on questions per decide() call (the server limit the
# request validators enforce for authored questions).
_MAX_QUESTIONS_PER_CALL = 64


class ModelAccessDeniedError(PermissionError):
    """Fusion dispatch denied — no usable candidate or arbiter model."""


@dataclass
class CandidateResult:
    """One candidate session's outcome; failures are recorded, not raised.

    ``index`` doubles as the arbiter's state/question label for that
    candidate (``cand{index}``) — authored candidates are 1..N in run order;
    refine and synthesis candidates are scored standalone with their own
    label and insert at the front of the ranking on adoption.
    """

    index: int
    model: str
    status: str = "failed"  # completed | interrupted | empty | failed
    result_text: str | None = None
    elapsed_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    scores: dict[str, float] = field(default_factory=dict)  # normalized 0..1
    raw_scores: dict[str, float] = field(default_factory=dict)  # threshold units
    aggregate: float | None = None  # mean of normalized scores; None unscored
    failing: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # tool-call counts
    error: str | None = None


@dataclass
class FusionResult:
    """Diagnostic return of :func:`run_fusion_dispatch`.

    Task bookkeeping happens through the callbacks; this only describes what
    happened (for the daemon branch's logging and for tests).
    """

    winner_model: str | None = None
    winner_text: str | None = None
    error: str | None = None


def _fusion_flags() -> dict[str, bool]:
    """Fail-open flag reads (a settings hiccup must never strand a task)."""
    try:
        from lucent import settings

        return {
            "best_of_n": bool(settings.fusion_best_of_n_enabled()),
            "synthesis": bool(settings.fusion_synthesis_enabled()),
            "refine": bool(settings.fusion_refine_loop_enabled()),
        }
    except Exception as error:
        runtime.log(f"Fusion flag reads failed: {error}", "WARN")
        return {"best_of_n": False, "synthesis": False, "refine": False}


def _fusion_max_candidates() -> int:
    """Clamped ``fusion.max_candidates`` read (fail-open to the default)."""
    try:
        from lucent import settings

        return int(settings.fusion_max_candidates())
    except Exception as error:
        runtime.log(f"Fusion max-candidates read failed: {error}", "WARN")
        return 4


async def _emit_event(
    task_id: str, kind: str, detail: str, metadata: dict[str, Any] | None = None
) -> None:
    """Task event fire-and-forget (observability never aborts a dispatch)."""
    try:
        from daemon.daemon import RequestAPI

        await RequestAPI.add_event(task_id, kind, detail, metadata)
    except Exception as error:
        runtime.log(f"Fusion event {kind} failed: {error}", "WARN")


async def _decide_with_retry(
    request: DecisionRequest,
) -> tuple[DecisionResponse | None, str | None]:
    """One deterministic retry on DecisionError — decide() is stateless."""
    last_error: str | None = None
    for _attempt in range(2):
        try:
            return await decide(request, timeout=ARBITER_TIMEOUT_SECONDS), None
        except DecisionError as exc:
            last_error = str(exc)
    return None, last_error


async def run_fusion_dispatch(
    *,
    daemon: Any,
    task: dict[str, Any],
    fusion_config: dict[str, Any],
    agent_type: str,
    description: str,
    system_message: str | None,
    task_mcp_config: dict[str, Any] | None,
    task_enable_config_discovery: Any,
    hooks: Any,
    expected_surface: Any,
    selected_model: str | None,
    task_reasoning_effort: Any,
    org_id: str,
    requesting_user_id: str | None,
    accessible_models: list[str],
    audit_base: dict[str, Any],
    on_complete: Callable[..., Any],
    on_fail: Callable[..., Any],
    on_needs_review: Callable[..., Any],
) -> FusionResult:
    """Run one fusion task end to end; terminal callbacks own errors.

    Callback signatures: ``on_complete(result_text, result_structured=...,
    result_summary=...  (the fusion comparison prefix),
    validation_tool_counts=...)`` feeds the daemon's normal completion tail;
    ``on_fail(task_id, reason, result=...)`` and ``on_needs_review(task_id,
    reason, result=...)`` route threshold and hard failures. The daemon
    branch is expected to ``continue`` when this returns.
    """
    try:
        return await _run_fusion_dispatch(
            daemon=daemon,
            task=task,
            fusion_config=fusion_config,
            agent_type=agent_type,
            description=description,
            system_message=system_message,
            task_mcp_config=task_mcp_config,
            task_enable_config_discovery=task_enable_config_discovery,
            hooks=hooks,
            expected_surface=expected_surface,
            selected_model=selected_model,
            task_reasoning_effort=task_reasoning_effort,
            org_id=org_id,
            requesting_user_id=requesting_user_id,
            accessible_models=accessible_models,
            audit_base=audit_base,
            on_complete=on_complete,
            on_fail=on_fail,
            on_needs_review=on_needs_review,
        )
    except Exception as exc:
        task_id = str(task.get("id") or "")
        reason = f"Fusion dispatch failed: {exc}"
        runtime.log(f"Fusion dispatch {task_id[:8]} failed: {exc}", "WARN")
        try:
            await on_fail(task_id, reason, result=None)
        except Exception as callback_error:
            runtime.log(f"Fusion failure callback failed: {callback_error}", "WARN")
        return FusionResult(error=reason)


async def _run_fusion_dispatch(
    *,
    daemon: Any,
    task: dict[str, Any],
    fusion_config: dict[str, Any],
    agent_type: str,
    description: str,
    system_message: str | None,
    task_mcp_config: dict[str, Any] | None,
    task_enable_config_discovery: Any,
    hooks: Any,
    expected_surface: Any,
    selected_model: str | None,
    task_reasoning_effort: Any,
    org_id: str,
    requesting_user_id: str | None,
    accessible_models: list[str],
    audit_base: dict[str, Any],
    on_complete: Callable[..., Any],
    on_fail: Callable[..., Any],
    on_needs_review: Callable[..., Any],
) -> FusionResult:
    task_id = str(task.get("id") or "")
    request_id = str(task.get("request_id") or "")
    dispatch_started = time.monotonic()
    mode = str(fusion_config.get("mode") or "best_of_n")
    arbiter_cfg = (
        fusion_config.get("arbiter") if isinstance(fusion_config.get("arbiter"), dict) else {}
    )
    candidates_cfg = (
        fusion_config.get("candidates") if isinstance(fusion_config.get("candidates"), dict) else {}
    )
    synthesis_cfg = (
        fusion_config.get("synthesis") if isinstance(fusion_config.get("synthesis"), dict) else {}
    )
    refine_cfg = (
        fusion_config.get("refine") if isinstance(fusion_config.get("refine"), dict) else {}
    )
    flags = _fusion_flags()
    arbiter_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    arbiter_calls = 0

    # ---- 1. Resolve candidate models (fail loud: two candidates ARE the
    # mode — a single-model run would silently be the old path). ------------
    candidate_models = _candidate_models(
        candidates_cfg,
        fallback_model=selected_model,
        accessible_models=accessible_models,
        max_candidates=_fusion_max_candidates(),
    )
    if len(candidate_models) < 2:
        reason = (
            "Fusion dispatch failed: resolved fewer than 2 candidate models "
            f"from {candidates_cfg!r} (selected_model={selected_model!r})"
        )
        return await _fail_task(on_fail, task_id, reason, metadata={"resolved": candidate_models})

    # ---- 2. Run candidates sequentially ------------------------------------
    candidates: list[CandidateResult] = []
    for index, model_id in enumerate(candidate_models, start=1):
        candidate = await _run_session_candidate(
            daemon=daemon,
            task=task,
            index=index,
            model_id=model_id,
            agent_type=agent_type,
            description=description,
            system_message=system_message,
            task_mcp_config=task_mcp_config,
            task_enable_config_discovery=task_enable_config_discovery,
            hooks=hooks,
            expected_surface=expected_surface,
            task_reasoning_effort=task_reasoning_effort,
            org_id=org_id,
            requesting_user_id=requesting_user_id,
            audit_base=audit_base,
        )
        candidates.append(candidate)
        await _emit_event(
            task_id,
            "fusion_candidate_done",
            f"candidate {index}/{len(candidate_models)} ({model_id}): {candidate.status}",
            {
                "index": index,
                "model": model_id,
                "status": candidate.status,
                "elapsed_ms": candidate.elapsed_ms,
                "input_tokens": candidate.input_tokens,
                "output_tokens": candidate.output_tokens,
                "output_chars": len(candidate.result_text or ""),
                "error": candidate.error,
            },
        )

    successful = [c for c in candidates if c.status == "completed" and c.result_text]
    if not successful:
        reason = (
            "Fusion dispatch failed: no candidate produced a usable result ("
            + "; ".join(f"cand{c.index}={c.model}:{c.error or c.status}" for c in candidates)
            + ")"
        )
        return await _fail_task(
            on_fail,
            task_id,
            reason,
            metadata={
                "candidates": [
                    {"index": c.index, "model": c.model, "status": c.status, "error": c.error}
                    for c in candidates
                ]
            },
        )
    degraded = len(successful) == 1
    if degraded:
        await _emit_event(
            task_id,
            "fusion_degraded_single_candidate",
            "Fusion degraded to one successful candidate; the arbiter still scores it",
            {"index": successful[0].index, "model": successful[0].model},
        )

    # ---- 3. Arbiter over every successful candidate -------------------------
    arbiter_model: str | None = None
    scored_error: str | None = None
    if arbiter_cfg.get("questions"):
        arbiter_error, scored_error, arbiter_calls, arbiter_model = await _resolve_and_score(
            arbiter_cfg=arbiter_cfg,
            candidates=successful,
            task=task,
            description=description,
            request_id=request_id,
            org_id=org_id,
            requesting_user_id=requesting_user_id,
            usage_acc=arbiter_usage,
        )
    if arbiter_cfg.get("questions") and scored_error is not None:
        reason = f"Fusion dispatch failed: arbiter could not score candidates ({scored_error})"
        return await _fail_task(on_fail, task_id, reason, metadata={"arbiter_model": arbiter_model})

    thresholds = _normalize_thresholds(arbiter_cfg)
    winner = _pick_winner(successful)

    # ---- 4. Refine pass (best_of_n, flag-gated, strictly better only) ------
    refine_state: dict[str, Any] | None = None
    refine_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    if mode == "best_of_n" and refine_cfg:
        if not flags.get("refine"):
            await _emit_event(
                task_id,
                "fusion_refine_skipped",
                "refine configured but fusion.refine_loop_enabled is off",
            )
        else:
            refine_state = await _maybe_refine(
                daemon=daemon,
                task=task,
                description=description,
                winner=winner,
                thresholds=thresholds,
                arbiter_model=arbiter_model,
                arbiter_cfg=arbiter_cfg,
                refine_cfg=refine_cfg,
                selected_model=selected_model,
                agent_type=agent_type,
                system_message=system_message,
                task_mcp_config=task_mcp_config,
                task_enable_config_discovery=task_enable_config_discovery,
                hooks=hooks,
                expected_surface=expected_surface,
                task_reasoning_effort=task_reasoning_effort,
                org_id=org_id,
                requesting_user_id=requesting_user_id,
                audit_base=audit_base,
                arbiter_usage=arbiter_usage,
                refine_usage=refine_usage,
            )
            if refine_state.get("candidate"):
                successful.insert(0, refine_state["candidate"])
                winner = refine_state["candidate"]

    # ---- 5. Synthesis (mode=synthesize; the arbiter is optional here) -------
    synthesis_state: dict[str, Any] | None = None
    if mode == "synthesize":
        synthesis_state = await _maybe_synthesize(
            daemon=daemon,
            task=task,
            description=description,
            candidates=successful,
            thresholds=thresholds,
            arbiter_model=arbiter_model,
            arbiter_cfg=arbiter_cfg,
            synthesis_cfg=synthesis_cfg,
            selected_model=selected_model,
            agent_type=agent_type,
            system_message=system_message,
            task_mcp_config=task_mcp_config,
            task_enable_config_discovery=task_enable_config_discovery,
            hooks=hooks,
            expected_surface=expected_surface,
            task_reasoning_effort=task_reasoning_effort,
            org_id=org_id,
            requesting_user_id=requesting_user_id,
            audit_base=audit_base,
            arbiter_usage=arbiter_usage,
        )
        if synthesis_state.get("candidate"):
            successful.insert(0, synthesis_state["candidate"])
            winner = synthesis_state["candidate"]
        else:
            reason = (
                f"Fusion dispatch failed: synthesis session produced no usable "
                f"result ({synthesis_state.get('error') or 'unknown error'})"
            )
            return await _fail_task(on_fail, task_id, reason, metadata={"mode": mode})

    # ---- 6. Threshold policy on the winner ----------------------------------
    totals = _usage_totals(
        candidates=candidates,
        arbiter_usage=arbiter_usage,
        refine_usage=refine_usage,
        synthesis_state=synthesis_state,
        degraded=degraded,
    )
    failing = _failing_questions(winner, thresholds)
    if thresholds and failing:
        failed_detail = ", ".join(
            f"{name}={winner.raw_scores.get(name, 0.0):.2f} below min {thresholds[name]}"
            for name in failing
        )
        reason = f"Fusion threshold policy failed: {failed_detail}"
        policy = arbiter_cfg.get("threshold_policy") or {}
        await _emit_event(
            task_id,
            "fusion_completed",
            f"mode={mode} winner={winner.model} aggregate={_fmt(winner.aggregate)}; "
            f"total_tokens={totals.get('total_tokens')} — threshold policy failed",
            _metadata(
                mode=mode,
                candidates=candidates,
                ranked=successful,
                arbiter_model=arbiter_model,
                arbiter_usage=arbiter_usage,
                refine_usage=refine_usage,
                winner=winner,
                degraded=degraded,
                refine_state=refine_state,
                synthesis_state=synthesis_state,
                threshold_failed=True,
                elapsed_ms=int((time.monotonic() - dispatch_started) * 1000),
                totals=totals,
            ),
        )
        if policy.get("on_fail") == "needs_review":
            await on_needs_review(task_id, reason, result=winner.result_text)
        else:
            await on_fail(task_id, reason, result=winner.result_text)
        return FusionResult(winner_model=winner.model, error=reason)

    # ---- 7. Complete through the daemon's normal tail -----------------------
    structured = _result_structured(
        mode=mode,
        winner=winner,
        ranked=successful,
        arbiter_model=arbiter_model,
        thresholds=thresholds,
        failed_names=[],
        degraded=degraded,
    )
    summary_prefix = _summary_prefix(
        mode=mode,
        ranked=successful,
        arbiter_model=arbiter_model,
        winner=winner,
        refine_state=refine_state,
        synthesis_state=synthesis_state,
        totals=totals,
        degraded=degraded,
    )
    await _emit_event(
        task_id,
        "fusion_completed",
        f"mode={mode} winner={winner.model} aggregate={_fmt(winner.aggregate)}; "
        f"total_tokens={totals.get('total_tokens')}",
        _metadata(
            mode=mode,
            candidates=candidates,
            ranked=successful,
            arbiter_model=arbiter_model,
            arbiter_usage=arbiter_usage,
            refine_usage=refine_usage,
            winner=winner,
            degraded=degraded,
            refine_state=refine_state,
            synthesis_state=synthesis_state,
            threshold_failed=False,
            elapsed_ms=int((time.monotonic() - dispatch_started) * 1000),
            totals=totals,
        ),
    )
    try:
        await on_complete(
            winner.result_text,
            result_structured=structured,
            result_summary=summary_prefix,
            validation_tool_counts=dict(winner.counts or {}),
        )
    except Exception as error:
        reason = f"Fusion completion tail failed: {error}"
        await on_fail(task_id, reason, result=None)
        return FusionResult(winner_model=winner.model, error=reason)
    return FusionResult(winner_model=winner.model, winner_text=winner.result_text)


async def _fail_task(
    on_fail: Callable[..., Any],
    task_id: str,
    reason: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> FusionResult:
    """Emit fusion_failed and run the fail callback; callback errors never raise."""
    await _emit_event(task_id, "fusion_failed", reason, metadata)
    try:
        await on_fail(task_id, reason, result=None)
    except Exception as error:
        runtime.log(f"Fusion failure callback failed: {error}", "WARN")
    return FusionResult(error=reason)


# ---------------------------------------------------------------------------
# Candidate resolution + execution
# ---------------------------------------------------------------------------


def _candidate_models(
    candidates_cfg: dict[str, Any],
    *,
    fallback_model: str | None,
    accessible_models: list[str],
    max_candidates: int,
) -> list[str]:
    """Resolve the candidate model list from an authoring-validated config.

    Count-based configs are filled here: the daemon's selected model leads,
    then the owner's other accessible tool-capable models, then repeats of
    the fallback only when nothing else is available (repeats are honest
    candidates — same model, independent runs). Access was enforced at
    authoring; run_session re-asserts per-call so a stale grant drops only
    that one candidate.
    """
    models = candidates_cfg.get("models")
    if isinstance(models, list) and len(models) >= 2:
        return [str(model) for model in models if model]

    try:
        count = int(candidates_cfg.get("count") or 2)
    except (TypeError, ValueError):
        count = 2
    count = max(2, min(max_candidates, count))
    pool: list[str] = []
    if fallback_model:
        pool.append(str(fallback_model))
    for model_id in accessible_models or []:
        if len(pool) >= count:
            break
        if model_id not in pool:
            pool.append(str(model_id))
    # Degenerate fill: the fallback repeated N times still gives the arbiter
    # N independent runs to compare.
    while len(pool) < count and pool:
        pool.append(pool[0])
    return pool[:count]


def _session_audit_context(audit_base: dict[str, Any], role: str, index: int) -> dict[str, Any]:
    return {
        **audit_base,
        "source": f"daemon.fusion_{role}",
        "fusion_candidate_index": index,
    }


async def _run_session_candidate(
    *,
    daemon: Any,
    task: dict[str, Any],
    index: int,
    model_id: str,
    agent_type: str,
    description: str,
    system_message: str | None,
    task_mcp_config: dict[str, Any] | None,
    task_enable_config_discovery: Any,
    hooks: Any,
    expected_surface: Any,
    task_reasoning_effort: Any,
    org_id: str,
    requesting_user_id: str | None,
    audit_base: dict[str, Any],
) -> CandidateResult:
    """One candidate chat session via ``daemon.run_session``; never raises.

    Uses a unique session name — the normal single-model path owns the
    ``{agent_type}-{task_id[:8]}`` name and its tracker keys, and a collision
    would clobber that bookkeeping.
    """
    task_id = str(task.get("id") or "")
    session_name = f"{agent_type}-{task_id[:8]}-cand-{index}"
    started = time.monotonic()
    candidate = CandidateResult(index=index, model=model_id)
    try:
        result = await daemon.run_session(
            session_name,
            system_message,
            f"Execute this task:\n\n{description}",
            model=model_id,
            reasoning_effort=task_reasoning_effort,
            mcp_config_override=task_mcp_config,
            enable_config_discovery=task_enable_config_discovery,
            hooks=hooks,
            expected_surface=expected_surface,
            audit_context=_session_audit_context(audit_base, "candidate", index),
        )
    except Exception as exc:
        candidate.status = "failed"
        candidate.error = f"session error: {exc}"
        return candidate
    candidate.elapsed_ms = int((time.monotonic() - started) * 1000)
    _absorb_bookkeeping(daemon, session_name, candidate, result)
    return candidate


def _absorb_bookkeeping(
    daemon: Any, session_name: str, candidate: CandidateResult, result: Any
) -> None:
    """Pop one fusion session's tracker keys into the candidate result.

    Pops only this session's keys — the normal path's session bookkeeping is
    keyed by its own name and is never touched here.
    """
    terminal = daemon._session_terminal_outcomes.pop(session_name, None)
    daemon._session_mcp_trackers.pop(session_name, None)
    counts = daemon._session_tool_trackers.pop(session_name, None)
    candidate.counts = dict(counts) if isinstance(counts, dict) else {}
    if terminal and isinstance(terminal, dict):
        if terminal.get("interrupted"):
            candidate.status = "interrupted"
            candidate.error = str(terminal.get("reason") or "session interrupted")
        elif terminal.get("result") and not str(candidate.result_text or "").strip():
            candidate.result_text = str(terminal.get("result"))
    if result and str(result).strip():
        candidate.result_text = str(result).strip()
    if candidate.result_text and str(candidate.result_text).strip():
        candidate.status = "completed"
        candidate.result_text = str(candidate.result_text).strip()
    elif not candidate.error:
        candidate.status = "empty"
        candidate.error = "session produced no result"
    usage = (daemon._session_usage_totals or {}).pop(session_name, None)
    if isinstance(usage, dict):
        candidate.input_tokens = usage.get("input_tokens")
        candidate.output_tokens = usage.get("output_tokens")


# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------


async def _resolve_and_score(
    *,
    arbiter_cfg: dict[str, Any],
    candidates: list[CandidateResult],
    task: dict[str, Any],
    description: str,
    request_id: str,
    org_id: str,
    requesting_user_id: str | None,
    usage_acc: dict[str, int],
) -> tuple[str, str | None, int, str]:
    """Resolve the arbiter model then score.

    Returns (model, error, decide-calls, resolved-model-id); error None on
    success. A resolved empty model with no error never happens (see
    :func:`_resolve_arbiter_model`) — one of error/model is always set.
    """
    arbiter_model, resolve_error = await _resolve_arbiter_model(
        org_id=org_id,
        explicit_model=arbiter_cfg.get("model"),
        owner_id=requesting_user_id,
    )
    if resolve_error is not None:
        return "", resolve_error, 0, ""
    score_error, calls = await _score_candidates(
        arbiter_model=arbiter_model,
        arbiter_cfg=arbiter_cfg,
        candidates=candidates,
        task=task,
        description=description,
        request_id=request_id,
        usage_acc=usage_acc,
        org_id=org_id,
        user_id=requesting_user_id,
    )
    return arbiter_model, score_error, calls, arbiter_model


async def _resolve_arbiter_model(
    *, org_id: str, explicit_model: Any, owner_id: str | None
) -> tuple[str, str | None]:
    """explicit → ``fusion.arbiter_model`` flag → ``resolve_decision_model_id``.

    Returns (model, error). A missing model is an error — judging candidates
    with whatever model happened to be handy would quietly corrupt the
    ranking, and best_of_n without an arbiter is meaningless (validation
    already requires the arbiter for that mode). When an owner id is
    available the resolved model is re-checked against their CURRENT grants
    (mirroring decision-step dispatch), because grants can change between
    creation and run; an explicit-but-unavailable model is a hard denial.
    """
    from daemon.dispatch.decision_step import (
        ModelAccessDeniedError,
        resolve_user_decision_model,
    )
    from lucent import settings
    from lucent.llm.decision import resolve_decision_model_id

    explicit = str(explicit_model or "").strip() or None
    if not explicit:
        try:
            explicit = settings.fusion_arbiter_model_id(organization_id=org_id) or None
        except Exception as error:
            runtime.log(f"Fusion arbiter flag read failed: {error}", "WARN")
            explicit = None
    if not explicit and owner_id:
        # Fallback chain below needs an owner context: resolve against the
        # owner's registry view rather than the process-wide registry pick.
        try:
            from lucent.db import ModelRepository, get_pool
            from lucent.db.models import get_authorized_models_pool

            pool = await get_pool()
            model_pool = await get_authorized_models_pool(
                pool, {"id": str(owner_id), "organization_id": str(org_id)}
            )
            accessible = await ModelRepository(model_pool).list_models_accessible_by(
                owner_id, org_id, enabled_only=True, limit=500
            )
            decision_ids = [
                str(row["id"])
                for row in accessible["items"]
                if row.get("model_class") == "decision"
            ]
            explicit = decision_ids[0] if decision_ids else None
        except Exception as error:
            runtime.log(f"Fusion arbiter owner-scope resolution failed: {error}", "WARN")
    if not explicit:
        try:
            explicit = await resolve_decision_model_id(organization_id=org_id) or None
        except Exception as error:
            runtime.log(f"Fusion arbiter fallback resolution failed: {error}", "WARN")
            explicit = None
    if not explicit:
        return "", (
            "no decision model available for the fusion arbiter — configure "
            "fusion.arbiter_model or add a decision model to the registry"
        )
    if owner_id:
        try:
            explicit = await resolve_user_decision_model(
                user_id=str(owner_id),
                org_id=org_id,
                explicit_model=explicit,
            )
        except ModelAccessDeniedError as exc:
            return "", f"arbiter model denied for the task owner: {exc}"
    return explicit, None


def _normalize_thresholds(arbiter_cfg: dict[str, Any]) -> dict[str, float]:
    """{question name → min} from the arbiter's threshold_policy."""
    policy = arbiter_cfg.get("threshold_policy")
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


async def _score_candidates(
    *,
    arbiter_model: str,
    arbiter_cfg: dict[str, Any],
    candidates: list[CandidateResult],
    task: dict[str, Any],
    description: str,
    request_id: str,
    usage_acc: dict[str, int],
    org_id: str = "",
    user_id: str | None = None,
) -> tuple[str | None, int]:
    """Score every candidate in place with the authored questions.

    Questions are duplicated per candidate with ``cand{index}_``-prefixed
    names so one decide() call returns comparable namespaced answers; calls
    are batched when the total exceeds the server's question budget. Every
    batch re-asks the authored questions verbatim, so aggregates are
    comparable across batches. Returns (None, calls) on success,
    (error-string, calls-so-far) otherwise.
    """
    base_questions = build_task_decision_questions(arbiter_cfg)
    if not base_questions:
        return "arbiter config carries no questions", 0
    batch_size = max(1, _MAX_QUESTIONS_PER_CALL // len(base_questions))
    calls = 0

    # One bounded parent-request fetch per scoring call; failure merely
    # trims the state, never the scoring.
    request_data: dict[str, Any] | None = None
    if request_id:
        request_data = await _fetch_request(request_id)

    thresholds = _normalize_thresholds(arbiter_cfg)
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        questions: dict[str, DecisionQuestion] = {}
        mapping: dict[str, tuple[CandidateResult, str]] = {}
        for candidate in batch:
            for name, question in base_questions.items():
                prefixed = f"cand{candidate.index}_{name}"
                mapping[prefixed] = (candidate, name)
                questions[prefixed] = DecisionQuestion(
                    name=prefixed,
                    type=question.type,
                    instructions=question.instructions,
                    criteria=question.criteria,
                )
        response, error = await _decide_with_retry(
            DecisionRequest(
                model_id=arbiter_model,
                state=_arbiter_state(
                    task=task,
                    description=description,
                    request_data=request_data,
                    batch=batch,
                ),
                questions=questions,
                keep_alive=_KEEP_ALIVE,
            )
        )
        if response is None:
            return error or "arbiter call failed without an error", calls
        calls += 1
        usage_acc["input_tokens"] += response.usage.input_tokens
        usage_acc["output_tokens"] += response.usage.output_tokens
        if org_id:
            # Every successful decide() lands an engine='decision' row in the
            # ledger (usage recording never breaks scoring).
            await record_decision_usage(
                org_id=org_id,
                user_id=user_id,
                model=arbiter_model,
                response=response,
            )
        for prefixed, answer in (response.answers or {}).items():
            entry = mapping.get(prefixed)
            if entry is None:
                continue
            candidate, base_name = entry
            normalized, raw = _answer_value(answer, questions[prefixed])
            candidate.scores[base_name] = normalized
            candidate.raw_scores[base_name] = raw
        for candidate in batch:
            candidate.aggregate = _aggregate(candidate.scores)
            candidate.failing = _failing_questions(candidate, thresholds)
    return None, calls


def _arbiter_state(
    *,
    task: dict[str, Any],
    description: str,
    request_data: dict[str, Any] | None,
    batch: list[CandidateResult],
) -> dict[str, str]:
    """Named state: request intro, task brief, one bounded excerpt per candidate
    (state field ``cand{index}`` lines up with the ``cand{index}_`` question
    prefix so the model knows which excerpt each question judges)."""
    state: dict[str, str] = {}
    title = str((request_data or {}).get("title") or "")
    body = str((request_data or {}).get("description") or "(none)")
    request_block = _truncate(
        f"Title: {title}\nDescription:\n{body}",
        _REQUEST_STATE_BUDGET,
        label="fusion request",
    )
    if request_block:
        state["request"] = request_block
    task_block = _truncate(
        f"Title: {task.get('title', 'Untitled')}\nDescription:\n{description}",
        _TASK_STATE_BUDGET,
        label="fusion task",
    )
    if task_block:
        state["task"] = task_block
    remaining = _CANDIDATE_STATE_TOTAL
    for candidate in batch:
        budget = min(_CANDIDATE_STATE_BUDGET, remaining)
        if budget <= 0:
            break
        text = _truncate(candidate.result_text or "", budget, label="candidate answer")
        state[f"cand{candidate.index}"] = text
        remaining -= max(0, budget - len(text))
    return state


def _score_levels(question: DecisionQuestion) -> int:
    """Rubric size for score questions (threshold min is in level units)."""
    criteria = question.criteria
    if isinstance(criteria, (dict, list)) and criteria:
        return max(2, len(criteria))
    return 2


def _answer_value(answer: Any, question: DecisionQuestion) -> tuple[float, float]:
    """(normalized 0..1, raw threshold units) for one answer; missing → (0, 0).

    noul: the probability used as-is for both. score: raw is the
    probability-weighted level-index average (threshold min units, matching
    decision-step semantics), normalized divides by (levels - 1). choice:
    the chosen option's confidence (choice questions cannot carry
    thresholds, so raw = normalized = confidence).
    """
    if isinstance(answer, NoulAnswer):
        value = float(answer.noul or 0.0)
        return value, value
    if isinstance(answer, ScoreAnswer):
        raw = float(answer.score or 0.0)
        return max(0.0, min(1.0, raw / (_score_levels(question) - 1))), raw
    if isinstance(answer, ChoiceAnswer):
        value = float(answer.confidence or 0.0)
        return value, value
    return 0.0, 0.0


def _aggregate(scores: dict[str, float]) -> float | None:
    """Mean of the normalized per-question values, or None when unscored."""
    if not scores:
        return None
    return sum(scores.values()) / len(scores)


def _failing_questions(candidate: CandidateResult, thresholds: dict[str, float]) -> list[str]:
    """Authored questions below their policy min (missing answers fail)."""
    failed: list[str] = []
    for name, minimum in thresholds.items():
        raw = candidate.raw_scores.get(name)
        if raw is None or raw < minimum:
            failed.append(name)
    return failed


def _pick_winner(candidates: list[CandidateResult]) -> CandidateResult:
    """Highest aggregate wins; then fewer threshold failures; then the earlier
    (lower) index. Refine/synthesis candidates insert at the front with index
    0, so an exact tie ships the text that won without the extra pass."""
    return max(
        candidates,
        key=lambda c: (
            c.aggregate if c.aggregate is not None else -1.0,
            -len(c.failing),
            -c.index,
        ),
    )


# ---------------------------------------------------------------------------
# Refine pass
# ---------------------------------------------------------------------------


async def _maybe_refine(
    *,
    daemon: Any,
    task: dict[str, Any],
    description: str,
    winner: CandidateResult,
    thresholds: dict[str, float],
    arbiter_model: str | None,
    arbiter_cfg: dict[str, Any],
    refine_cfg: dict[str, Any],
    selected_model: str | None,
    agent_type: str,
    system_message: str | None,
    task_mcp_config: dict[str, Any] | None,
    task_enable_config_discovery: Any,
    hooks: Any,
    expected_surface: Any,
    task_reasoning_effort: Any,
    org_id: str,
    requesting_user_id: str | None,
    audit_base: dict[str, Any],
    arbiter_usage: dict[str, int],
    refine_usage: dict[str, int],
) -> dict[str, Any]:
    """Bounded refine loop; returns a state dict, never raises.

    Each pass: a refinement session over the current best text plus a
    per-question critique, then a re-arbiter of the refined text. A pass is
    adopted only when its aggregate is STRICTLY better — a tie keeps the
    original text so a bounded loop can never oscillate or spend tokens for
    nothing. A refine failure never fails the task; the current best ships.
    """
    task_id = str(task.get("id") or "")
    try:
        max_passes = int(refine_cfg.get("max_refine_passes") or 1)
    except (TypeError, ValueError):
        max_passes = 1
    max_passes = max(1, min(2, max_passes))
    refine_model = str(refine_cfg.get("model") or selected_model or "")
    state: dict[str, Any] = {
        "passes": 0,
        "taken": False,
        "candidate": None,
        "model": refine_model or None,
    }
    if not arbiter_model or not arbiter_cfg.get("questions"):
        state["error"] = "refine requires an arbiter with questions"
        return state
    current_best = winner

    for pass_i in range(max_passes):
        if not refine_model:
            state["error"] = "no refine model configured"
            break
        critique = _refine_critique(current_best, thresholds, arbiter_cfg)
        prompt = (
            f"Execute this task:\n\n{description}\n\n"
            "--- Previous response (candidate) ---\n"
            + _truncate(
                current_best.result_text or "",
                _RESULT_PROMPT_BUDGET,
                label="previous response",
            )
            + "\n--- Critique ---\n"
            + critique
            + "\n\nProduce an improved full response to the ORIGINAL task. "
            "Address every point in the critique. Return the complete "
            "improved response only — no commentary about the critique."
        )
        session_name = f"{agent_type}-{task_id[:8]}-refine-{pass_i}"
        refined = CandidateResult(index=0, model=refine_model)
        try:
            result = await daemon.run_session(
                session_name,
                system_message,
                prompt,
                model=refine_model,
                reasoning_effort=task_reasoning_effort,
                mcp_config_override=task_mcp_config,
                enable_config_discovery=task_enable_config_discovery,
                hooks=hooks,
                expected_surface=expected_surface,
                audit_context=_session_audit_context(audit_base, "refine", pass_i),
            )
        except Exception as exc:
            state["error"] = f"refine session error: {exc}"
            break
        state["passes"] += 1
        _absorb_bookkeeping(daemon, session_name, refined, result)
        usage = (daemon._session_usage_totals or {}).pop(session_name, None)
        if isinstance(usage, dict):
            refine_usage["input_tokens"] += usage.get("input_tokens") or 0
            refine_usage["output_tokens"] += usage.get("output_tokens") or 0
        if refined.status != "completed" or not refined.result_text:
            state.setdefault("error", refined.error or f"refine pass {pass_i} produced no text")
            break

        # Re-arbiter the refined text as a standalone candidate.
        score_error, _refine_calls = await _score_candidates(
            arbiter_model=arbiter_model,
            arbiter_cfg=arbiter_cfg,
            candidates=[refined],
            task=task,
            description=description,
            request_id=str(task.get("request_id") or ""),
            usage_acc=arbiter_usage,
            org_id=org_id,
            user_id=requesting_user_id,
        )
        if score_error is not None:
            state["error"] = f"refine re-score failed: {score_error}"
            break

        baseline_aggregate = current_best.aggregate
        refined_aggregate = refined.aggregate
        taken = (
            refined_aggregate is not None
            and baseline_aggregate is not None
            and refined_aggregate > baseline_aggregate
        )
        state["taken"] = bool(taken)
        state["baseline_aggregate"] = baseline_aggregate
        state["refined_aggregate"] = refined_aggregate
        await _emit_event(
            task_id,
            "fusion_refine_done",
            (
                f"pass {pass_i + 1}: refine TAKEN "
                f"({_fmt(baseline_aggregate)} -> {_fmt(refined_aggregate)})"
                if taken
                else (
                    f"pass {pass_i + 1}: refine discarded "
                    f"({_fmt(baseline_aggregate)} >= {_fmt(refined_aggregate)})"
                )
            ),
            {
                "pass": pass_i + 1,
                "taken": bool(taken),
                "baseline_aggregate": baseline_aggregate,
                "refined_aggregate": refined_aggregate,
                "model": refine_model,
                "error": state.get("error"),
            },
        )
        if not taken:
            break
        refined.index = 0
        state["candidate"] = refined
        current_best = refined
    return state


def _refine_critique(
    candidate: CandidateResult,
    thresholds: dict[str, float],
    arbiter_cfg: dict[str, Any],
) -> str:
    """Per-question critique from the arbiter's view of the current best.

    Questions failing their min are marked SHORTFALL specifically; met or
    unthresholded questions are listed for incremental improvement.
    """
    lines: list[str] = []
    for raw in arbiter_cfg.get("questions") or []:
        name = str((raw or {}).get("name") or "")
        instructions = str((raw or {}).get("instructions") or "")
        raw_value = candidate.raw_scores.get(name)
        minimum = thresholds.get(name)
        achieved = f"{raw_value:.2f}" if raw_value is not None else "unanswered"
        if minimum is not None:
            if raw_value is None or raw_value < minimum:
                lines.append(
                    f"- {name}: SHORTFALL, fix specifically "
                    f"(required min {minimum:.2f}; achieved {achieved})"
                )
            else:
                lines.append(f"- {name}: met (achieved {achieved}); keep it")
        else:
            lines.append(f"- {name}: achieved {achieved} — improve if possible")
        lines[-1] += f" — {_truncate(instructions, _CRITIQUE_ITEM_BUDGET, label='question')}"
        if len(lines) >= 64:
            break
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


async def _maybe_synthesize(
    *,
    daemon: Any,
    task: dict[str, Any],
    description: str,
    candidates: list[CandidateResult],
    thresholds: dict[str, float],
    arbiter_model: str | None,
    arbiter_cfg: dict[str, Any],
    synthesis_cfg: dict[str, Any],
    selected_model: str | None,
    agent_type: str,
    system_message: str | None,
    task_mcp_config: dict[str, Any] | None,
    task_enable_config_discovery: Any,
    hooks: Any,
    expected_surface: Any,
    task_reasoning_effort: Any,
    org_id: str,
    requesting_user_id: str | None,
    audit_base: dict[str, Any],
    arbiter_usage: dict[str, int],
) -> dict[str, Any]:
    """One synthesis session over the successful candidates; a state dict.

    The synthesis result is re-scored once with the same arbiter (when one
    is configured — optional for synthesize mode) so thresholds and the
    comparison line speak for the text that actually ships.
    """
    task_id = str(task.get("id") or "")
    state: dict[str, Any] = {"candidate": None}
    synthesis_model = str(synthesis_cfg.get("model") or selected_model or "")
    if not synthesis_model:
        state["error"] = "no synthesis model configured"
        return state
    state["model"] = synthesis_model

    remaining = _SYNTHESIS_TOTAL_BUDGET
    sections: list[str] = []
    for candidate in candidates:
        budget = min(_SYNTHESIS_CANDIDATE_BUDGET, remaining)
        if budget <= 0:
            break
        text = _truncate(candidate.result_text or "", budget, label="candidate answer")
        remaining -= max(0, budget - len(text))
        sections.append(f"--- Candidate {candidate.index} (model: {candidate.model}) ---\n{text}")
    prompt = (
        f"Execute this task:\n\n{description}\n\n"
        "Several independent model candidates produced responses below. "
        "Blend them into one best response: carry every correct distinct "
        "insight from any candidate, drop repeated content and anything "
        "contradicted by a better-supported claim.\n\n"
        + "\n\n".join(sections)
        + "\n\nReturn the complete final response only."
    )
    session_name = f"{agent_type}-{task_id[:8]}-synth-0"
    started = time.monotonic()
    synthesized = CandidateResult(index=0, model=synthesis_model)
    try:
        result = await daemon.run_session(
            session_name,
            system_message,
            prompt,
            model=synthesis_model,
            reasoning_effort=task_reasoning_effort,
            mcp_config_override=task_mcp_config,
            enable_config_discovery=task_enable_config_discovery,
            hooks=hooks,
            expected_surface=expected_surface,
            audit_context=_session_audit_context(audit_base, "synthesis", 0),
        )
    except Exception as exc:
        state["error"] = f"synthesis session error: {exc}"
        return state
    _absorb_bookkeeping(daemon, session_name, synthesized, result)
    usage = (daemon._session_usage_totals or {}).pop(session_name, None)
    if isinstance(usage, dict):
        state["input_tokens"] = usage.get("input_tokens")
        state["output_tokens"] = usage.get("output_tokens")
    state["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if synthesized.status != "completed" or not synthesized.result_text:
        state["error"] = synthesized.error or "synthesis produced no usable result"
        return state

    if arbiter_model and arbiter_cfg.get("questions"):
        score_error, _synth_calls = await _score_candidates(
            arbiter_model=arbiter_model,
            arbiter_cfg=arbiter_cfg,
            candidates=[synthesized],
            task=task,
            description=description,
            request_id=str(task.get("request_id") or ""),
            usage_acc=arbiter_usage,
            org_id=org_id,
            user_id=requesting_user_id,
        )
        if score_error is not None:
            runtime.log(
                f"Fusion {task_id[:8]}: synthesis re-score failed ({score_error}); "
                "synthesis ships unscored",
                "WARN",
            )
            state["re_score_error"] = score_error
    state["candidate"] = synthesized
    return state


# ---------------------------------------------------------------------------
# Telemetry payloads
# ---------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k"


def _candidate_entry(candidate: CandidateResult) -> dict[str, Any]:
    return {
        "index": candidate.index,
        "model": candidate.model,
        "status": candidate.status,
        "input_tokens": candidate.input_tokens,
        "output_tokens": candidate.output_tokens,
        "output_chars": len(candidate.result_text or ""),
        "elapsed_ms": candidate.elapsed_ms,
        "aggregate": candidate.aggregate,
        "scores": {name: round(value, 4) for name, value in candidate.scores.items()},
        "raw_scores": {name: round(value, 4) for name, value in candidate.raw_scores.items()},
        "failing": list(candidate.failing),
        "excerpt": _truncate(
            candidate.result_text or "", _EXCERPT_BUDGET, label="candidate excerpt"
        ),
        "error": candidate.error,
    }


def _usage_totals(
    *,
    candidates: list[CandidateResult],
    arbiter_usage: dict[str, int],
    refine_usage: dict[str, int],
    synthesis_state: dict[str, Any] | None,
    degraded: bool,
) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "candidates_input": sum((c.input_tokens or 0) for c in candidates),
        "candidates_output": sum((c.output_tokens or 0) for c in candidates),
        "arbiter_input": arbiter_usage.get("input_tokens", 0),
        "arbiter_output": arbiter_usage.get("output_tokens", 0),
        "synthesis_input": (synthesis_state or {}).get("input_tokens") or 0,
        "synthesis_output": (synthesis_state or {}).get("output_tokens") or 0,
        "refine_input": refine_usage.get("input_tokens", 0),
        "refine_output": refine_usage.get("output_tokens", 0),
        "degraded": degraded,
    }
    totals["total_tokens"] = (
        totals["candidates_input"]
        + totals["candidates_output"]
        + totals["arbiter_input"]
        + totals["arbiter_output"]
        + totals["synthesis_input"]
        + totals["synthesis_output"]
        + totals["refine_input"]
        + totals["refine_output"]
    )
    return totals


def _metadata(
    *,
    mode: str,
    candidates: list[CandidateResult],
    ranked: list[CandidateResult],
    arbiter_model: str | None,
    arbiter_usage: dict[str, int],
    refine_usage: dict[str, int],
    winner: CandidateResult,
    degraded: bool,
    refine_state: dict[str, Any] | None,
    synthesis_state: dict[str, Any] | None,
    threshold_failed: bool,
    elapsed_ms: int,
    totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": mode,
        "candidates": [_candidate_entry(c) for c in ranked],
        "all_candidates": [
            {"index": c.index, "model": c.model, "status": c.status, "error": c.error}
            for c in candidates
        ],
        "arbiter": {
            "model": arbiter_model,
            "input_tokens": arbiter_usage.get("input_tokens", 0),
            "output_tokens": arbiter_usage.get("output_tokens", 0),
        },
        "refine": {
            "passes": (refine_state or {}).get("passes", 0),
            "taken": bool((refine_state or {}).get("taken", False)),
            "baseline_aggregate": (refine_state or {}).get("baseline_aggregate"),
            "refined_aggregate": (refine_state or {}).get("refined_aggregate"),
            "model": (refine_state or {}).get("model"),
            "input_tokens": refine_usage.get("input_tokens", 0),
            "output_tokens": refine_usage.get("output_tokens", 0),
            "error": (refine_state or {}).get("error"),
        },
        "winner": {
            "index": winner.index,
            "model": winner.model,
            "aggregate": winner.aggregate,
        },
        "degraded": degraded,
        "threshold_failed": threshold_failed,
        "elapsed_ms": elapsed_ms,
    }
    if synthesis_state:
        payload["synthesis"] = {
            "model": synthesis_state.get("model"),
            "input_tokens": synthesis_state.get("input_tokens"),
            "output_tokens": synthesis_state.get("output_tokens"),
            "elapsed_ms": synthesis_state.get("elapsed_ms"),
            "re_score_error": synthesis_state.get("re_score_error"),
        }
    if totals is not None:
        payload["totals"] = totals
    return payload


def _result_structured(
    *,
    mode: str,
    winner: CandidateResult,
    ranked: list[CandidateResult],
    arbiter_model: str | None,
    thresholds: dict[str, float],
    failed_names: list[str],
    degraded: bool,
) -> dict[str, Any]:
    """Typed payload stored as result_structured (downstream steps read it)."""
    fusion_payload: dict[str, Any] = {
        "mode": mode,
        "winner": {"model": winner.model, "aggregate": winner.aggregate},
        "candidates": [
            {
                "index": c.index,
                "model": c.model,
                "aggregate": c.aggregate,
                "scores": {name: round(value, 4) for name, value in c.scores.items()},
            }
            for c in ranked
        ],
        "arbiter_model": arbiter_model,
        "degraded": degraded,
    }
    if thresholds:
        fusion_payload["thresholds"] = {
            name: {"min": minimum, "met": name not in failed_names}
            for name, minimum in thresholds.items()
        }
        fusion_payload["threshold_failed"] = list(failed_names)
    return {"fusion": fusion_payload}


def _summary_prefix(
    *,
    mode: str,
    ranked: list[CandidateResult],
    arbiter_model: str | None,
    winner: CandidateResult,
    refine_state: dict[str, Any] | None,
    synthesis_state: dict[str, Any] | None,
    totals: dict[str, Any],
    degraded: bool,
) -> str:
    """One-line human comparison prepended to the completed task's summary."""
    scored = ", ".join(f"{c.model} {_fmt(c.aggregate)}" for c in ranked)
    parts = [f"[fusion {mode}]", f"{len(ranked)} candidates ({scored})", f"winner={winner.model}"]
    if degraded:
        parts.append("DEGRADED to 1 candidate")
    if arbiter_model:
        parts.append(f"arbiter={arbiter_model}")
    token_bits = [f"cand {_k(totals['candidates_input'] + totals['candidates_output'])}"]
    if totals["arbiter_input"] + totals["arbiter_output"]:
        token_bits.append(f"arbiter {_k(totals['arbiter_input'] + totals['arbiter_output'])}")
    if totals["synthesis_input"] + totals["synthesis_output"]:
        token_bits.append(f"synth {_k(totals['synthesis_input'] + totals['synthesis_output'])}")
    if (refine_state or {}).get("passes"):
        token_bits.append(
            f"refine {int(refine_state['taken'])}/{refine_state['passes']} "
            f"({_k(totals['refine_input'] + totals['refine_output'])})"
        )
    parts.append(f"tokens: {' + '.join(token_bits)}")
    return "; ".join(parts)
