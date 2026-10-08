"""Decision-model relevance gate for the message memory injection hook.

Second application of the System One decision client (
:mod:`lucent.llm.decision`): after the heuristic filters in
``_run_message_memory_lookup_hook`` (similarity floor, score-drop spread,
session dedup), the surviving candidates plus a bounded conversation state
are sent to a decision-class model in ONE ``/v1/systemone`` call — one
``noul`` question per candidate — and only candidates the model scores at
or above ``memory.inject_decision_gate_threshold`` are injected.

Placement (hooks.py): the gate sits between dedup and the
``inject_max_memories`` cap, because gating the already-capped set could
only confirm what the similarity threshold picked and never sharpen
anything. Candidate search previews (~1000 chars) are used as the memory
state; the batched ``get_memories`` full-content fetch stays after
selection, so the gate never moves a larger speculative call ahead of the
model.

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
# limit (up to 8 query windows x ~30 results), so the gate window is capped
# and candidates beyond it are dropped (never allowed to backfill the cap —
# the model already judged the conversation does not call for that many).
MEMORY_INJECT_DECISION_MAX_QUESTIONS = 16
# Pre-model-call latency budget: this gate runs inline before the model
# answers the user (the request pre-review gate is a background path and
# can afford 30s; this one cannot). Warm inference is
# tens-of-milliseconds; the budget is set so a COLD model load (measured
# ~7s for nimble on the reference machine, held warm by keep_alive)
# still completes instead of tripping into fail-open on the first turn.
MEMORY_INJECT_DECISION_TIMEOUT_SECONDS = 15.0
_MEMORY_INJECT_DECISION_KEEP_ALIVE = "30m"
_MEMORY_INJECT_DECISION_USER_TEXT_BUDGET = 4000
_MEMORY_INJECT_DECISION_HISTORY_BUDGET = 4000
_MEMORY_INJECT_DECISION_HISTORY_MESSAGES = 8
_MEMORY_INJECT_DECISION_PROJECT_BUDGET = 300
_MEMORY_INJECT_DECISION_MEMORY_BUDGET = 600
_MEMORY_INJECT_DECISION_CONTENT_BUDGET = 500


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

    def metadata(self) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "threshold": self.threshold,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "question_count": len(self.scores),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
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
    if tags:
        parts.append(f"Tags: {tags}")
    memory_type = str(memory.get("type") or "").strip()
    if memory_type:
        parts.append(f"Type: {memory_type}")
    content = _truncate(memory.get("content"), _MEMORY_INJECT_DECISION_CONTENT_BUDGET)
    parts.append(f"Content: {content}")
    return " | ".join(parts)


_QUESTION_TEMPLATE = (
    "The memory shown in field {field} contains information that is directly "
    "relevant to the user's message and the surrounding conversation shown in "
    "the other fields — information worth placing in the model's context for "
    "this turn — not merely topically adjacent to it."
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


async def run_injection_gate(
    model_id: str,
    state: dict[str, str],
    gated: list[tuple[str, dict[str, Any]]],
    *,
    threshold: float,
    timeout: float = MEMORY_INJECT_DECISION_TIMEOUT_SECONDS,
) -> InjectionGateOutcome:
    """Run the screen end-to-end; does NOT swallow exceptions (caller wraps)."""
    request = DecisionRequest(
        model_id=model_id,
        state=state,
        questions=build_gate_questions(gated),
        keep_alive=_MEMORY_INJECT_DECISION_KEEP_ALIVE,
    )
    response = await _decide(request, timeout=timeout)
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
        )
    return InjectionGateOutcome(
        verdict="kept" if len(kept_fields) == len(gated) else "filtered",
        model_id=str(response.model or model_id),
        threshold=threshold,
        scores=scores,
        kept_fields=frozenset(kept_fields),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
