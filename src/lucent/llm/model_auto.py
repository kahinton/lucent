"""Auto model selection: rule filters + a decision-model choice question.

The shared core of flag-gated "auto" model selection (default off —
``models.auto_selection_enabled``): given the models accessible to a
principal and the task/message context, first apply hard rule filters
(capability fit, context-window vs the estimated prompt size), then ask a
decision-class model (Ollama 0.35+ System One via
:func:`lucent.llm.decision.decide`) ONE ``choice`` question — which
surviving model best fits the work. The answer carries per-option
probabilities; a confidence floor, a tie-break toward the ordinary default
chain's pick, and any resolution failure all fall back to the exact
pre-auto chain the caller passes as ``fallback_pick`` — so a degraded
arbiter is behaviorally identical to the flag being off.

The core is deliberately I/O-lean: it needs no database pool of its own.
Callers pass the already-ACL-filtered candidate list (``ModelInfo`` objects
from the daemon's registry or plain row dicts from the server's
``ModelRepository.list_models_accessible_by``) and record usage themselves
(``engine="decision"``, like every other decision-model consumer).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from lucent.llm.decision import (
    ChoiceAnswer,
    DecisionError,
    DecisionQuestion,
    DecisionRequest,
    decide,
    resolve_decision_model_id,
)

# The reserved model-id sentinel meaning "pick the best model for me".
MODEL_AUTO = "auto"

ARBITER_TIMEOUT_SECONDS = float(
    os.environ.get("LUCENT_MODEL_AUTO_TIMEOUT_SECONDS", "60")
)

# State budgets (chars) — mirror the fusion arbiter's constants; the models
# table is rendered one line per candidate so the aggregate cap dominates.
_TASK_STATE_BUDGET = 2_000
_MODEL_LINE_BUDGET = 200
_MODELS_STATE_TOTAL = 8_000

# Client-side cap on options sent to the arbiter (protocol allows 255; a
# 32-wide choice keeps the question answerable and the state small).
_MAX_AUTO_OPTIONS = 32

# If the top-two probabilities are closer than this margin and the default
# chain's pick is the runner-up, keep the default chain's pick.
_TIE_MARGIN = 0.10

# Headroom added to the estimated prompt size before comparing against a
# model's context window (guards under-counted injected context).
_CONTEXT_HEADROOM_TOKENS = 8_000

_CHOICE_QUESTION = "best_model"


def is_auto_model(model: str | None) -> bool:
    """Whether a model field carries the auto sentinel."""
    if not isinstance(model, str):
        return False
    return model.strip().lower() == MODEL_AUTO


AUTO_DISABLED_MESSAGE = "Auto model selection is disabled"


def split_auto_sentinel(model: str | None, *, enabled: bool) -> tuple[bool, str | None]:
    """Classify a model value at authoring time.

    Returns ``(is_auto_sentinel, error)``: ``(False, None)`` for ordinary
    values (validate/access checks proceed unchanged); ``(True, None)`` when
    the sentinel is authored while auto selection is enabled (skip those
    checks — the daemon resolves the real model later); ``(True, error)``
    when the sentinel is authored while disabled, with ``AUTO_DISABLED_MESSAGE``.
    """
    if not is_auto_model(model):
        return False, None
    if enabled:
        return True, None
    return True, AUTO_DISABLED_MESSAGE


def estimate_prompt_tokens(*texts: str | None) -> int:
    """Rough token estimate (4 chars/token) for the given texts, ignoring blanks."""
    return sum(len(text) for text in texts if text) // 4


def _model_field(model: object, name: str) -> object:
    """Read a metadata field from either a ModelInfo or a row dict."""
    if isinstance(model, dict):
        return model.get(name)
    return getattr(model, name, None)


def _model_id(model: object) -> str:
    return str(_model_field(model, "id") or "")


def _model_ids(models: Sequence[object]) -> list[str]:
    return [_model_id(model) for model in models]


def _estimate_context_window(model: object) -> int:
    value = _model_field(model, "context_window")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_PARAM_TAG_RE = re.compile(r"^(\d+(?:\.\d+)?)b$", re.IGNORECASE)


def _model_param_scale(model: object) -> str | None:
    """Parameter count from the model's tags (``"763b"`` → ``"763B"``).

    Models carry parameter counts only as tagging convention (there is no
    numeric column), so this is a best-effort read of the tag list. Returns
    None when no tag looks like a size — the arbiter's criteria simply omit
    it rather than guessing.
    """
    tags = _model_field(model, "tags") or []
    for tag in tags:
        match = _PARAM_TAG_RE.match(str(tag).strip())
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            # Display integers without a decimal point (31B, not 31.0B).
            number = f"{value:g}"
            return f"{number}B"
    return None


def apply_rule_filters(
    models: Sequence[object],
    *,
    require_vision: bool = False,
    estimated_prompt_tokens: int = 0,
) -> tuple[list[object], list[tuple[str, str]]]:
    """Hard filters every candidate must pass; returns (survivors, dropped).

    Dropped entries are ``(model_id, reason)`` pairs. Tool support is
    guaranteed by every caller's candidate pool (the daemon's task pool and
    the chat catalog are already tools-filtered/classified), so this filter
    only enforces vision and context-window fit. A model with an unknown
    context window (0) skips the context filter — the window is never
    guessed, and unknown models are never dropped on suspicion.
    """
    survivors: list[object] = []
    dropped: list[tuple[str, str]] = []
    needed = estimated_prompt_tokens + _CONTEXT_HEADROOM_TOKENS
    for model in models:
        model_id = _model_id(model)
        if require_vision and not bool(_model_field(model, "supports_vision")):
            dropped.append((model_id, "does not support image input"))
            continue
        window = _estimate_context_window(model)
        if window > 0 and window < needed:
            dropped.append(
                (model_id, f"context window {window} below estimate {needed}")
            )
            continue
        survivors.append(model)
    return survivors, dropped


def build_task_state(
    *,
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    request_title: str | None = None,
    prompt_excerpt: str | None = None,
) -> dict[str, str]:
    """Named state fields describing the work, within budget.

    Mirrors the ``add()``-helper style of
    ``daemon/review/decision_gate.build_decision_state``: a running budget
    that shrinks later fields rather than guessing.
    """
    state: dict[str, str] = {}
    used = 0

    def add(key: str, text: str, budget: int) -> None:
        nonlocal used
        text = (text or "").strip()
        if not text or used >= _TASK_STATE_BUDGET:
            return
        remaining = _TASK_STATE_BUDGET - used
        budget = min(budget, remaining)
        snippet = text[:budget]
        state[key] = snippet
        used += len(snippet)

    if agent_type:
        # agent_type is a tiny enum-like token but still counts against the
        # budget so the state total can never exceed the cap.
        state["agent_type"] = agent_type
        used += len(agent_type)
    add("task_title", title, 300)
    add("request_title", request_title, 300)
    add("task_description", description, _TASK_STATE_BUDGET)
    add("task_excerpt", prompt_excerpt, _TASK_STATE_BUDGET)
    return state


def model_line_for_state(model: object) -> str:
    """One-line metadata summary for the arbiter's state."""
    model_id = _model_id(model)
    name = str(_model_field(model, "name") or model_id)
    category = str(_model_field(model, "category") or "general")
    tags = _model_field(model, "tags") or []
    window = _estimate_context_window(model)
    scale = _model_param_scale(model)
    notes = str(_model_field(model, "notes") or "").strip()
    head = f"{name} [{category}"
    if tags:
        head += f", {', '.join(str(tag) for tag in tags[:4])}"
    head += "]"
    parts = [head]
    if scale:
        # Surface the parameter count as its own part: raw "763b" tags are
        # tagging convention, and a small judge model reads "763B parameters"
        # far more reliably than a tag it may treat as an opaque id.
        parts.append(f"{scale} parameters")
    if window:
        parts.append(f"context {window} tokens")
    if notes:
        parts.append(notes)
    line = f"{model_id}: " + "; ".join(parts)
    return line[:_MODEL_LINE_BUDGET]


def build_auto_questions(models: Sequence[object]) -> dict[str, DecisionQuestion]:
    """The single ``choice`` question over surviving model ids.

    Constraints (image input, context size) are enforced by the rule
    filter — :func:`decide` answers each question independently, so a
    companion ``noul`` question could never constrain this choice. Soft
    preferences travel in the instructions.
    """
    criteria: dict[str, str] = {}
    for model in models:
        model_id = _model_id(model)
        if not model_id or model_id in criteria:
            continue
        name = str(_model_field(model, "name") or model_id)
        category = str(_model_field(model, "category") or "")
        # The parameter scale rides the criteria label: the choice answer is
        # made against these lines, so size must be visible here and not
        # only in the state's model block.
        scale = _model_param_scale(model)
        label_bits = [category] if category else []
        if scale:
            label_bits.append(f"{scale} params")
        criteria[model_id] = f"{name} ({', '.join(label_bits)})" if label_bits else name
    return {
        _CHOICE_QUESTION: DecisionQuestion(
            name=_CHOICE_QUESTION,
            type="choice",
            instructions=(
                "Given the task and each model's metadata, which model best "
                "fits this work? Match model size to the request: simple, "
                "short, or well-scoped requests (greetings, quick factual "
                "questions, one-step tasks) should go to a small or "
                "lightweight model — a much larger model adds latency and "
                "cost with no benefit. Prefer fast/general models for narrow "
                "work; reasoning-class models for analysis or long-horizon "
                "problems; agentic/coding models for multi-step tool-driven "
                "work. Weigh each option's context window against the task's "
                "size, and use the parameter count when shown to compare "
                "options' weight."
            ),
            criteria=criteria,
        ),
    }


@dataclass(slots=True)
class AutoChoice:
    """The outcome of one auto-selection attempt.

    ``source`` is ``"arbiter"`` when the decision model's pick was adopted
    and ``"fallback"`` when the ordinary default chain decided (a disabled
    flag never reaches this core — the caller resolves before calling).
    """

    model_id: str
    source: str  # "arbiter" | "fallback"
    reason: str
    fallback_reason: str | None = None
    survivors: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    arbiter_model_id: str | None = None
    options_truncated: int = 0
    usage: tuple[int, int] | None = None


async def _decide_with_retry(
    request: DecisionRequest,
) -> tuple[object, str | None]:
    """One deterministic retry on DecisionError — decide() is stateless."""
    last_error: str | None = None
    for _attempt in range(2):
        try:
            return await decide(request, timeout=ARBITER_TIMEOUT_SECONDS), None
        except DecisionError as exc:
            last_error = str(exc)
    return None, last_error


def _argmax(probabilities: dict[str, float]) -> tuple[str | None, float]:
    """(top option, top probability); (None, 0.0) for empty input."""
    if not probabilities:
        return None, 0.0
    top_id = max(probabilities, key=lambda key: probabilities[key])
    return top_id, probabilities[top_id]


def _runner_up(probabilities: dict[str, float], top_id: str) -> tuple[str | None, float]:
    """(second-best option, its probability), excluding ``top_id``."""
    others = {key: value for key, value in probabilities.items() if key != top_id}
    return _argmax(others)


async def _resolve_arbiter_model(
    explicit: str | None, org_id: object
) -> str | None:
    """Explicit setting id → first enabled decision model (fail-open)."""
    if explicit:
        return explicit
    org_str = str(org_id) if org_id else None
    for prefer_db in (True, False):
        try:
            resolved = await resolve_decision_model_id(
                explicit_model_id=None,
                organization_id=org_str,
                prefer_db=prefer_db,
            )
        except Exception:  # noqa: BLE001 — resolution fails open
            resolved = None
        if resolved:
            return resolved
    return None


def _default_confidence_floor() -> float:
    """Settings-backed confidence floor; settings failure keeps the default."""
    try:
        from lucent import settings

        return settings.auto_selection_confidence()
    except Exception:  # noqa: BLE001 — settings reads fail open
        return 0.6


def select_model_auto(
    *,
    models: Sequence[object],
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    request_title: str | None = None,
    prompt_excerpt: str | None = None,
    require_vision: bool = False,
    extra_prompt_tokens: int = 0,
    org_id: object = None,
    decision_model_id: str | None = None,
    confidence_floor: float | None = None,
    fallback_pick: Callable[[], tuple[str, str]],
) -> object:
    """Awaitable: pick a model — rule filters, then one choice question.

    ``fallback_pick`` returns ``(model_id, reason)`` for the ordinary
    behavior — literally today's default chain — and decides every
    degraded outcome: no decision model, no survivors, endpoint errors,
    below-floor confidence, and ties the default pick wins.
    """
    return _select_model_auto(
        models=models,
        agent_type=agent_type,
        title=title,
        description=description,
        request_title=request_title,
        prompt_excerpt=prompt_excerpt,
        require_vision=require_vision,
        extra_prompt_tokens=extra_prompt_tokens,
        org_id=org_id,
        decision_model_id=decision_model_id,
        confidence_floor=confidence_floor,
        fallback_pick=fallback_pick,
    )


def _safe_fallback_pick(
    fallback_pick: Callable[[], tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Advisory fallback pick — a pick failure must never abort the arbiter path."""
    try:
        return fallback_pick()
    except Exception as error:  # noqa: BLE001 — advisory only
        return None, str(error)


async def _select_model_auto(
    *,
    models: Sequence[object],
    agent_type: str | None,
    title: str | None,
    description: str | None,
    request_title: str | None,
    prompt_excerpt: str | None,
    require_vision: bool,
    extra_prompt_tokens: int,
    org_id: object,
    decision_model_id: str | None,
    confidence_floor: float | None,
    fallback_pick: Callable[[], tuple[str, str]],
) -> AutoChoice:
    if confidence_floor is None:
        confidence_floor = _default_confidence_floor()

    arbiter_model = await _resolve_arbiter_model(decision_model_id, org_id)
    estimated = extra_prompt_tokens + estimate_prompt_tokens(
        agent_type or "", title or "", description or "", request_title or "",
        prompt_excerpt or "",
    )
    survivor_models, dropped = apply_rule_filters(
        models,
        require_vision=require_vision,
        estimated_prompt_tokens=estimated,
    )
    survivor_ids = _model_ids(survivor_models)

    def _fallback(fallback_reason: str) -> AutoChoice:
        picked, reason = fallback_pick()
        return AutoChoice(
            model_id=picked,
            source="fallback",
            reason=reason,
            fallback_reason=fallback_reason,
            survivors=survivor_ids,
            dropped=dropped,
            arbiter_model_id=arbiter_model,
        )

    if not arbiter_model:
        return _fallback("Auto selection has no decision-class arbiter model")
    if not survivor_ids:
        return _fallback("rule filters left no eligible model")
    if len(survivor_models) == 1:
        # A single survivor is a decision, not a guess — skip the arbiter.
        only = _model_id(survivor_models[0])
        return AutoChoice(
            model_id=only,
            source="arbiter",
            reason="only one model passes the auto-selection rule filters",
            survivors=survivor_ids,
            dropped=dropped,
            probabilities={only: 1.0},
            confidence=1.0,
            arbiter_model_id=arbiter_model,
        )

    # Cap the option list; the default chain's pick is always kept in the
    # pool so a tie can still gravitate toward the conservative choice.
    fallback_model_id, _ = _safe_fallback_pick(fallback_pick)
    ordered = list(survivor_models)
    truncated = 0
    if len(ordered) > _MAX_AUTO_OPTIONS:
        kept_head = [m for m in ordered[: _MAX_AUTO_OPTIONS]]
        if fallback_model_id and _model_id(fallback_model_id) not in _model_ids(
            kept_head
        ):
            tail_pick = next(
                (m for m in ordered if _model_id(m) == fallback_model_id), None
            )
            if tail_pick is not None:
                kept_head[-1] = tail_pick
        truncated = len(ordered) - len(kept_head)
        ordered = kept_head

    state = build_task_state(
        agent_type=agent_type,
        title=title,
        description=description,
        request_title=request_title,
        prompt_excerpt=prompt_excerpt,
    )
    model_lines = [model_line_for_state(model) for model in ordered]
    usable = model_lines
    while usable and sum(len(line) for line in usable) > _MODELS_STATE_TOTAL:
        usable = usable[:-1]
    state["models"] = "\n".join(usable)

    request = DecisionRequest(
        model_id=arbiter_model,
        state=state,
        questions=build_auto_questions(ordered),
        keep_alive="30m",
    )
    response, error = await _decide_with_retry(request)
    if response is None:
        return _fallback(f"Auto-selection arbiter failed: {error}")
    usage = response.usage
    choice = response.answers.get(_CHOICE_QUESTION)
    if not isinstance(choice, ChoiceAnswer) or choice.choice not in _model_ids(ordered):
        return _fallback("arbiter returned an unknown or missing model option")

    probabilities = dict(choice.probabilities or {})
    top_id, top_p = _argmax(probabilities)
    if not top_id:
        # No per-option probabilities surfaced — trust the bare choice only
        # at or above the confidence floor.
        top_id, top_p = choice.choice, choice.confidence
    if top_id not in survivor_ids:
        return _fallback("arbiter probabilities favor a filtered-out model")
    if top_p < confidence_floor:
        return _fallback(
            f"arbiter top probability {top_p:.2f} below confidence floor "
            f"{confidence_floor:.2f}"
        )
    _runner_id, runner_p = _runner_up(probabilities, top_id)
    if (
        fallback_model_id
        and top_p - runner_p < _TIE_MARGIN
        and _runner_id == fallback_model_id
    ):
        return _fallback(
            f"arbiter tied between {top_id} and {_runner_id} "
            f"(margin {top_p - runner_p:.2f}); default-chain pick wins the tie"
        )

    return AutoChoice(
        model_id=top_id,
        source="arbiter",
        reason=(
            f"Auto-selection arbiter chose {top_id} (p={top_p:.2f}, "
            f"confidence={choice.confidence:.2f})"
        ),
        survivors=survivor_ids,
        dropped=dropped,
        probabilities=probabilities,
        confidence=choice.confidence,
        arbiter_model_id=arbiter_model,
        options_truncated=truncated,
        usage=(usage.input_tokens, usage.output_tokens) if usage else None,
    )
