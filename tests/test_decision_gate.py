"""Tests for the decision-model pre-review gate (review lifecycle).

Covers the question/state builders, the noul-threshold pass rule, decision
model resolution, flag gating, fail-open behavior on decision errors, the
conservative auto-approve preconditions, and the pre-screen text rendering.
The full request-review lifecycle exercises (gate pass auto-completing the
request, gate fail escalating with findings) live in test_review_lifecycle.py.
"""

from uuid import uuid4

import pytest

import daemon.review.decision_gate as decision_gate
from daemon.daemon import LucentDaemon
from daemon.review.decision_gate import (
    DecisionGateOutcome,
    build_decision_questions,
    build_decision_state,
    evaluate_decision_response,
    maybe_decision_gate,
    pass_note,
    pre_screen_text,
    render_findings_block,
)
from daemon.review.lifecycle import _review_updatable_memories
from lucent.llm.decision import (
    DecisionError,
    DecisionResponse,
    DecisionUsage,
    NoulAnswer,
)
from lucent.model_registry import ModelInfo
from lucent.settings import clear_runtime_setting_cache, set_runtime_setting_cache

REVIEW_TITLE = "Post-completion review"


def _fake_daemon() -> LucentDaemon:
    return LucentDaemon()


def _task(**overrides) -> dict:
    data = {"id": str(uuid4()), "title": "Do a thing", "status": "completed"}
    data.update(overrides)
    return data


def _request_data(**overrides) -> dict:
    data = {
        "id": str(uuid4()),
        "title": "Ship a report",
        "description": "Write a concise report",
        "organization_id": str(uuid4()),
        "tasks": [_task()],
    }
    data.update(overrides)
    return data


def _response(scores: dict[str, float], *, model: str = "nimble") -> DecisionResponse:
    return DecisionResponse(
        model=model,
        answers={
            name: NoulAnswer(noul=score) for name, score in scores.items()
        },
        usage=DecisionUsage(input_tokens=10, output_tokens=2),
    )


# ── State building ────────────────────────────────────────────────────────


def test_state_includes_request_and_tasks():
    daemon = _fake_daemon()
    request_data = _request_data(
        tasks=[
            _task(),
            _task(title="Post-completion review", status="completed"),  # excluded: review task
            _task(title="Still running", status="pending"),  # excluded: not terminal
        ]
    )
    state, contracted = build_decision_state(
        daemon, request_data, char_budget=16000, item_budget=2000
    )
    assert "request" in state
    assert "Ship a report" in state["request"]
    # Only the single terminal non-review task got a per-task state field.
    assert "task_1" in state
    assert "task_2" not in state
    assert contracted == []


def test_state_contracted_tasks_get_state_fields_and_refs():
    daemon = _fake_daemon()
    request_data = _request_data(
        tasks=[
            _task(output_contract={"json_schema": {"type": "object"}}),
            _task(title="Second", output_contract={"json_schema": {"type": "object"}}),
        ]
    )
    state, contracted = build_decision_state(
        daemon, request_data, char_budget=16000, item_budget=2000
    )
    assert [idx for idx, _ in contracted] == [1, 2]
    assert "task_1" in state and "task_2" in state
    assert "output contract (json_schema)" in state["task_1"]


def test_state_contracted_tasks_capped_at_sixteen():
    daemon = _fake_daemon()
    request_data = _request_data(
        tasks=[
            _task(
                title=f"Contracted {i}",
                output_contract={"json_schema": {"type": "object"}},
            )
            for i in range(20)
        ]
    )
    _, contracted = build_decision_state(
        daemon, request_data, char_budget=16000, item_budget=100
    )
    assert len(contracted) == decision_gate.DECISION_GATE_MAX_CONTRACT_TASK_QUESTIONS


def test_state_non_contracted_tasks_stop_at_budget():
    daemon = _fake_daemon()
    request_data = _request_data(
        tasks=[
            _task(title=f"Task {i}", result="x" * 300)
            for i in range(100)
        ]
    )
    state, _ = build_decision_state(
        daemon, request_data, char_budget=1000, item_budget=300
    )
    included = len(state) - 1  # minus the "request" field
    assert 1 <= included < 100


# ── Question building ─────────────────────────────────────────────────────


def test_questions_always_include_aggregates():
    daemon = _fake_daemon()
    questions = build_decision_questions(daemon, _request_data(), [])
    assert set(questions) == {"request_fulfilled", "not_blocked"}
    assert all(q.type == "noul" for q in questions.values())


def test_questions_add_contract_and_durability():
    daemon = _fake_daemon()
    request_data = _request_data(
        target_repo="owner/repo",
        tasks=[_task(output_contract={"json_schema": {"type": "object"}})],
    )
    state, contracted = build_decision_state(
        daemon, request_data, char_budget=16000, item_budget=2000
    )
    questions = build_decision_questions(daemon, request_data, contracted)
    assert "contracts_satisfied" in questions
    assert "deliverables_durable" in questions
    assert "contract_task_1_satisfied" in questions
    assert "durably captured" in questions["deliverables_durable"].instructions


def test_questions_omit_contract_questions_without_contracts():
    daemon = _fake_daemon()
    request_data = _request_data(tasks=[_task(), _task()])
    questions = build_decision_questions(daemon, request_data, [])
    assert not any("contract" in name for name in questions)


def test_full_built_request_validates_against_systemone_limits():
    """The built state+questions must fit the client-enforced request caps."""
    daemon = _fake_daemon()
    request_data = _request_data(
        tasks=[
            _task(
                title=f"Contracted {i}",
                result="y" * 500,
                output_contract={
                    "json_schema": {"type": "object", "required": ["a" * 50]}
                },
            )
            for i in range(16)
        ]
    )
    state, contracted = build_decision_state(
        daemon, request_data, char_budget=16000, item_budget=2000
    )
    questions = build_decision_questions(daemon, request_data, contracted)
    from lucent.llm.decision import DecisionRequest, _validate_request

    request = DecisionRequest(
        model_id="nimble", state=state, questions=questions
    )
    _validate_request(request)  # raises on >64 questions / body > 64 KiB


# ── Pass rule ─────────────────────────────────────────────────────────────


def test_evaluate_all_passing():
    daemon = _fake_daemon()
    request_data = _request_data()
    questions = build_decision_questions(daemon, request_data, [])
    score_map = {name: 0.9 for name in questions}
    scores, failed = evaluate_decision_response(
        _response(score_map), questions, threshold=0.7
    )
    assert not failed
    assert set(scores) == set(questions)


def test_evaluate_single_failure_escorts_to_failed():
    daemon = _fake_daemon()
    request_data = _request_data()
    questions = build_decision_questions(daemon, request_data, [])
    score_map = {name: (0.1 if name == "not_blocked" else 0.95) for name in questions}
    scores, failed = evaluate_decision_response(
        _response(score_map), questions, threshold=0.7
    )
    assert set(failed) == {"not_blocked"}
    assert failed["not_blocked"] == questions["not_blocked"].instructions
    assert scores["not_blocked"] == pytest.approx(0.1)


def test_evaluate_missing_answer_fails_conservatively():
    daemon = _fake_daemon()
    request_data = _request_data()
    questions = build_decision_questions(daemon, request_data, [])
    scores, failed = evaluate_decision_response(
        _response({}), questions, threshold=0.7
    )
    assert set(failed) == set(questions)
    assert all(score == 0.0 for score in scores.values())


def test_evaluate_threshold_is_inclusive():
    daemon = _fake_daemon()
    questions = build_decision_questions(daemon, _request_data(), [])
    scores, failed = evaluate_decision_response(
        _response({name: 0.7 for name in questions}), questions, threshold=0.7
    )
    assert not failed


# ── Pre-screen text rendering ────────────────────────────────────────────


def _outcome(**overrides) -> DecisionGateOutcome:
    defaults = dict(
        verdict="failed",
        model_id="nimble",
        threshold=0.7,
        scores={"not_blocked": 0.12, "request_fulfilled": 0.91},
        failed={"not_blocked": "no blockers present"},
        question_count=2,
    )
    defaults.update(overrides)
    return DecisionGateOutcome(**defaults)


def test_findings_block_lists_failed_questions_with_probabilities():
    block = render_findings_block(_outcome())
    assert "DECISION PRE-SCREEN FINDINGS" in block
    assert "not_blocked (probability 0.12)" in block
    assert "no blockers present" in block
    assert "model: nimble" in block
    assert "threshold 0.70" in block


def test_findings_empty_when_gate_skipped_or_auto_approved():
    assert pre_screen_text(None) == ""
    assert pre_screen_text(DecisionGateOutcome(verdict="skipped")) == ""
    passed = _outcome(verdict="passed", failed={}, model_id="nimble")
    assert pre_screen_text(passed) == ""
    assert pass_note(passed).startswith("A decision-model pre-screen")


def test_blocked_pass_renders_note_with_reason():
    blocked = _outcome(
        verdict="passed",
        failed={},
        auto_approve_blocked=True,
        blocked_reason="request has a target repo/paths (durable-delivery review required)",
    )
    block = pre_screen_text(blocked)
    assert "DECISION PRE-SCREEN RESULTS" in block
    assert "full review is still required" in block
    assert "durable-delivery" in block
    # Not an escalation: no failed-question listing.
    assert "(probability" not in block


# ── Auto-approve preconditions ───────────────────────────────────────────


def test_precondition_blockers():
    daemon = _fake_daemon()
    assert daemon._is_request_review_task({"title": REVIEW_TITLE})

    # Failed task blocks approval.
    blocked = _request_data(tasks=[_task(status="failed", error="boom")])
    assert decision_gate._precondition_blockers(daemon, blocked, []) is not None

    # Updatable memories block approval; goal-type memories do not.
    linked = _request_data()
    updatable = _review_updatable_memories([{"memory_id": "m1", "memory_type": "technical"}])
    assert decision_gate._precondition_blockers(daemon, linked, updatable) is not None
    goals_only = _review_updatable_memories([{"memory_id": "g1", "memory_type": "goal"}])
    assert decision_gate._precondition_blockers(daemon, linked, goals_only) is None

    # Target repo blocks approval.
    assert decision_gate._precondition_blockers(
        daemon, _request_data(target_repo="owner/repo"), []
    ) is not None

    # A clean request has no blockers.
    assert decision_gate._precondition_blockers(daemon, _request_data(), []) is None


# ── Decision-model resolution ────────────────────────────────────────────


async def test_resolve_decision_model_explicit_override(monkeypatch):
    daemon = _fake_daemon()
    monkeypatch.setattr(
        "lucent.settings.decision_gate_model_id",
        lambda *, organization_id=None: "tev1",
    )

    model = await decision_gate.resolve_decision_model(daemon, "org")
    assert model == "tev1"


async def test_resolve_decision_model_rejects_registry_known_non_decision(monkeypatch):
    daemon = _fake_daemon()
    from lucent.model_registry import ModelInfo

    monkeypatch.setattr(
        "lucent.settings.decision_gate_model_id",
        lambda *, organization_id=None: "gemma3:4b",
    )
    monkeypatch.setattr(
        "lucent.model_registry.get_model",
        lambda model_id: ModelInfo(
            id="gemma3:4b", provider="ollama", name="Gemma", category="general"
        ),
    )

    def _unused_connect(*args, **kwargs):  # DB must not be consulted
        raise AssertionError("DB lookup should not run for an explicit setting")

    monkeypatch.setattr(decision_gate, "_warn", lambda *a, **k: None)
    model = await decision_gate.resolve_decision_model(daemon, "org")
    assert model is None


async def test_resolve_decision_model_db_first_pick(monkeypatch):
    daemon = _fake_daemon()

    class _FakeConn:
        async def close(self):
            pass

    class _FakeRepo:
        def __init__(self, conn):
            pass

        async def list_models(self, **kwargs):
            assert kwargs["model_class"] == "decision"
            assert kwargs["enabled_only"] is True
            assert kwargs["limit"] == 1
            return {
                "items": [{"id": "nimble", "name": "Nimble", "provider": "ollama"}]
            }

    async def fake_connect_scoped(dsn, *, organization_id=None, **kwargs):
        return _FakeConn()

    monkeypatch.setattr(
        "lucent.settings.decision_gate_model_id", lambda *, organization_id=None: None
    )
    monkeypatch.setattr("daemon.db_scope.connect_scoped", fake_connect_scoped)
    monkeypatch.setattr("lucent.db.models.ModelRepository", _FakeRepo)

    model = await decision_gate.resolve_decision_model(daemon, "org")
    assert model == "nimble"


async def test_resolve_decision_model_falls_back_to_memory_registry(monkeypatch):
    daemon = _fake_daemon()

    async def failing_connect(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "lucent.settings.decision_gate_model_id", lambda *, organization_id=None: None
    )
    monkeypatch.setattr("daemon.db_scope.connect_scoped", failing_connect)
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

    model = await decision_gate.resolve_decision_model(daemon, "org")
    assert model == "nimble"


async def test_resolve_decision_model_none_when_no_models(monkeypatch):
    daemon = _fake_daemon()

    class _FakeConn:
        async def close(self):
            pass

    class _FakeRepo:
        def __init__(self, conn):
            pass

        async def list_models(self, **kwargs):
            return {"items": []}

    async def fake_connect_scoped(dsn, *, organization_id=None, **kwargs):
        return _FakeConn()

    monkeypatch.setattr(
        "lucent.settings.decision_gate_model_id", lambda *, organization_id=None: None
    )
    monkeypatch.setattr("daemon.db_scope.connect_scoped", fake_connect_scoped)
    monkeypatch.setattr("lucent.db.models.ModelRepository", _FakeRepo)
    monkeypatch.setattr(
        "lucent.model_registry.list_decision_models", lambda: []
    )
    monkeypatch.setattr(decision_gate, "_warn", lambda *a, **k: None)

    assert await decision_gate.resolve_decision_model(daemon, "org") is None


# ── Flag gating & fail-open ──────────────────────────────────────────────


class _RecordingDecide:
    def __init__(self, response):
        self.response = response
        self.calls: list = []

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        return self.response


def _patch_settings(monkeypatch, *, enabled=True, threshold=0.7, model="nimble"):
    monkeypatch.setattr(
        "lucent.settings.decision_gate_enabled",
        lambda *, organization_id=None: enabled,
    )
    monkeypatch.setattr(
        "lucent.settings.decision_gate_threshold",
        lambda *, organization_id=None: threshold,
    )
    monkeypatch.setattr(
        "lucent.settings.decision_gate_model_id",
        lambda *, organization_id=None: model,
    )


async def test_gate_returns_skipped_when_flag_off(monkeypatch):
    daemon = _fake_daemon()
    recording = _RecordingDecide(_response({"request_fulfilled": 0.99}))
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch, enabled=False)

    outcome = await maybe_decision_gate(daemon, str(uuid4()), _request_data())

    assert outcome.verdict == "skipped"
    assert recording.calls == []


@pytest.mark.asyncio
async def test_gate_respects_org_scoped_flag_cache(monkeypatch):
    """Cache-based gating: org flag works through set_runtime_setting_cache."""
    org_id = "org-" + uuid4().hex[:12]
    daemon = _fake_daemon()
    recording = _RecordingDecide(None)
    monkeypatch.setattr(decision_gate, "decide", recording)
    monkeypatch.setattr(
        decision_gate, "resolve_decision_model", _async_return("nimble")
    )
    try:
        set_runtime_setting_cache(org_id, "requests.decision_gate_enabled", False)
        outcome = await maybe_decision_gate(
            daemon, str(uuid4()), _request_data(organization_id=org_id)
        )
        assert outcome.verdict == "skipped"
        assert recording.calls == []

        set_runtime_setting_cache(org_id, "requests.decision_gate_enabled", True)
        # decide is monkeypatched to return None — the gate must fail-open on
        # the malformed response, not crash.
        outcome = await maybe_decision_gate(
            daemon, str(uuid4()), _request_data(organization_id=org_id)
        )
        assert outcome.verdict == "skipped"
        assert outcome.error is not None
    finally:
        clear_runtime_setting_cache(org_id)


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


async def test_gate_passes_and_records_usage(monkeypatch):
    daemon = _fake_daemon()
    recording = _RecordingDecide(_response({"request_fulfilled": 0.95, "not_blocked": 0.88}))
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(
        decision_gate,
        "resolve_decision_model",
        _async_return("nimble"),
    )
    monkeypatch.setattr(
        "daemon.daemon.RequestAPI.get_request_memories", _async_return([])
    )

    outcome = await maybe_decision_gate(daemon, str(uuid4()), _request_data())

    assert outcome.verdict == "passed"
    assert outcome.auto_approve
    assert not outcome.failed
    assert outcome.question_count == 2
    assert outcome.input_tokens == 10
    assert outcome.output_tokens == 2
    assert len(recording.calls) == 1


async def test_gate_downgrades_pass_with_precondition_blocker(monkeypatch):
    daemon = _fake_daemon()
    recording = _RecordingDecide(
        _response(
            {
                "request_fulfilled": 0.95,
                "not_blocked": 0.88,
                "deliverables_durable": 0.9,
            }
        )
    )
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(decision_gate, "resolve_decision_model", _async_return("nimble"))
    monkeypatch.setattr(
        "daemon.daemon.RequestAPI.get_request_memories", _async_return([])
    )

    outcome = await maybe_decision_gate(
        daemon, str(uuid4()), _request_data(target_repo="owner/repo")
    )

    assert outcome.verdict == "passed"
    assert not outcome.auto_approve
    assert outcome.auto_approve_blocked
    assert "target repo" in (outcome.blocked_reason or "")


async def test_gate_fail_open_on_decision_error(monkeypatch):
    daemon = _fake_daemon()

    async def failing_decide(request, **kwargs):
        raise DecisionError("endpoint down")

    monkeypatch.setattr(decision_gate, "decide", failing_decide)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(decision_gate, "resolve_decision_model", _async_return("nimble"))

    outcome = await maybe_decision_gate(daemon, str(uuid4()), _request_data())

    assert outcome.verdict == "skipped"
    assert "endpoint down" in outcome.error
    assert not outcome.auto_approve


async def test_gate_skips_with_no_terminal_tasks(monkeypatch):
    daemon = _fake_daemon()
    recording = _RecordingDecide(None)
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    request_data = _request_data(tasks=[_task(status="pending")])

    outcome = await maybe_decision_gate(daemon, str(uuid4()), request_data)

    assert outcome.verdict == "skipped"
    assert recording.calls == []


async def test_gate_fail_on_low_score(monkeypatch):
    daemon = _fake_daemon()
    recording = _RecordingDecide(
        _response({"request_fulfilled": 0.3, "not_blocked": 0.95})
    )
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(decision_gate, "resolve_decision_model", _async_return("nimble"))

    outcome = await maybe_decision_gate(daemon, str(uuid4()), _request_data())

    assert outcome.verdict == "failed"
    assert set(outcome.failed) == {"request_fulfilled"}
    assert not outcome.auto_approve


# ── Review-lifecycle wire-up ─────────────────────────────────────────────
# Daemon-side tests through _create_request_review_task, following the
# monkeypatched-RequestAPI pattern used by tests/test_task_outputs.py.


_ORG = "22222222-2222-2222-2222-222222222222"
_USER = "33333333-3333-3333-3333-333333333333"


def _review_request_data(**overrides):
    data = {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "Ship a report",
        "description": "Write a concise report",
        "organization_id": _ORG,
        "created_by": _USER,
        "priority": "medium",
        "tasks": [
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "status": "completed",
                "title": "Write the report",
                "result": "Report produced and recorded.",
            }
        ],
    }
    data.update(overrides)
    return data


def _patch_daemon_review_machinery(monkeypatch, *, captured):
    """Stub the review plumbing around _create_request_review_task."""
    from daemon.daemon import LucentDaemon

    daemon = LucentDaemon()

    async def fake_find_review_agent_type(_org_id, _requesting_user_id):
        return "request-review", "primary"

    async def fake_resolve_review_requesting_user_id(*, org_id, requester_user_id):
        return requester_user_id

    async def fake_get_request_memories(_request_id):
        return []

    async def fake_create_task(**kwargs):
        captured["created"] = kwargs
        return {"id": "review-task-id", **kwargs}

    async def fake_select_model_for_user(**kwargs):
        captured["model_selected"] = True
        return "test-review-model", "unit test stub"

    monkeypatch.setattr(daemon, "_find_review_agent_type", fake_find_review_agent_type)
    monkeypatch.setattr(
        daemon, "_resolve_review_requesting_user_id", fake_resolve_review_requesting_user_id
    )
    monkeypatch.setattr(
        "daemon.daemon.RequestAPI.get_request_memories", fake_get_request_memories
    )
    monkeypatch.setattr("daemon.daemon.RequestAPI.create_task", fake_create_task)
    monkeypatch.setattr(
        "daemon.daemon._select_model_for_user", fake_select_model_for_user
    )
    return daemon


@pytest.mark.asyncio
async def test_gate_pass_auto_completes_without_llm_review(monkeypatch):
    """Pre-screen pass → review task created and auto-resolved to completed."""
    captured: dict = {}
    daemon = _patch_daemon_review_machinery(monkeypatch, captured=captured)
    status_updates: list = []
    events: list = []

    async def fake_update_request_status(request_id, status, **kwargs):
        status_updates.append((request_id, status))
        return {"id": request_id, "status": status}

    async def fake_add_event(task_id, event_type, detail=None, metadata=None):
        events.append((str(task_id), event_type, detail, metadata))
        return {"id": "event-id"}

    monkeypatch.setattr(
        "daemon.daemon.RequestAPI.update_request_status", fake_update_request_status
    )
    async def fake_get_request(request_id, **kwargs):
        return _review_request_data()

    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", fake_get_request)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", fake_add_event)

    async def fake_claim_task(task_id, instance_id):
        captured["claimed"] = (task_id, instance_id)
        return {"id": task_id, "status": "claimed"}

    async def fake_complete_task(task_id, result, instance_id=None, **kwargs):
        captured["completed"] = (task_id, result)
        return {
            "id": task_id,
            "status": "completed",
            "result": result,
            "request_id": "11111111-1111-1111-1111-111111111111",
        }

    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", fake_claim_task)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", fake_complete_task)

    async def forbidden(**kwargs):
        raise AssertionError("no LLM review model selection expected on auto-approve")

    monkeypatch.setattr("daemon.daemon._select_model_for_user", forbidden)

    recording = _RecordingDecide(
        _response({"request_fulfilled": 0.97, "not_blocked": 0.99})
    )
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(decision_gate, "resolve_decision_model", _async_return("nimble"))

    request_id = "11111111-1111-1111-1111-111111111111"
    review_task = await daemon._create_request_review_task(
        request_id, _review_request_data()
    )

    assert review_task is not None
    assert review_task["_decision_gate_auto_resolved"] is True
    # The audit record was created with the canonical review title.
    assert captured["created"]["title"] == "Post-completion review"
    assert "decision-model pre-screen" in captured["created"]["description"]
    # Claim → complete → request completed, all through the review lifecycle.
    assert captured["claimed"][0] == "review-task-id"
    assert "REQUEST_REVIEW_DECISION: APPROVED" in captured["completed"][1]
    assert status_updates == [(request_id, "completed")]
    # Pass event with gate metadata was emitted.
    event_types = [e[1] for e in events]
    assert "decision_gate_pre_screen" in event_types
    gate_event = next(e for e in events if e[1] == "decision_gate_pre_screen")
    assert gate_event[3]["verdict"] == "passed"
    assert gate_event[3]["model"] == "nimble"


@pytest.mark.asyncio
async def test_gate_fail_creates_review_task_with_findings(monkeypatch):
    captured: dict = {}
    daemon = _patch_daemon_review_machinery(monkeypatch, captured=captured)
    events: list = []

    async def fake_add_event(task_id, event_type, detail=None, metadata=None):
        events.append((str(task_id), event_type, detail, metadata))
        return {"id": "event-id"}

    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", fake_add_event)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("not expected on the findings path")

    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_request_status", forbidden)

    recording = _RecordingDecide(
        _response({"request_fulfilled": 0.2, "not_blocked": 0.95})
    )
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(decision_gate, "resolve_decision_model", _async_return("nimble"))

    review_task = await daemon._create_request_review_task(
        "11111111-1111-1111-1111-111111111111", _review_request_data()
    )

    assert review_task.get("_decision_gate_auto_resolved") is None
    description = captured["created"]["description"]
    assert "DECISION PRE-SCREEN FINDINGS" in description
    assert "request_fulfilled (probability 0.20)" in description
    # The LLM review model was selected — a real review session is expected.
    assert captured.get("model_selected") is True
    event_types = [e[1] for e in events]
    assert "decision_gate_findings" in event_types
    gate_event = next(e for e in events if e[1] == "decision_gate_findings")
    assert sorted(gate_event[3]["failed_questions"]) == ["request_fulfilled"]


@pytest.mark.asyncio
async def test_gate_off_keeps_current_path_byte_for_byte(monkeypatch):
    captured: dict = {}
    daemon = _patch_daemon_review_machinery(monkeypatch, captured=captured)

    recording = _RecordingDecide(None)
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch, enabled=False)

    review_task = await daemon._create_request_review_task(
        "11111111-1111-1111-1111-111111111111", _review_request_data()
    )

    assert review_task is not None
    assert recording.calls == []
    description = captured["created"]["description"]
    assert "DECISION PRE-SCREEN" not in description
    assert captured.get("model_selected") is True


@pytest.mark.asyncio
async def test_gate_passed_blocked_prepends_results_note(monkeypatch):
    """Screen passed but target-repo precondition → full review, pass note."""
    captured: dict = {}
    daemon = _patch_daemon_review_machinery(monkeypatch, captured=captured)
    events: list = []

    async def fake_add_event(task_id, event_type, detail=None, metadata=None):
        events.append((str(task_id), event_type, detail, metadata))
        return {"id": "event-id"}

    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", fake_add_event)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("blocked pass must not auto-resolve")

    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_request_status", forbidden)

    recording = _RecordingDecide(
        _response(
            {"request_fulfilled": 0.97, "not_blocked": 0.99, "deliverables_durable": 0.93}
        )
    )
    monkeypatch.setattr(decision_gate, "decide", recording)
    _patch_settings(monkeypatch)
    monkeypatch.setattr(decision_gate, "resolve_decision_model", _async_return("nimble"))

    review_task = await daemon._create_request_review_task(
        "11111111-1111-1111-1111-111111111111",
        _review_request_data(target_repo="owner/repo"),
    )

    assert review_task.get("_decision_gate_auto_resolved") is None
    description = captured["created"]["description"]
    assert "DECISION PRE-SCREEN RESULTS" in description
    assert "full review is still required" in description
    assert "(probability" not in description  # no failed-question listing
    assert captured.get("model_selected") is True


@pytest.mark.asyncio
async def test_auto_resolve_claim_race_leaves_task_for_llm_review(monkeypatch):
    """A lost claim race must leave the task pending, not break the flow."""
    daemon = _patch_daemon_review_machinery(
        monkeypatch, captured={}
    )

    async def fake_claim_task(task_id, instance_id):
        return None  # dispatcher won the race

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("complete must not run without a successful claim")

    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", fake_claim_task)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", forbidden)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_request_status", forbidden)

    assert await decision_gate._auto_resolve_review_task(
        daemon,
        {"id": "review-task-id"},
        "11111111-1111-1111-1111-111111111111",
        _outcome(verdict="passed", failed={}),
    ) is False
