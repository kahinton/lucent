"""Tests for the shared flow-gate mechanism (evaluate + propagate).

The mechanism is kind-agnostic: one function turns a validated structured
result into a gate outcome, and one function settles the remaining work. These
tests pin the contract that matters:

* a gate that evaluates to its stop value short-circuits downstream work while
  the gating task itself still COMPLETES successfully;
* a gate that evaluates true changes nothing at all;
* the gating task is never short-circuited;
* the post-completion review task is exempt, so a gated request still reaches
  review.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from daemon.dispatch import flow_gate, script_executor
from daemon.dispatch.flow_gate import evaluate_gate, propagate_flow_stop

TASK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"
TEMPLATE_ID = "55555555-5555-5555-5555-555555555555"


def _gate_task(*, stop_value: bool = False):
    return {
        "id": TASK_ID,
        "request_id": REQUEST_ID,
        "organization_id": ORG_ID,
        "title": "Decide whether to act",
        "output_contract": {
            "json_schema": {
                "type": "object",
                "required": ["should_act"],
                "properties": {"should_act": {"type": "boolean"}},
            },
            "gate": {"path": "should_act", "stop_value": stop_value},
        },
    }


# ── evaluate_gate ────────────────────────────────────────────────────────


def test_gate_false_is_a_stop_decision():
    outcome = evaluate_gate(_gate_task(), {"should_act": False})

    assert outcome.configured is True
    assert outcome.evaluated is True
    assert outcome.stop is True
    assert outcome.action == "stop_flow"
    assert outcome.decision == "stop_flow"
    assert outcome.value is False
    assert outcome.stop_value is False
    summary = outcome.summary()
    assert "Flow gate stopped the flow" in summary
    assert "no downstream action was taken" in summary
    # The value actually seen is recorded, not just the decision.
    assert "False" in summary


def test_gate_true_changes_nothing():
    outcome = evaluate_gate(_gate_task(), {"should_act": True})

    assert outcome.configured is True
    assert outcome.evaluated is True
    assert outcome.stop is False
    assert outcome.decision == "continue"
    assert outcome.summary("original summary") == "original summary"
    assert outcome.metadata()["gate_value"] is True


def test_no_gate_spec_is_not_configured():
    task = {
        "id": TASK_ID,
        "output_contract": {
            "json_schema": {"type": "object", "properties": {}},
        },
    }
    outcome = evaluate_gate(task, {"anything": True})
    assert outcome.configured is False
    assert outcome.stop is False


def test_dotted_path_and_list_index_resolution():
    task = {
        "id": TASK_ID,
        "output_contract": {
            "json_schema": {"type": "object"},
            "gate": {"path": "actions.0.proceed", "stop_value": False},
        },
    }
    outcome = evaluate_gate(task, {"actions": [{"proceed": False}]})
    assert outcome.evaluated is True
    assert outcome.stop is True
    assert outcome.value is False


def test_unresolvable_path_fails_closed():
    task = {
        "id": TASK_ID,
        "output_contract": {
            "json_schema": {"type": "object"},
            "gate": {"path": "missing.path", "stop_value": False},
        },
    }
    outcome = evaluate_gate(task, {"other": True})
    assert outcome.configured is True
    assert outcome.evaluated is False
    assert outcome.stop is True
    assert "does not exist" in (outcome.reason or "")


def test_explicit_spec_supports_the_decision_result_shape():
    """Decision parity reuses this function with its own threshold policy."""
    task = {"id": TASK_ID, "decision_config": {"threshold_policy": {"on_fail": "stop_flow"}}}
    spec = {"path": "threshold_failed", "stop_value": True, "action": "stop_flow"}

    failed = evaluate_gate(task, {"threshold_failed": ["quality"]}, spec=spec)
    assert failed.stop is True

    passed = evaluate_gate(task, {"threshold_failed": []}, spec=spec)
    assert passed.stop is False


# ── propagate_flow_stop ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propagate_short_circuits_and_emits_one_event(monkeypatch):
    calls = {"short_circuit": [], "events": []}

    async def _short_circuit(task_id, *, reason=None, metadata=None):
        calls["short_circuit"].append((task_id, reason, metadata))
        return [
            {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "title": "Publish"},
            {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "title": "Notify"},
        ]

    async def _event(task_id, event_type, detail=None, metadata=None):
        calls["events"].append((task_id, event_type, detail, metadata))
        return {"id": task_id}

    monkeypatch.setattr(
        flow_gate.runtime.RequestAPI,
        "short_circuit_pending_tasks",
        _short_circuit,
    )
    monkeypatch.setattr(flow_gate.runtime.RequestAPI, "add_event", _event)

    outcome = evaluate_gate(_gate_task(), {"should_act": False})
    settled = await propagate_flow_stop(task=_gate_task(), outcome=outcome)

    assert [item["title"] for item in settled] == ["Publish", "Notify"]
    assert calls["short_circuit"][0][0] == TASK_ID
    assert len(calls["events"]) == 1
    _, event_type, detail, metadata = calls["events"][0]
    assert event_type == "flow_gate_stopped"
    assert "Short-circuited 2 remaining task(s)" in detail
    assert "Publish" in detail and "Notify" in detail
    assert metadata["short_circuited_count"] == 2
    assert metadata["gate_value"] is False
    assert metadata["gate_decision"] == "stop_flow"


@pytest.mark.asyncio
async def test_propagate_does_nothing_when_gate_passes(monkeypatch):
    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("a passing gate must not propagate a flow stop")

    monkeypatch.setattr(
        flow_gate.runtime.RequestAPI,
        "short_circuit_pending_tasks",
        _forbidden,
    )

    outcome = evaluate_gate(_gate_task(), {"should_act": True})
    assert await propagate_flow_stop(task=_gate_task(), outcome=outcome) == []


# ── daemon wiring: the script kind ───────────────────────────────────────


def _script_task(**overrides):
    task = {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Run the gate script",
        "agent_type": "code",
        "requesting_user_id": UUID(USER_ID),
        "task_kind": "script",
        "execution_config": {"source": "inline", "script": "print('{}')"},
        "sandbox_config": {"working_dir": "/workspace", "timeout_seconds": 1800},
        "sandbox_template_id": UUID(TEMPLATE_ID),
        "output_contract": {
            "json_schema": {
                "type": "object",
                "required": ["should_act"],
                "properties": {"should_act": {"type": "boolean"}},
            },
            "gate": {"path": "should_act", "stop_value": False},
        },
    }
    task.update(overrides)
    return task


async def _dispatch_one_script(monkeypatch, *, should_act, run_summary="script finished"):
    """Run one script dispatch with fakes; return the recorded calls."""
    from daemon.daemon import LucentDaemon

    task = _script_task()
    calls = {
        "script": [],
        "completed": [],
        "failed": [],
        "events": [],
        "short_circuit": [],
        "review_ensure": 0,
        "order": [],
    }

    async def _noop(*_args, **_kwargs):
        return None

    async def _pending():
        return [task]

    async def _claim(task_id, _instance_id):
        return {"id": task_id, "organization_id": str(ORG_ID)}

    async def _matches(_claimed):
        return True

    async def _start(task_id, _instance_id=None):
        return {"id": task_id}

    async def _complete(task_id, result, instance_id=None, **kwargs):
        calls["completed"].append((task_id, result, kwargs))
        calls["order"].append("complete")
        return {"id": task_id}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls["failed"].append((task_id, error, result))
        return {"id": task_id}

    async def _event(task_id, event_type, detail=None, metadata=None):
        calls["events"].append((task_id, event_type, detail, metadata))
        return {"id": task_id}

    async def _short_circuit(task_id, *, reason=None, metadata=None):
        calls["short_circuit"].append((task_id, reason, metadata))
        calls["order"].append("short_circuit")
        return [{"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "title": "Downstream"}]

    async def _run_script(**kwargs):
        calls["script"].append(kwargs)
        await kwargs["on_complete"](
            str(kwargs["task"]["id"]),
            "done",
            result_structured={"should_act": should_act},
            result_summary=run_summary,
            validation_status="valid",
        )

    async def _ensure_review():
        calls["review_ensure"] += 1

    async def _entitled(_task):
        return None

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _ensure_review)
    monkeypatch.setattr(daemon, "_task_matches_bound_org", _matches)
    monkeypatch.setattr(script_executor, "run_script_task", _run_script)
    monkeypatch.setattr(script_executor, "script_entitlement_failure", _entitled)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr(
        "daemon.daemon.RequestAPI.short_circuit_pending_tasks", _short_circuit
    )

    await daemon._dispatch_tracked_tasks(max_tasks=1)
    return calls


@pytest.mark.asyncio
async def test_script_gate_false_short_circuits_downstream_and_completes(
    monkeypatch,
):
    calls = await _dispatch_one_script(monkeypatch, should_act=False)

    assert calls["failed"] == []
    assert len(calls["completed"]) == 1
    task_id, _result, kwargs = calls["completed"][0]
    # The gating task completes successfully with the gate outcome recorded.
    assert task_id == TASK_ID
    assert kwargs["result_summary"].startswith("Flow gate stopped the flow")
    assert "script finished" in kwargs["result_summary"]
    # The remaining flow is short-circuited, naming the gating task.
    assert len(calls["short_circuit"]) == 1
    assert calls["short_circuit"][0][0] == TASK_ID
    # One event records the count and which tasks were short-circuited.
    gate_events = [e for e in calls["events"] if e[1] == "flow_gate_stopped"]
    assert len(gate_events) == 1
    assert "Short-circuited 1 remaining task(s)" in gate_events[0][2]
    assert gate_events[0][3]["short_circuited_task_ids"] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ]
    # The review pass still runs for the request.
    assert calls["review_ensure"] == 1


@pytest.mark.asyncio
async def test_script_gate_true_is_byte_identical_to_today(monkeypatch):
    calls = await _dispatch_one_script(monkeypatch, should_act=True)

    assert calls["failed"] == []
    assert len(calls["completed"]) == 1
    _, _result, kwargs = calls["completed"][0]
    assert kwargs["result_summary"] == "script finished"
    assert calls["short_circuit"] == []
    assert [e for e in calls["events"] if e[1] == "flow_gate_stopped"] == []


@pytest.mark.asyncio
async def test_gating_task_is_completed_before_propagation(monkeypatch):
    """The gating task is settled first, so it can never be a short-circuit target."""
    calls = await _dispatch_one_script(monkeypatch, should_act=False)

    # The completion write lands before the short-circuit request, so the
    # server's pending/planned filter cannot include the gating task.
    assert calls["order"] == ["complete", "short_circuit"]
    assert calls["completed"][0][0] == TASK_ID  # the gate itself completed
    assert calls["short_circuit"][0][0] == TASK_ID  # addressed by gating task id
