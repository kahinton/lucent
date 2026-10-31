"""Decision-model relevance gate for the message memory injection hook.

Second application of the System One decision client (
:mod:`lucent.llm.decision`): after the absolute similarity floor in
``_run_message_memory_lookup_hook``, the surviving candidates plus a bounded
conversation state are sent to a decision-class model in ONE
``/v1/systemone`` call — one ``noul`` question per candidate — and only
candidates the model scores at or above
``memory.inject_decision_gate_threshold`` are injected.

While the gate is active it is the relevance authority, so the two
recall-suppressing heuristics ahead of it stay gate-off only: the per-window
score-spread cut (it can drop a relevant memory because an unrelated top hit
anchored the window) and the cross-turn dedup set (hook context is transient,
so a memory injected on an earlier turn is no longer in the model's context
and must be re-evaluated rather than permanently suppressed).

Placement (hooks.py): the gate sits after the similarity floor and before
the ``inject_max_memories`` cap, because gating the already-capped set could
only confirm what the similarity threshold picked and never sharpen
anything. With the gate active the hook collects every similarity-qualified
candidate (bypassing the gate-off score-spread and cross-turn dedup rules)
so the model — not a prefilter — decides what is relevant. Candidate search
previews (~1000 chars) are used as the memory state; the batched
``get_memories`` full-content fetch stays after selection, so the gate never
moves a larger speculative call ahead of the model.

Fail-open everywhere, enforced by the CALLER (the hook wraps this module
and must never let an exception escape — ``HookManager`` renders escaping
hook exceptions as a visible "hook error:" chip): a ``DecisionError``,
timeout, or unparseable response leaves the pre-gate survivor set intact.
The one fail-closed sub-case is a missing/malformed answer for a single
candidate inside an otherwise-complete response — that candidate is
dropped, mirroring the request pre-review gate's conservatism.

Question names are generated here (``memory_<n>``) rather than derived
from memory ids, so weird id strings can never collide with the 128-char
question-name limit; the id stays inside the state field body.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from lucent.llm.decision import (
    DecisionQuestion,
    DecisionRequest,
    DecisionResponse,
)
from lucent.llm.decision import (
    decide as _decide,
)

# The merged multi-query candidate pool can exceed the server's 64-question
# limit (up to 8 query windows x ~30 results), so the gate window is capped and
# candidates beyond it are dropped (never allowed to backfill the cap — the
# model already judged the conversation does not call for that many).
#
# The cap is also the dominant latency control. The endpoint answers each
# question in its own pass over the state, so cost is roughly linear in the
# question count AND in the state size. Measured on the reference machine
# (nimble, num_ctx 8192, trimmed budgets below):
#
#     questions | input tokens | warm wall
#           1    |        610   |   4.0s   (includes a cold load)
#           3    |      3,261   |   6.6s
#           4    |      5,308   |   8.2s
#           6    |     10,830   |  11.5s
#           8    |     18,264   |  15.0s
#          16    |      ~67,000 |  28s     (also exceeds the 8192-token context)
#
# Six is the largest window whose worst case still leaves room under the default
# timeout for a cold model load. It costs nothing in practice: production turns
# inject 0-3 memories, and the injection cap is 10, so candidates past the sixth
# were going to be dropped by rank anyway.
MEMORY_INJECT_DECISION_MAX_QUESTIONS = 6
# Pre-model-call latency budget: this gate runs inline before the model
# answers the user (the request pre-review gate is a background path and
# can afford 30s; this one cannot). Overridable per org with
# ``memory.inject_decision_gate_timeout_seconds``.
MEMORY_INJECT_DECISION_TIMEOUT_SECONDS = 15.0
_MEMORY_INJECT_DECISION_KEEP_ALIVE = "30m"
# Payload budgets. These are a LATENCY control, not just a size control.
#
# The decision endpoint is quadratic in the question count: measured input
# tokens are ~64 x questions^2 for a fixed state, and wall time tracks input
# tokens at roughly 1.5-2 ms/token on the reference machine. A 16-question
# screen built from the original budgets (4000-char user text + 4000-char
# history + 16 x 500-char memories) came to ~67k input tokens, needed ~28s, and
# -- because the model's context is 8192 tokens -- could not be answered at
# all: the server rejects an oversized prompt instead of truncating it.
#
# The budgets below hold a full 16-candidate screen inside what the model can
# accept and what the inline path can absorb. This is a coarse yes/no about
# topical relevance between one message and one memory preview; a couple of
# sentences of context resolve it, and the extra characters only cost time.
_MEMORY_INJECT_DECISION_USER_TEXT_BUDGET = 1200
_MEMORY_INJECT_DECISION_HISTORY_BUDGET = 1200
# Keep the conversation window short. The decision model is a small classifier
# and long meta-conversations (especially ones discussing gating/injection)
# bleed into the relevance judgment: with the previous 8-message window a
# memory that literally contained the phrase the user asked about scored
# 0.02-0.59 and was filtered, while the same state with the last 4 messages
# scored 0.98-0.99. Two turns are enough to resolve pronouns; more is noise.
_MEMORY_INJECT_DECISION_HISTORY_MESSAGES = 4
_MEMORY_INJECT_DECISION_PROJECT_BUDGET = 200
_MEMORY_INJECT_DECISION_MEMORY_BUDGET = 360
_MEMORY_INJECT_DECISION_CONTENT_BUDGET = 280

# The endpoint rejects an oversized prompt outright ("prompt 0 has N tokens;
# expected 1-M") rather than truncating it, so an over-budget screen is a hard
# failure, not a slow one. Decision models are small (nimble runs at num_ctx
# 8192) and the request grows with the SQUARE of the question count, so the
# gate checks the assembled prompt against the model's context before sending
# and drops candidates until it fits. Dropping candidates fails open in the
# only direction that matters: the survivors are still screened, and the
# dropped ones are injected unfiltered because the gate never saw them.
_MEMORY_INJECT_DECISION_CONTEXT_TOKENS = 8192
# Deliberately loose: ~3.4 characters per token under-counts prose tokenization,
# so this is a ceiling on characters, not a token estimate. It exists to stop a
# pathological payload, not to tune throughput.
_MEMORY_INJECT_DECISION_CONTEXT_CHARS = int(
    _MEMORY_INJECT_DECISION_CONTEXT_TOKENS * 2.6
)


def _state_chars(state: dict[str, str]) -> int:
    return sum(len(str(value)) for value in state.values())


def _single_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _truncate(text: str, budget: int) -> str:
    line = _single_line(text)
    return line if len(line) <= budget else line[:budget]


@dataclass(slots=True)
class InjectionGateOutcome:
    """Result of one decision-model screen over the gated candidate window."""

    verdict: str  # "kept" | "filtered" | "skipped"
    model_id: str | None
    threshold: float
    scores: dict[str, float]  # question/field name → relevance probability
    kept_fields: frozenset[str] = dc_field(default_factory=frozenset)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: Wall time for the decision call. Recorded because the gate runs inline
    #: before the model answers: a slow screen is a latency regression and,
    #: past the timeout, a silent loss of filtering. Without this number the
    #: only visible symptom is a fail-open warning with no magnitude.
    duration_ms: int = 0

    def metadata(self) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "threshold": self.threshold,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "question_count": len(self.scores),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "duration_ms": self.duration_ms,
        }


def build_gate_state(
    *,
    user_text: str,
    messages: list[dict[str, Any]] | None,
    session_state: dict[str, Any] | None,
    candidates: list[tuple[dict[str, Any], float]],
) -> tuple[dict[str, str], list[tuple[str, dict[str, Any]]]]:
    """Build the decision state plus the gated candidate window.

    Named fields: ``user_message`` (the turn text), ``conversation`` (the
    last few non-tool/non-system messages), ``project`` (chat project name,
    fail-closed), and ``memory_<n>`` per gated candidate. Empty fields are
    omitted entirely. The gated window is the candidate list capped at
    ``MEMORY_INJECT_DECISION_MAX_QUESTIONS``.
    """
    state: dict[str, str] = {}

    user_line = _truncate(user_text, _MEMORY_INJECT_DECISION_USER_TEXT_BUDGET)
    if user_line:
        state["user_message"] = user_line

    history = _conversation_lines(messages, skip=_single_line(user_text))
    if history:
        state["conversation"] = history

    project = _project_name(session_state)
    if project:
        state["project"] = f"Chat project: {project}"[:_MEMORY_INJECT_DECISION_PROJECT_BUDGET]

    gated: list[tuple[str, dict[str, Any]]] = []
    for memory, _score in candidates[:MEMORY_INJECT_DECISION_MAX_QUESTIONS]:
        field = f"memory_{len(gated) + 1}"
        state[field] = _memory_state(memory)
        gated.append((field, memory))
    return state, gated


def _conversation_lines(
    messages: list[dict[str, Any]] | None, *, skip: str
) -> str:
    """Render the last few conversational messages under the byte budget.

    Walks backwards over ``messages`` (as serialized for hooks: system /
    user / assistant / tool dicts with string content), skipping tool
    results and boilerplate system lines plus the trailing user message
    that is already shown as ``user_message``.
    """
    if not messages:
        return ""
    lines: list[str] = []
    used = 0
    skipped_current = False
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role in ("tool", "system"):
            continue
        content = _single_line(message.get("content") or "")[:500]
        if not content:
            continue
        if role == "user" and not skipped_current and content == skip:
            # The trailing user message is already the user_message field.
            skipped_current = True
            continue
        line = f"{role}: {content}"
        if used + len(line) + 1 > _MEMORY_INJECT_DECISION_HISTORY_BUDGET:
            break
        used += len(line) + 1
        lines.append(line)
        if len(lines) >= _MEMORY_INJECT_DECISION_HISTORY_MESSAGES:
            break
    return "\n".join(reversed(lines))


def _project_name(session_state: dict[str, Any] | None) -> str | None:
    """Fail-closed chat project name for the state (absent/malformed → None)."""
    if not session_state:
        return None
    context = session_state.get("_project_context")
    if not isinstance(context, dict):
        return None
    project = context.get("project")
    if not isinstance(project, dict):
        return None
    name = str(project.get("name") or "")
    return name or None


def _memory_state(memory: dict[str, Any]) -> str:
    tags = ", ".join(
        str(tag) for tag in (memory.get("tags") or [])[:8] if str(tag).strip()
    )
    parts = []
    # The id prefix lets the decision model match an explicit user reference
    # ("the memory ending in 4f4bd136") to the right candidate. Question names
    # stay memory_<n>, so odd id strings can never leak into the 128-char
    # question-name limit; the id is only ever body text.
    memory_id = str(memory.get("id") or "")[:8]
    if memory_id:
        parts.append(f"Id: {memory_id}")
    if tags:
        parts.append(f"Tags: {tags}")
    memory_type = str(memory.get("type") or "").strip()
    if memory_type:
        parts.append(f"Type: {memory_type}")
    content = _truncate(memory.get("content"), _MEMORY_INJECT_DECISION_CONTENT_BUDGET)
    parts.append(f"Content: {content}")
    return " | ".join(parts)


# One question per candidate, and the instruction is repeated for every one of
# them. At 16 candidates a 260-character instruction is ~4.2k tokens of pure
# duplication -- more than half the model's context -- so it is deliberately
# terse. The fields are already named ``memory_<n>``; the question only has to
# ask whether that field is worth injecting.
_QUESTION_TEMPLATE = (
    "Is the memory in field {field} directly relevant to this turn's user "
    "message and conversation, and worth injecting?"
)


def build_gate_questions(gated: list[tuple[str, dict[str, Any]]]) -> dict[str, DecisionQuestion]:
    """One ``noul`` relevance question per gated candidate."""
    return {
        name: DecisionQuestion(
            name=name,
            type="noul",
            instructions=_QUESTION_TEMPLATE.format(field=name),
        )
        for name, _memory in gated
    }


def evaluate_gate_response(
    response: DecisionResponse,
    gated: list[tuple[str, dict[str, Any]]],
    *,
    threshold: float,
) -> tuple[dict[str, float], list[str], bool]:
    """Split a decision response into (scores, kept fields, complete).

    A candidate with a missing/malformed answer is dropped (fail-closed for
    that candidate, mirroring the request pre-review gate). ``complete`` is
    False when the response overall is unusable (no answers at all) so the
    caller can fail open instead of filtering everything.
    """
    scores: dict[str, float] = {}
    kept_fields: list[str] = []
    complete = False
    for field, _memory in gated:
        answer = (response.answers or {}).get(field) if response.answers else None
        probability: float | None = None
        if answer is not None and getattr(answer, "noul", None) is not None:
            try:
                probability = float(answer.noul)
            except (TypeError, ValueError):
                probability = None
        if probability is None:
            score = 0.0
        else:
            score = probability
            complete = True
        scores[field] = score
        if score >= threshold:
            kept_fields.append(field)
    return scores, kept_fields, complete


def fits_context(
    state: dict[str, str],
    gated: list[tuple[str, dict[str, Any]]],
) -> bool:
    """Whether this screen can be sent at all.

    The endpoint rejects an oversized prompt instead of truncating it, so an
    over-budget screen is a hard failure, not a slow one. Screening a *subset*
    is not an acceptable workaround: the caller's kept-field bookkeeping is
    built from the full candidate list, so every candidate the gate never saw
    would be counted as unrelated and silently dropped. Refusing the whole
    screen keeps the failure open.
    """
    return bool(gated) and _state_chars(state) <= _MEMORY_INJECT_DECISION_CONTEXT_CHARS


async def run_injection_gate(
    model_id: str,
    state: dict[str, str],
    gated: list[tuple[str, dict[str, Any]]],
    *,
    threshold: float,
    timeout: float | None = None,
) -> InjectionGateOutcome:
    """Run the screen end-to-end; does NOT swallow exceptions (caller wraps).

    ``timeout`` defaults to the org's ``memory.inject_decision_gate_timeout_seconds``
    setting so an operator can widen the budget on slower hardware without a
    code change.

    When the assembled prompt cannot fit the model's context the outcome is
    ``skipped``, which the caller treats as fail-open: the pre-gate candidates
    are injected unfiltered. Returning per-candidate zeros instead would read
    as "unrelated" and silently drop every memory, which is the opposite
    failure mode.
    """
    if timeout is None:
        try:
            from lucent.settings import message_inject_decision_gate_timeout_seconds

            timeout = message_inject_decision_gate_timeout_seconds()
        except Exception:  # noqa: BLE001 — a settings failure must not disable the gate
            timeout = MEMORY_INJECT_DECISION_TIMEOUT_SECONDS
    if not fits_context(state, gated):
        # Refuse rather than screen a truncated window: the caller's kept-field
        # bookkeeping is built from the FULL candidate list, so screening a
        # subset would make every dropped candidate look unrelated.
        return InjectionGateOutcome(
            verdict="skipped",
            model_id=model_id,
            threshold=threshold,
            scores={},
            error=(
                "decision prompt exceeded the model context window; "
                "candidates were injected unfiltered"
            ),
        )
    request = DecisionRequest(
        model_id=model_id,
        state=state,
        questions=build_gate_questions(gated),
        keep_alive=_MEMORY_INJECT_DECISION_KEEP_ALIVE,
    )
    started = time.monotonic()
    response = await _decide(request, timeout=timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    scores, kept_fields, complete = evaluate_gate_response(response, gated, threshold=threshold)
    if not complete:
        # No candidate answer was parseable — treat the whole screen as
        # skipped so the caller fails open instead of filtering everything.
        return InjectionGateOutcome(
            verdict="skipped",
            model_id=str(response.model or model_id),
            threshold=threshold,
            scores=scores,
            error="decision response had no usable answers",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=duration_ms,
        )
    return InjectionGateOutcome(
        verdict="kept" if len(kept_fields) == len(gated) else "filtered",
        model_id=str(response.model or model_id),
        threshold=threshold,
        scores=scores,
        kept_fields=frozenset(kept_fields),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=duration_ms,
    )
