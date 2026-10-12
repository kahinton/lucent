"""Client for System One decision models (Jev-style, via Ollama 0.35+).

Decision models answer typed questions about a state instead of generating
chat text: ``choice`` (pick an option, return per-option probabilities),
``noul`` (probability that a condition is true), and ``score`` (probability-
weighted position on an ordered rubric). Ollama exposes this locally through
``POST {OLLAMA_HOST}/v1/systemone``; a single request answers every named
question at once, which keeps per-decision latency in the tens of
milliseconds.

This module is the only supported path for using decision models — they are
classified ``model_class="decision"`` in the registry and are explicitly
excluded from the chat engines. Callers build a :class:`DecisionRequest`,
send it with :func:`decide`, and receive typed :class:`DecisionAnswer`
objects.

Limits enforced client-side mirror the server so callers fail fast with
actionable messages: 1-64 questions per request, 2-255 choice options,
2-26 score levels, and a 64 KiB request body.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Union

import httpx

# Server-side limits documented by Ollama 0.35 for /v1/systemone.
MAX_QUESTIONS = 64
MIN_CHOICE_OPTIONS = 2
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 26
MAX_BODY_BYTES = 64 * 1024

DEFAULT_TIMEOUT_SECONDS = 30.0

QUESTION_TYPES = {"choice", "noul", "score"}

QuestionType = Literal["choice", "noul", "score"]

logger = logging.getLogger(__name__)


class DecisionError(RuntimeError):
    """Raised when a decision request cannot be built, sent, or parsed."""


@dataclass(slots=True)
class DecisionQuestion:
    """One named question about the request state.

    ``type`` selects the answer semantics:

    - ``choice``: pick one option from ``criteria`` (option key → short
      description of what the option means).
    - ``noul``: return the probability that ``instructions`` is true.
    - ``score``: place the state on the ordered rubric ``criteria``
      (level labels, low to high); the answer is a probability-weighted
      average of level *indices*, not a percentage.
    """

    name: str
    type: QuestionType
    instructions: str
    criteria: dict[str, str] | list[str] | None = None


@dataclass(slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(slots=True)
class NoulAnswer:
    noul: float


@dataclass(slots=True)
class ScoreAnswer:
    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


DecisionAnswer = Union[ChoiceAnswer, NoulAnswer, ScoreAnswer]


@dataclass(frozen=True, slots=True)
class DecisionUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    model: str
    answers: dict[str, DecisionAnswer]
    usage: DecisionUsage


@dataclass(slots=True)
class DecisionRequest:
    """A state plus the named questions to ask about it.

    ``state`` is the text to judge — a plain string or a JSON object of named
    text fields. ``questions`` is an ordered mapping of question name to
    :class:`DecisionQuestion`.
    """

    model_id: str
    state: str | dict[str, str]
    questions: dict[str, DecisionQuestion] = field(default_factory=dict)
    keep_alive: str | None = None


def _validate_request(request: DecisionRequest) -> None:
    """Validate a request against Ollama's documented limits.

    Fails fast client-side so the caller sees a targeted message instead of a
    bare 400 from the server.
    """
    if not request.model_id or not request.model_id.strip():
        raise DecisionError("Decision request requires a model_id.")
    if not request.questions:
        raise DecisionError("Decision request requires at least one question.")
    if len(request.questions) > MAX_QUESTIONS:
        raise DecisionError(
            f"Too many questions: {len(request.questions)} "
            f"(server allows at most {MAX_QUESTIONS})."
        )
    for name, question in request.questions.items():
        if question.type not in QUESTION_TYPES:
            raise DecisionError(
                f"Question '{name}' has unknown type {question.type!r}. "
                f"Expected one of: {', '.join(sorted(QUESTION_TYPES))}."
            )
        if not question.instructions or not question.instructions.strip():
            raise DecisionError(f"Question '{name}' requires instructions.")
        if question.type == "choice":
            options = question.criteria or {}
            if not isinstance(options, dict):
                raise DecisionError(
                    f"Choice question '{name}' criteria must be an "
                    "option key → description mapping."
                )
            if not MIN_CHOICE_OPTIONS <= len(options) <= MAX_CHOICE_OPTIONS:
                raise DecisionError(
                    f"Choice question '{name}' needs between {MIN_CHOICE_OPTIONS} "
                    f"and {MAX_CHOICE_OPTIONS} options; got {len(options)}."
                )
        elif question.type == "score":
            levels = question.criteria or []
            if not isinstance(levels, list):
                raise DecisionError(
                    f"Score question '{name}' criteria must be an ordered "
                    "list of level labels."
                )
            if not MIN_SCORE_LEVELS <= len(levels) <= MAX_SCORE_LEVELS:
                raise DecisionError(
                    f"Score question '{name}' needs between {MIN_SCORE_LEVELS} "
                    f"and {MAX_SCORE_LEVELS} levels; got {len(levels)}."
                )
        if len(name) > 128:
            raise DecisionError(
                f"Question name '{name}' is too long (max 128 characters)."
            )

    body = _request_payload(request, validate_body=False)
    size = len(json.dumps(body).encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise DecisionError(
            f"Decision request body is {size} bytes "
            f"(server allows at most {MAX_BODY_BYTES}). Trim the state or questions."
        )


def _question_payload(question: DecisionQuestion) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": question.type, "instructions": question.instructions}
    if question.criteria is not None:
        payload["criteria"] = question.criteria
    return payload


def _request_payload(request: DecisionRequest, *, validate_body: bool = True) -> dict[str, Any]:
    if validate_body:
        _validate_request(request)
    payload: dict[str, Any] = {
        "model": request.model_id,
        "state": request.state,
        "questions": {
            name: _question_payload(question) for name, question in request.questions.items()
        },
    }
    if request.keep_alive is not None:
        payload["keep_alive"] = request.keep_alive
    return payload


def _parse_answer(name: str, answer: Any) -> DecisionAnswer:
    """Parse one entry of the server's ``answers`` map into a typed answer."""
    if not isinstance(answer, dict):
        raise DecisionError(f"Malformed answer for question '{name}'.")
    answer_type = str(answer.get("type") or "")
    try:
        if answer_type == "choice":
            probabilities = answer.get("probabilities") or {}
            return ChoiceAnswer(
                choice=str(answer.get("choice") or ""),
                probabilities={
                    str(key): float(value) for key, value in probabilities.items()
                },
                confidence=float(answer.get("confidence") or 0.0),
            )
        if answer_type == "noul":
            return NoulAnswer(noul=float(answer.get("noul") or 0.0))
        if answer_type == "score":
            legend = answer.get("legend") or {}
            probabilities = answer.get("probabilities") or {}

            def _level_order(kv):
                key = kv[0]
                return (0, int(key), "") if str(key).isdigit() else (1, 0, str(key))

            return ScoreAnswer(
                score=float(answer.get("score") or 0.0),
                legend={
                    str(key): str(value)
                    for key, value in sorted(legend.items(), key=_level_order)
                },
                probabilities={
                    str(key): float(value) for key, value in probabilities.items()
                },
                confidence=float(answer.get("confidence") or 0.0),
            )
        raise DecisionError(
            f"Unknown answer type {answer_type!r} for question '{name}'."
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise DecisionError(f"Malformed answer for question '{name}': {exc}") from exc


def decision_base_url(base_url: str | None = None) -> str:
    """Resolve the Ollama API base for decision calls.

    ``OLLAMA_HOST`` may carry a trailing ``/api`` (the discovery convention);
    the System One endpoint hangs off the bare server root, so that suffix is
    stripped.
    """
    resolved = (base_url or os.environ.get("OLLAMA_HOST") or "").strip().rstrip("/")
    if resolved.rstrip("/").endswith("/api"):
        resolved = resolved[: -len("/api")]
    if not resolved:
        raise DecisionError(
            "No Ollama host configured. Set OLLAMA_HOST or pass base_url."
        )
    return resolved


async def decide(
    request: DecisionRequest,
    *,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> DecisionResponse:
    """Send a decision request and return typed answers.

    One HTTP round trip per :class:`DecisionRequest` — the server answers
    every named question in the same call.
    """
    _validate_request(request)
    url = f"{decision_base_url(base_url)}/v1/systemone"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"http://{url}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=_request_payload(request))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        raise DecisionError(
            f"Decision endpoint returned {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        # str(exc) is empty for several httpx transport errors (bare
        # ReadTimeout/ConnectError) — fall back to the exception class name
        # so a skipped gate never surfaces as "failed: " with no cause.
        raise DecisionError(
            f"Decision endpoint call failed: {str(exc).strip() or type(exc).__name__}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DecisionError(f"Decision endpoint returned invalid JSON: {exc}") from exc

    return _parse_response(data)


def _parse_response(data: Any) -> DecisionResponse:
    if not isinstance(data, dict):
        raise DecisionError("Decision endpoint returned a non-object response.")
    raw_answers = data.get("answers")
    if not isinstance(raw_answers, dict):
        raise DecisionError("Decision response is missing an 'answers' object.")
    answers = {
        name: _parse_answer(name, answer) for name, answer in raw_answers.items()
    }
    usage_row = data.get("usage") or {}
    return DecisionResponse(
        model=str(data.get("model") or ""),
        answers=answers,
        usage=DecisionUsage(
            input_tokens=int(usage_row.get("input_tokens") or 0),
            output_tokens=int(usage_row.get("output_tokens") or 0),
        ),
    )


async def resolve_decision_model_id(
    *,
    explicit_model_id: str | None = None,
    organization_id: str | None = None,
    prefer_db: bool = False,
) -> str | None:
    """Resolve the decision-class model id to use, or None.

    Shared by the decision-model gates (request pre-review, memory
    injection) since both run in processes where the DB model registry may
    or may not be loaded:

    - An explicit ``explicit_model_id`` wins. An id the in-memory registry
      knows but classifies non-decision is rejected; an unknown id is
      trusted (admin-configured, and the daemon never loads the DB
      registry).
    - Otherwise the first enabled decision-class model is taken:
      DB-first when ``prefer_db`` (background gates — DB ordering by
      provider/name gives a stable pick), in-memory registry first for the
      hot message path (both processes load DB models at startup, so the
      lookup usually never touches the DB).

    All lookup failures degrade to ``None`` with a logged warning —
    callers treat None as "gate skipped".
    """
    org = organization_id or None

    if explicit_model_id:
        try:
            from lucent.model_registry import get_model, is_decision_model

            if get_model(explicit_model_id) is not None and not is_decision_model(
                explicit_model_id
            ):
                logger.warning(
                    "Decision model '%s' is not a decision-class model;", explicit_model_id
                )
                return None
        except Exception as exc:  # registry lookups must never break the gate
            logger.warning("Decision model registry check failed: %s", exc)
        return explicit_model_id

    def _first_registry_model() -> str | None:
        try:
            from lucent.model_registry import list_decision_models

            candidates = list_decision_models()
            if candidates:
                return str(candidates[0].id)
        except Exception as exc:
            logger.warning("In-memory decision model lookup failed: %s", exc)
        return None

    async def _first_db_model() -> str | None:
        try:
            from lucent.db.models import ModelRepository
            from lucent.db.pool import scoped_acquire

            async with scoped_acquire(organization_id=org, role="system") as conn:
                result = await ModelRepository(conn).list_models(
                    model_class="decision",
                    enabled_only=True,
                    org_id=org,
                    limit=1,
                )
            items = (result or {}).get("items") or []
            if items:
                return str(items[0]["id"])
        except Exception as exc:
            logger.warning("Decision model DB lookup failed: %s", exc)
        return None

    if prefer_db:
        return await _first_db_model() or _first_registry_model()
    return _first_registry_model() or await _first_db_model()
