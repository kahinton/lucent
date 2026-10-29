"""MCP tool for asking the person clarifying questions mid-turn.

Every mainstream assistant exposes this as a first-class affordance rather than
asking the model to end its turn and hope the person replies: OpenAI has
structured output/clarifying prompts, Anthropic's Claude Code has
``AskUserQuestion`` (which accepts up to four batched questions, each with a
recommended option), and Copilot's SDK has an ``ask_user`` tool with an
``on_user_input_request`` handler. Lucent's chat had no equivalent — the model
could only write a question as prose and stop, which loses the pending tool loop
and the structured answer.

``ask_user`` is a real tool so both native engines (Copilot, Claude Code) and
Lucent's own LangChain loop get it for free from the MCP tool surface.

Design points that matter for answer quality:

* **Batched questions.** One call may ask up to
  :data:`~lucent.llm.tool_approval.MAX_QUESTIONS_PER_REQUEST` questions. Two
  questions asked in sequence cost two model round-trips and two human waits;
  asked together they cost one of each.
* **A recommended option.** The model may mark at most one option per question
  as recommended, which the UI highlights. People should be able to accept the
  model's judgment with one click instead of re-deriving it.
* **Free-text always available.** Suggested options are a shortcut, never a
  cage: the person can always type what the model did not think of.
"""

from __future__ import annotations

import json

from mcp.server import MCPServer as FastMCP
from pydantic import BaseModel, Field

from lucent.llm.context import get_llm_context
from lucent.llm.tool_approval import (
    MAX_CHOICES_PER_QUESTION,
    MAX_QUESTIONS_PER_REQUEST,
    interaction_broker,
    normalize_question,
)
from lucent.logging import get_logger
from lucent.tools.annotations import READ_ONLY

logger = get_logger("tools.interaction")

#: How long a question waits for an answer before the tool reports "unanswered".
_DEFAULT_QUESTION_TIMEOUT_SECONDS = 240


class AnswerChoice(BaseModel):
    """One suggested answer the person can click."""

    label: str = Field(description="Short answer text shown on the button.")
    description: str = Field(
        default="",
        description="Optional one-line explanation of the tradeoff this answer implies.",
    )
    recommended: bool = Field(
        default=False,
        description="True when this is the answer you believe is best. At most one option "
        "per question may be recommended.",
    )


class ClarifyingQuestion(BaseModel):
    """One question to ask the person."""

    question: str = Field(description="The question itself. One sentence is usually enough.")
    header: str = Field(
        default="",
        description="Short label (2-4 words) shown above the question, e.g. 'Launch date'.",
    )
    choices: list[AnswerChoice | str] = Field(
        default_factory=list,
        description="Two to four plausible answers. A plain string is shorthand for "
        "{label: <string>}. Leave empty when the answer must be free text.",
    )
    allow_free_form: bool = Field(
        default=True,
        description="Whether the person may type an answer instead of choosing. Keep this "
        "true unless free text genuinely makes no sense.",
    )


def _question_timeout_seconds() -> int:
    try:
        from lucent.settings import chat_ask_user_timeout_seconds

        return chat_ask_user_timeout_seconds()
    except Exception:  # noqa: BLE001 — a settings read must never break the tool
        return _DEFAULT_QUESTION_TIMEOUT_SECONDS


def _coerce_questions(
    questions: list[ClarifyingQuestion] | None,
    *,
    question: str,
    header: str,
    choices: list[AnswerChoice | str] | None,
    allow_freeform: bool,
) -> list[dict] | None:
    """Build the question list from either call shape.

    ``questions`` is the preferred batched form. The top-level ``question`` /
    ``header`` / ``choices`` / ``allow_freeform`` arguments are accepted so a
    single question stays one short call (and so an older prompt still works).
    Returns ``None`` for an invalid batch so the caller reports an error instead
    of silently asking nothing.
    """
    raw: list[dict] = []
    if questions:
        raw = [entry.model_dump() for entry in questions]
    elif str(question or "").strip():
        raw = [{
            "question": question,
            "header": header,
            "choices": [
                choice.model_dump() if isinstance(choice, BaseModel) else choice
                for choice in (choices or [])
            ],
            "allow_free_form": allow_freeform,
        }]
    if not raw or len(raw) > MAX_QUESTIONS_PER_REQUEST:
        return None
    normalized = [normalize_question(entry) for entry in raw]
    normalized = [entry for entry in normalized if entry is not None]
    if not normalized:
        return None
    for entry in normalized:
        # At most one recommendation per question: more than one is not a
        # recommendation, it is indecision presented as advice.
        seen_recommended = False
        for choice in entry["choices"]:
            if choice["recommended"] and seen_recommended:
                choice["recommended"] = False
            elif choice["recommended"]:
                seen_recommended = True
    return normalized


def register_interaction_tools(mcp: FastMCP) -> None:
    """Register the conversational interaction tools."""

    @mcp.tool(
        annotations=READ_ONLY,
        description="""Ask the user one or more clarifying questions and wait for the answers.

Use this instead of guessing when the request is genuinely ambiguous and the
answer changes what you do: an unclear target, a missing constraint, two
reasonable interpretations, or an action the user should confirm.

Prefer ONE call with every question you need, rather than several calls in a
row. Up to 5 questions may be asked at once, and they all appear on the same
inline card, so batching saves the user a wait and saves you a round-trip. Only
ask again when an answer genuinely raises a question you could not have
anticipated.

For each question:
- `choices` should list the plausible answers you would otherwise have to guess
  between. Two to four is usually right; never invent filler options.
- Mark at most ONE choice `recommended: true` when you think it is clearly the
  best answer. The user sees it highlighted and can accept it in one click, so
  recommend whenever you actually have a view — that is the fastest path to a
  good outcome. Omit it when the options are genuinely equal.
- `description` on a choice can explain the tradeoff in a few words.
- The user can always type a free-text answer instead, so options never have to
  be exhaustive.

The card appears inline and blocks this turn until the user answers or the wait
expires. When nobody is present (daemon or background work) this returns
answered=false; in that case use `send_handoff` with `requires_response=true`
instead of stopping to wait.

Args:
    questions: Questions to ask, at most 5. Each entry is
        {"question": str, "header": str, "choices": [...], "allow_free_form": bool}
        where a choice is either a string or
        {"label": str, "description": str, "recommended": bool}.
    question: Shorthand for a single question when you only need one.
    header: Short label (2-4 words) for the shorthand question.
    choices: Suggested answers for the shorthand question.
    allow_freeform: Whether the user may type freely for the shorthand question.

Returns: JSON with answered and `answers`, a list parallel to the questions
asked. Each entry carries header, question, answered, answer, choice, and
recommended (the option you suggested, so you can see whether they took it)."""
    )
    async def ask_user(
        questions: list[ClarifyingQuestion] | None = None,
        question: str = "",
        header: str = "",
        choices: list[AnswerChoice | str] | None = None,
        allow_freeform: bool = True,
    ) -> str:
        payload = _coerce_questions(
            questions,
            question=question,
            header=header,
            choices=choices,
            allow_freeform=allow_freeform,
        )
        if payload is None:
            return json.dumps({
                "error": (
                    f"Provide 1-{MAX_QUESTIONS_PER_REQUEST} questions with non-empty "
                    "text, either as `questions` or as the `question` argument."
                )
            })
        context = get_llm_context()
        result = await interaction_broker.ask_questions(
            session_id=context.get("session_id"),
            questions=payload,
            timeout_seconds=_question_timeout_seconds(),
        )
        return json.dumps(result, default=str)


__all__ = ["register_interaction_tools"]
