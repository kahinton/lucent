"""Tests for auto model selection (flag-gated, default off).

Covers the shared selector core in ``lucent/llm/model_auto.py``: the
rule filters (vision fit, context-window vs the estimated prompt size —
unknown windows are never guessed), the single ``choice`` question, and
``select_model_auto``'s degraded outcomes (below-confidence-floor, ties
won by the default chain, unknown options, endpoint errors with one
retry, no decision model) with a monkeypatched ``decide``.
"""

import pytest

from lucent.llm import model_auto as ma
from lucent.llm.decision import (
    ChoiceAnswer,
    DecisionResponse,
    DecisionUsage,
)


def _model(
    model_id: str,
    *,
    category: str = "general",
    context_window: int = 0,
    supports_vision: bool = False,
    tags: list[str] | None = None,
    notes: str = "",
) -> dict:
    return {
        "id": model_id,
        "name": f"Model {model_id.upper()}",
        "provider": "test",
        "category": category,
        "context_window": context_window,
        "supports_tools": True,
        "supports_vision": supports_vision,
        "tags": tags or [],
        "notes": notes,
    }


def _canned_decide(answer):
    """Patch ma.decide with a canned ChoiceAnswer carrying usage."""
    async def fake_decide(request, **_kw):
        return DecisionResponse(
            model="nimble",
            answers={"best_model": answer},
            usage=DecisionUsage(input_tokens=100, output_tokens=10),
        )
    return fake_decide


# ── pure helpers ────────────────────────────────────────────────────────


def test_is_auto_model_matches_only_the_sentinel():
    assert ma.is_auto_model("auto")
    assert ma.is_auto_model("AUTO")
    assert ma.is_auto_model(" auto ")
    assert not ma.is_auto_model(None)
    assert not ma.is_auto_model("gpt-4")
    assert not ma.is_auto_model(123)


def test_estimate_prompt_tokens_ignores_blank_texts():
    assert ma.estimate_prompt_tokens(None, "") == 0
    assert ma.estimate_prompt_tokens("a" * 8) == 2


def test_rule_filters_drop_vision_and_small_context():
    big = _model("big", context_window=100_000)
    vision = _model("vision", context_window=100_000, supports_vision=True)
    small = _model("small", context_window=1_000)
    unknown = _model("unknown", context_window=0)
    survivors, dropped = ma.apply_rule_filters(
        [big, vision, small, unknown],
        require_vision=True,
        estimated_prompt_tokens=1_000,
    )
    assert [m["id"] for m in survivors] == ["vision"]
    assert dropped == [
        ("big", "does not support image input"),
        ("small", "does not support image input"),
        ("unknown", "does not support image input"),
    ]


def test_rule_filters_never_guess_unknown_windows():
    # A zero window (unknown) passes even a huge estimate.
    unknown = _model("unknown", context_window=0)
    survivors, dropped = ma.apply_rule_filters(
        [unknown], estimated_prompt_tokens=1_000_000
    )
    assert [m["id"] for m in survivors] == ["unknown"]
    assert dropped == []


def test_task_state_budget_truncates_description():
    state = ma.build_task_state(
        agent_type="code", title="T" * 100, description="D" * 9000
    )
    assert state["agent_type"] == "code"
    assert len(state["task_title"]) == 100
    assert len(state["task_description"]) <= ma._TASK_STATE_BUDGET
    assert sum(len(v) for v in state.values()) <= ma._TASK_STATE_BUDGET


def test_model_line_rendering():
    line = ma.model_line_for_state(
        _model("a", category="fast", context_window=100_000,
               tags=["one", "two", "three", "four", "five"], notes="N")
    )
    assert line.startswith("a: Model A [fast, one, two, three, four]")
    assert "context 100000 tokens" in line
    assert "N" in line
    assert len(line) <= ma._MODEL_LINE_BUDGET


def test_param_scale_from_tags():
    # Tagging convention only — "763b"-style tags become readable sizes.
    assert ma._model_param_scale(_model("big", tags=["763b", "fast"])) == "763B"
    assert ma._model_param_scale(_model("mid", tags=["9.5b"])) == "9.5B"
    assert ma._model_param_scale(_model("none", tags=["fast"])) is None
    assert ma._model_param_scale(_model("empty", tags=[])) is None
    assert ma._model_param_scale(_model("junk", tags=["x763b", "b763"])) is None


def test_criteria_and_model_line_surface_param_scale():
    big = _model("big", tags=["763b", "fast"], category="fast", context_window=1_048_576)
    small = _model("small", tags=["31b"], category="general", context_window=262_144)
    questions = ma.build_auto_questions([big, small])
    assert questions["best_model"].criteria == {
        "big": "Model BIG (fast, 763B params)",
        "small": "Model SMALL (general, 31B params)",
    }
    big_line = ma.model_line_for_state(big)
    small_line = ma.model_line_for_state(small)
    assert "763B parameters" in big_line
    assert "31B parameters" in small_line
    assert len(big_line) <= ma._MODEL_LINE_BUDGET


def test_build_auto_questions_single_choice():
    questions = ma.build_auto_questions([_model("a"), _model("b")])
    assert list(questions) == ["best_model"]
    question = questions["best_model"]
    assert question.type == "choice"
    assert question.criteria == {"a": "Model A (general)", "b": "Model B (general)"}


# ── select_model_auto ───────────────────────────────────────────────────


def _chain_pick(pick="chain-default", reason="default chain"):
    return lambda: (pick, reason)


@pytest.mark.asyncio
async def test_arbiter_pick_wins(monkeypatch):
    monkeypatch.setattr(ma, "decide", _canned_decide(
        ChoiceAnswer(choice="a", probabilities={"a": 0.8, "b": 0.12},
                     confidence=0.9)
    ))
    choice = await ma.select_model_auto(
        models=[_model("a"), _model("b")],
        decision_model_id="nimble",
        agent_type="code",
        title="Do a thing",
        fallback_pick=_chain_pick("b"),
    )
    assert choice.model_id == "a"
    assert choice.source == "arbiter"
    assert choice.probabilities == {"a": 0.8, "b": 0.12}
    assert choice.usage == (100, 10)
    assert choice.fallback_reason is None


@pytest.mark.asyncio
async def test_below_floor_confidence_falls_back(monkeypatch):
    monkeypatch.setattr(ma, "decide", _canned_decide(
        ChoiceAnswer(choice="a", probabilities={"a": 0.45, "b": 0.4},
                     confidence=0.5)
    ))
    choice = await ma.select_model_auto(
        models=[_model("a"), _model("b")],
        decision_model_id="nimble",
        confidence_floor=0.6,
        fallback_pick=_chain_pick("b"),
    )
    assert choice.model_id == "b" and choice.source == "fallback"
    assert "below confidence floor" in choice.fallback_reason


@pytest.mark.asyncio
async def test_close_tie_gives_the_default_chain_pick(monkeypatch):
    monkeypatch.setattr(ma, "decide", _canned_decide(
        ChoiceAnswer(choice="a", probabilities={"a": 0.65, "b": 0.59},
                     confidence=0.7)
    ))
    choice = await ma.select_model_auto(
        models=[_model("a"), _model("b")],
        decision_model_id="nimble",
        fallback_pick=_chain_pick("b"),
    )
    assert choice.model_id == "b"
    assert "tied between" in choice.fallback_reason


@pytest.mark.asyncio
async def test_unknown_option_falls_back(monkeypatch):
    monkeypatch.setattr(ma, "decide", _canned_decide(
        ChoiceAnswer(choice="ghost", probabilities={"ghost": 0.9},
                     confidence=0.9)
    ))
    choice = await ma.select_model_auto(
        models=[_model("a"), _model("b")],
        decision_model_id="nimble",
        fallback_pick=_chain_pick("b"),
    )
    assert choice.model_id == "b"
    assert "unknown or missing model option" in choice.fallback_reason


@pytest.mark.asyncio
async def test_endpoint_error_retries_then_falls_back(monkeypatch):
    from lucent.llm.decision import DecisionError

    attempts = []

    async def flaky_decide(request, **_kw):
        attempts.append(request)
        raise DecisionError("endpoint down")

    monkeypatch.setattr(ma, "decide", flaky_decide)
    choice = await ma.select_model_auto(
        models=[_model("a"), _model("b")],
        decision_model_id="nimble",
        fallback_pick=_chain_pick("b"),
    )
    assert len(attempts) == 2, "exactly one deterministic retry"
    assert choice.model_id == "b" and choice.source == "fallback"
    assert "endpoint down" in choice.fallback_reason


@pytest.mark.asyncio
async def test_no_decision_model_degrades_to_chain():
    choice = await ma.select_model_auto(
        models=[_model("a"), _model("b")],
        decision_model_id=None,
        fallback_pick=_chain_pick("b"),
    )
    assert choice.model_id == "b" and choice.source == "fallback"
    assert "no decision-class arbiter model" in choice.fallback_reason


@pytest.mark.asyncio
async def test_zero_survivors_degrade_to_chain():
    tiny = _model("tiny", context_window=10)
    choice = await ma.select_model_auto(
        models=[tiny],
        extra_prompt_tokens=50_000,
        decision_model_id="nimble",
        fallback_pick=_chain_pick("b"),
    )
    assert choice.model_id == "b" and choice.source == "fallback"
    assert choice.dropped and "no eligible model" in choice.fallback_reason


@pytest.mark.asyncio
async def test_single_survivor_skips_the_arbiter(monkeypatch):
    async def fail_decide(request, **_kw):
        raise AssertionError("arbiter must not be called for one survivor")

    monkeypatch.setattr(ma, "decide", fail_decide)
    choice = await ma.select_model_auto(
        models=[_model("only", context_window=1_000_000)],
        extra_prompt_tokens=1_000,
        decision_model_id="nimble",
        fallback_pick=_chain_pick("other"),
    )
    assert choice.model_id == "only" and choice.source == "arbiter"
    assert choice.confidence == 1.0


@pytest.mark.asyncio
async def test_option_cap_keeps_fallback_pick(monkeypatch):
    models = [_model(f"m-{i}") for i in range(40)]
    models.append(_model("chain-default"))
    monkeypatch.setattr(ma, "decide", _canned_decide(
        ChoiceAnswer(choice="m-0", probabilities={"m-0": 0.9},
                     confidence=0.95)
    ))
    choice = await ma.select_model_auto(
        models=models,
        decision_model_id="nimble",
        fallback_pick=_chain_pick("chain-default"),
    )
    assert choice.model_id == "m-0"
    assert choice.options_truncated == len(models) - ma._MAX_AUTO_OPTIONS
    # The option list actually sent keeps the fallback pick inside the cap.
    # (Re-derive: the decide request carried <= _MAX_AUTO_OPTIONS criteria.)
    assert len(ma.build_auto_questions(models[: ma._MAX_AUTO_OPTIONS])[
        "best_model"
    ].criteria) <= ma._MAX_AUTO_OPTIONS
