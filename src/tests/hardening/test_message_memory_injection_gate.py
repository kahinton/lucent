"""Decision-model relevance gate for the message memory injection hook.

Covers the ``memory.inject_decision_gate_*`` settings flag: after the
existing similarity-floor / score-drop / dedup filters, surviving candidates
plus the conversation state go to a decision-class model (one ``noul``
question per candidate) and only memories scoring at or above the threshold
survive. Flag-off stays byte-for-byte; any gate failure is fail-open.

Also covers the shared decision-model resolver
(``lucent.llm.decision.resolve_decision_model_id``) that this gate and the
request pre-review gate both use.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

import lucent.llm.memory_injection_gate as gate_module
from lucent.llm.decision import (
    DecisionError,
    DecisionResponse,
    DecisionUsage,
    NoulAnswer,
    resolve_decision_model_id,
)
from lucent.llm.hooks import HookManager
from lucent.model_registry import ModelInfo


class FakeBridge:
    """Same shape as the injection-suite FakeBridge (tool-aware replay)."""

    def __init__(
        self,
        results: list[dict[str, Any]],
        full: dict[str, str] | None = None,
    ):
        self.results = results
        self.full = full or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, payload: dict[str, Any]) -> str:
        self.calls.append((name, payload))
        if name == "get_memories":
            memories = [
                {"id": memory_id, "content": content}
                for memory_id, content in self.full.items()
                if not payload.get("memory_ids") or memory_id in payload["memory_ids"]
            ]
            return json.dumps({"memories": memories})
        return json.dumps({"memories": self.results})


def _memory(mid: str, score: float, content: str = "x") -> dict[str, Any]:
    return {"id": mid, "content": content, "tags": [], "similarity_score": score}


class _RecordingDecide:
    """Replaces ``memory_injection_gate._decide``; returns per-field scores."""

    def __init__(self, scores: dict[str, float], error: Exception | None = None):
        self.scores = scores
        self.error = error
        self.requests: list[Any] = []

    async def __call__(self, request, *, base_url=None, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        answers = {
            name: NoulAnswer(noul=self.scores.get(name, 0.0))
            for name in request.questions
        }
        return DecisionResponse(
            model=request.model_id,
            answers=answers,
            usage=DecisionUsage(input_tokens=100, output_tokens=10),
        )


def _patch_gate(monkeypatch, *, threshold: float = 0.7) -> None:
    monkeypatch.setattr("lucent.llm.hooks._message_inject_decision_gate_enabled", lambda: True)
    monkeypatch.setattr(
        "lucent.llm.hooks._message_inject_decision_gate_threshold", lambda: threshold
    )


def _patch_model_id(monkeypatch, model_id: str | None) -> None:
    async def _resolver(*args, **kwargs):
        return model_id

    monkeypatch.setattr("lucent.llm.hooks._message_inject_decision_model_id", _resolver)


def _messages(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


# ── flag off: byte-for-byte ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_off_never_calls_decide_and_keeps_current_behaviour(monkeypatch):
    poisoned = _RecordingDecide({"memory_1": 0.99})
    poisoned.error = DecisionError("the gate must never be consulted")
    monkeypatch.setattr(
        gate_module, "_decide", poisoned
    )
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "hit memory")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    # Flag off: no decide call, plain injection, no decision_* metadata.
    assert outcome.injectable_executions[0].text == (
        "Relevant accessible memories for the user's message:\n\n- 11111111: hit memory"
    )
    assert poisoned.requests == []
    metadata = outcome.injectable_executions[0].metadata
    assert not [key for key in metadata if key.startswith("decision_")]


@pytest.mark.asyncio
async def test_gate_flag_on_without_model_disables_gate(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, None)
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "hit memory")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    assert "hit memory" in outcome.injectable_executions[0].text
    metadata = outcome.injectable_executions[0].metadata
    assert not [key for key in metadata if key.startswith("decision_")]


# ── keeps / drops ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_drops_low_relevance_memory(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    recorder = _RecordingDecide({"memory_1": 0.9, "memory_2": 0.1})
    monkeypatch.setattr(gate_module, "_decide", recorder)

    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.6, "keepsake"),
        _memory("22222222-2222-2222-2222-222222222222", 0.55, "noiseword"),
    ])
    state: dict[str, Any] = {}
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    assert "keepsake" in execution.text
    assert "noiseword" not in execution.text
    assert execution.metadata["decision_gate"] is True
    assert execution.metadata["decision_model"] == "nimble"
    assert execution.metadata["decision_gated"] == 1
    assert execution.metadata["decision_scores"]["11111111"] == 0.9
    assert execution.metadata["decision_scores"]["22222222"] == 0.1
    # Gated-rejected memory joins neither the dedup set nor the model prompt.
    assert state["injected_memory_ids"] == {"11111111-1111-1111-1111-111111111111"}
    # One decide call; the question names are generated, not memory ids.
    request = recorder.requests[0]
    assert list(request.questions) == ["memory_1", "memory_2"]
    assert all(q.type == "noul" for q in request.questions.values())
    assert "noiseword" in request.state["memory_2"]


@pytest.mark.asyncio
async def test_gate_threshold_boundary_is_inclusive(monkeypatch):
    _patch_gate(monkeypatch, threshold=0.7)
    _patch_model_id(monkeypatch, "nimble")
    monkeypatch.setattr(gate_module, "_decide", _RecordingDecide({"memory_1": 0.7}))
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "keepsake")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    assert "keepsake" in outcome.injectable_executions[0].text


@pytest.mark.asyncio
async def test_gate_filtering_everything_emits_gated_silence_line(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    monkeypatch.setattr(
        gate_module, "_decide", _RecordingDecide({"memory_1": 0.1, "memory_2": 0.2})
    )
    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.6, "keepsake"),
        _memory("22222222-2222-2222-2222-222222222222", 0.55, "noiseword"),
    ])
    state: dict[str, Any] = {}
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    silence = outcome.executions[0]
    assert silence.text == (
        "memory-lookup: 0 injected (2 gated, 0 overflow, 0 deduped, 0 below threshold)"
    )
    assert silence.metadata["decision_gated"] == 2
    assert silence.metadata["skipped"] == 2
    # Nothing was injected → nothing joins the dedup set; a later turn with
    # fresh conversation context may re-gate the same candidates.
    assert state.get("injected_memory_ids") in (None, set())


@pytest.mark.asyncio
async def test_gate_keeps_everything_reports_no_gated_counter(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    monkeypatch.setattr(
        gate_module, "_decide", _RecordingDecide({"memory_1": 0.9, "memory_2": 0.8})
    )
    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.6, "keepsake"),
        _memory("22222222-2222-2222-2222-222222222222", 0.55, "noiseword"),
    ])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    metadata = outcome.injectable_executions[0].metadata
    assert metadata["decision_gated"] == 0
    assert metadata["memory_count"] == 2


# ── fail-open ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_error_fails_open_without_hook_error_chip(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    monkeypatch.setattr(
        gate_module, "_decide", _RecordingDecide({}, error=DecisionError("endpoint down"))
    )
    bridge = FakeBridge([
        _memory(f"aaaaaaaa-0000-0000-0000-{index:012d}", 0.6, f"memory {index}")
        for index in range(5)
    ])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    # No visible hook-error chip: the gate swallowed the failure.
    assert not any("hook error" in execution.text for execution in outcome.executions)
    execution = outcome.injectable_executions[0]
    # Cap still bounds injection (max_memories default 3) — fail-open is
    # pre-gate behaviour, not more injection.
    assert execution.metadata["memory_count"] == 3
    assert execution.metadata["decision_skipped"] == "endpoint down"
    assert execution.metadata["decision_gated"] == 0


@pytest.mark.asyncio
async def test_gate_unexpected_error_fails_open(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")

    async def _explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gate_module, "_decide", _explode)
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "keepsake")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    assert "keepsake" in outcome.injectable_executions[0].text
    metadata = outcome.injectable_executions[0].metadata
    assert "boom" in metadata["decision_skipped"]


# ── gate window cap (>64-question pool protection) ──────────────────────────


@pytest.mark.asyncio
async def test_gate_window_caps_questions_and_counts_overflow(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    recorder = _RecordingDecide({f"memory_{n}": 0.9 for n in range(1, 17)})
    monkeypatch.setattr(gate_module, "_decide", recorder)

    bridge = FakeBridge([
        _memory(f"aaaaaaaa-0000-0000-0000-{index:012d}", 0.6, f"memory {index}")
        for index in range(20)
    ])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert len(request.questions) == 16  # MEMORY_INJECT_DECISION_MAX_QUESTIONS
    assert list(request.questions) == [f"memory_{n}" for n in range(1, 17)]
    metadata = outcome.injectable_executions[0].metadata
    assert metadata["decision_overflow"] == 4
    assert metadata["memory_count"] == 3  # max_memories cap still binds
    # Overflow candidates were dropped by the gate window; cap displacement
    # is not a "skip", so only the overflow adds to the skipped total.
    assert metadata["skipped"] == 4


# ── daemon-style path (no session_state / no explicit user text) ────────────


@pytest.mark.asyncio
async def test_gate_builds_state_from_messages_without_session_state(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    recorder = _RecordingDecide({"memory_1": 0.9})
    monkeypatch.setattr(gate_module, "_decide", recorder)

    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "keepsake")])
    manager = HookManager()  # no session_state (daemon callers)
    # A prior assistant turn before the current user message: the round-1
    # guard keys off the trailing user message, and the gate's conversation
    # field is built from the remaining history.
    messages = [
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "injection threshold question"},
    ]
    outcome = await manager.before_model_call(messages=messages, memory_bridge=bridge)
    assert "keepsake" in outcome.injectable_executions[0].text
    request = recorder.requests[0]
    assert request.state["user_message"] == "injection threshold question"
    assert "assistant: earlier answer" in request.state["conversation"]
    assert "project" not in request.state


@pytest.mark.asyncio
async def test_gate_includes_project_context_field(monkeypatch):
    _patch_gate(monkeypatch)
    _patch_model_id(monkeypatch, "nimble")
    recorder = _RecordingDecide({"memory_1": 0.9, "memory_2": 0.9})
    monkeypatch.setattr(gate_module, "_decide", recorder)

    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.5, "boosted project memory"),
        _memory("22222222-2222-2222-2222-222222222222", 0.6, "plain memory"),
    ])
    session_state = {
        "_project_context": {
            "project": {"id": "p1", "name": "Aurora"},
            "memory_ids": ["11111111-1111-1111-1111-111111111111"],
            "files": [],
        },
    }
    manager = HookManager(session_state=session_state)
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    request = recorder.requests[0]
    assert request.state["project"] == "Chat project: Aurora"
    # The project boost (ranking-only) still applies before the gate, so the
    # boosted memory is gated first and survives: injection order preserved.
    execution = outcome.injectable_executions[0]
    assert execution.text.index("boosted project memory") < execution.text.index("plain memory")
    assert execution.metadata["decision_gate"] is True


# ── question naming and state budgets (unit level) ──────────────────────────


def test_build_gate_state_omits_empty_fields_and_caps_window():
    from lucent.llm.memory_injection_gate import (
        MEMORY_INJECT_DECISION_MAX_QUESTIONS,
        build_gate_state,
    )

    candidates = [
        (_memory(f"aaaaaaaa-0000-0000-0000-{index:012d}", 0.6, f"mem {index}"), 0.6)
        for index in range(MEMORY_INJECT_DECISION_MAX_QUESTIONS + 5)
    ]
    state, gated = build_gate_state(
        user_text="hello memory consolidation",
        messages=[{"role": "user", "content": "hello memory consolidation"}],
        session_state={},
        candidates=candidates,
    )
    assert len(gated) == MEMORY_INJECT_DECISION_MAX_QUESTIONS
    assert list(state) == [
        "user_message",
        *[f"memory_{n}" for n in range(1, MEMORY_INJECT_DECISION_MAX_QUESTIONS + 1)],
    ]
    assert state["memory_1"] == "Content: mem 0"


def test_build_gate_questions_share_one_template():
    from lucent.llm.memory_injection_gate import build_gate_questions, build_gate_state

    candidates = [(_memory("11111111-1111-1111-1111-111111111111", 0.6, "mem"), 0.6)]
    state, gated = build_gate_state(
        user_text="hello memory consolidation",
        messages=None,
        session_state=None,
        candidates=candidates,
    )
    questions = build_gate_questions(gated)
    assert set(questions) == {"memory_1"}
    assert questions["memory_1"].name in state
    assert questions["memory_1"].type == "noul"


def test_evaluate_gate_response_drops_missing_answer():
    from lucent.llm.decision import DecisionResponse, DecisionUsage
    from lucent.llm.memory_injection_gate import (
        build_gate_state,
        evaluate_gate_response,
    )

    _, gated = build_gate_state(
        user_text="hello memory consolidation",
        messages=None,
        session_state=None,
        candidates=[(_memory("11111111-1111-1111-1111-111111111111", 0.6), 0.6),
                    (_memory("22222222-2222-2222-2222-222222222222", 0.6), 0.6)],
    )
    response = DecisionResponse(
        model="nimble",
        answers={"memory_1": NoulAnswer(noul=0.8)},  # memory_2 missing
        usage=DecisionUsage(input_tokens=1, output_tokens=1),
    )
    scores, kept, complete = evaluate_gate_response(response, gated, threshold=0.7)
    assert set(scores) == {"memory_1", "memory_2"}
    assert kept == ["memory_1"]
    assert complete is True


def test_evaluate_gate_response_no_usable_answers_not_complete():
    from lucent.llm.decision import DecisionResponse, DecisionUsage
    from lucent.llm.memory_injection_gate import evaluate_gate_response

    response = DecisionResponse(
        model="nimble", answers={}, usage=DecisionUsage(input_tokens=1, output_tokens=1)
    )
    _scores, _kept, complete = evaluate_gate_response(
        response, [("memory_1", _memory("11111111-1111-1111-1111-111111111111", 0.6))],
        threshold=0.7,
    )
    assert complete is False


# ── shared resolver ──────────────────────────────────────────────────────────


async def test_resolver_prefers_registry_by_default():
    async def _no_db(*args, **kwargs):
        raise AssertionError("DB must not be touched while the registry has models")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            "lucent.model_registry.list_decision_models",
            lambda: [
                ModelInfo(
                    id="nimble",
                    provider="ollama",
                    name="Nimble",
                    category="decision",
                    model_class="decision",
                )
            ],
        )
        monkeypatch.setattr("lucent.db.pool.scoped_acquire", _no_db)
        model = await resolve_decision_model_id(prefer_db=False)
        assert model == "nimble"
    finally:
        monkeypatch.undo()


async def test_resolver_explicit_unknown_id_is_trusted():
    model = await resolve_decision_model_id(explicit_model_id="some-custom-model")
    assert model == "some-custom-model"


async def test_resolver_explicit_non_decision_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "lucent.model_registry.get_model",
        lambda model_id: ModelInfo(
            id="gemma3:4b", provider="ollama", name="Gemma", category="general"
        ),
    )
    assert await resolve_decision_model_id(explicit_model_id="gemma3:4b") is None


def _fake_db_model(monkeypatch, model_id: str | None):
    class _FakeRepo:
        def __init__(self, conn):
            pass

        async def list_models(self, **kwargs):
            assert kwargs["model_class"] == "decision"
            assert kwargs["enabled_only"] is True
            assert kwargs["limit"] == 1
            return {"items": ([{"id": model_id}] if model_id else [])}

    @asynccontextmanager
    async def _acquire(*args, **kwargs):
        yield object()

    monkeypatch.setattr("lucent.db.pool.scoped_acquire", _acquire)
    monkeypatch.setattr("lucent.db.models.ModelRepository", _FakeRepo)


async def test_resolver_registry_empty_falls_back_to_db(monkeypatch):
    monkeypatch.setattr("lucent.model_registry.list_decision_models", lambda: [])
    _fake_db_model(monkeypatch, "nimble")
    assert await resolve_decision_model_id(prefer_db=False) == "nimble"


async def test_resolver_db_first_when_prefer_db(monkeypatch):
    monkeypatch.setattr(
        "lucent.model_registry.list_decision_models",
        lambda: [
            ModelInfo(
                id="tev1", provider="ollama", name="Tev", category="decision",
                model_class="decision",
            )
        ],
    )
    _fake_db_model(monkeypatch, "nimble")
    assert await resolve_decision_model_id(prefer_db=True) == "nimble"


async def test_resolver_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr("lucent.model_registry.list_decision_models", lambda: [])
    _fake_db_model(monkeypatch, None)
    assert await resolve_decision_model_id(prefer_db=True) is None


async def test_resolver_db_failure_degrades_to_registry(monkeypatch):
    monkeypatch.setattr(
        "lucent.model_registry.list_decision_models",
        lambda: [
            ModelInfo(
                id="nimble", provider="ollama", name="Nimble", category="decision",
                model_class="decision",
            )
        ],
    )

    @asynccontextmanager
    async def _failing(*args, **kwargs):
        raise RuntimeError("db down")
        yield None

    monkeypatch.setattr("lucent.db.pool.scoped_acquire", _failing)
    assert await resolve_decision_model_id(prefer_db=True) == "nimble"


# ── env-driven flag (real settings path, no monkeypatched wrappers) ─────────


@pytest.mark.asyncio
async def test_gate_env_driven_flag_engages_pipeline(monkeypatch):
    monkeypatch.setenv("LUCENT_INJECT_DECISION_GATE_ENABLED", "true")
    monkeypatch.setenv("LUCENT_INJECT_DECISION_GATE_THRESHOLD", "0.5")
    # No OLLAMA_HOST in the test env → resolver returns None → gate disabled.
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr("lucent.model_registry.list_decision_models", lambda: [])

    async def _fail_db(*args, **kwargs):
        raise RuntimeError("no db in unit tests")
        yield None

    monkeypatch.setattr("lucent.db.pool.scoped_acquire", _fail_db)
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "hit memory")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=_messages("injection threshold question"),
        memory_bridge=bridge,
    )
    # Flag on but no decision model resolvable → standard path, no gate meta.
    assert "hit memory" in outcome.injectable_executions[0].text
    metadata = outcome.injectable_executions[0].metadata
    assert not [key for key in metadata if key.startswith("decision_")]
