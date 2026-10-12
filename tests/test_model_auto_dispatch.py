"""Auto model selection: daemon dispatch integration (Phase 3).

Harness mirrors tests/test_fusion_tasks.py: the full chat-path patch set
around ``_dispatch_tracked_tasks`` with ``_select_model_for_user`` left REAL
so the flag-gated arbiter engages end-to-end — ``lucent.llm.model_auto.decide``
is monkeypatched to a canned ``choice`` answer, the accessible-model pool is
faked, and the task events / persisted model settings are asserted.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from daemon.daemon import LucentDaemon
from lucent.llm.decision import (
    ChoiceAnswer,
    DecisionError,
    DecisionResponse,
    DecisionUsage,
)

ORG_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"
TASK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
NIMBLE = "11111111-aaaa-bbbb-cccc-111111111111"


def _task(**overrides):
    task = {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Auto probe task",
        "description": "Work that could run on several models",
        "agent_type": "code",
        "requesting_user_id": UUID(USER_ID),
    }
    task.update(overrides)
    return task


def _accessible(model_ids, context_window=100_000):
    return [
        SimpleNamespace(
            id=model_id,
            name=f"Model {model_id}",
            category="general",
            model_class="language",
            context_window=context_window,
            supports_tools=True,
            supports_vision=False,
            tags=[],
            notes="",
        )
        for model_id in model_ids
    ]


def _choice(choice_id, probabilities, confidence=0.9):
    return DecisionResponse(
        model="nimble",
        answers={
            "best_model": ChoiceAnswer(
                choice=choice_id, probabilities=probabilities, confidence=confidence
            )
        },
        usage=DecisionUsage(input_tokens=100, output_tokens=10),
    )


async def _resolve_nimble(_explicit, _org_id):
    return NIMBLE


async def _harness(
    monkeypatch,
    task,
    *,
    auto_enabled: bool,
    decide_responses: list | None = None,
    daemon_model: str | None = "daemon-default",
):
    """Full dispatch harness with the real model selection in place."""
    events: list[tuple[str, str, dict | None]] = []
    calls: dict[str, list] = {
        "sessions": [],
        "update_model": [],
        "usage": [],
        "started": [],
        "completed": [],
    }
    decide_requests: list = []
    queue = [task]

    async def _noop():
        return None

    async def _pending():
        return list(queue)

    async def _claim(_task_id, _instance_id):
        return {"id": _task_id, "claim_version": 1}

    async def _update_model(task_id, **kw):
        calls["update_model"].append((task_id, kw))
        return {"ok": True}

    async def _start(task_id, _instance_id=None):
        calls["started"].append(task_id)
        return {"id": task_id}

    async def _complete(task_id, result, instance_id=None, **kwargs):
        calls["completed"].append((task_id, result, kwargs))
        return {"id": task_id}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls.setdefault("failed", []).append((task_id, error))
        return {"id": task_id}

    async def _event(task_id, event_type, detail=None, metadata=None):
        events.append((event_type, detail, metadata))
        return {"id": task_id}

    async def _run_session(session_name, system_message, prompt, **kwargs):
        calls["sessions"].append((session_name, kwargs.get("model")))
        daemon._session_terminal_outcomes[session_name] = {
            "completed": True,
            "error": None,
        }
        return "Task done."

    async def _accessible(_user_id, _org_id):
        return _accessible_models(["chat-a", "chat-b"])

    async def _decide(request, **_kw):
        decide_requests.append(request)
        if decide_responses:
            response = decide_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        raise DecisionError("no canned arbiter response")

    async def _capture_usage(**kwargs):
        calls["usage"].append(kwargs)

    def _validate(_result, task=None, tool_counts=None):
        return (True, "")

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop)
    monkeypatch.setattr(daemon, "run_session", _run_session)
    monkeypatch.setattr(daemon, "_validate_task_result", _validate)
    daemon_module = __import__("daemon.daemon", fromlist=["x"])
    monkeypatch.setattr(
        daemon_module,
        "MCP_CONFIG",
        {
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {"Authorization": "Bearer daemon-key"},
                "tools": ["*"],
            }
        },
    )
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr(
        "daemon.daemon.RequestAPI.update_task_model_settings", _update_model
    )
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.mark_task_needs_review", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _noop_ctx)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", _noop_return_none)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _noop_agent)
    monkeypatch.setattr("daemon.daemon.load_accessible_skills_for_agent", _noop_list)
    monkeypatch.setattr("daemon.daemon.load_accessible_mcp_servers_for_agent", _noop_list)
    monkeypatch.setattr("daemon.daemon.load_accessible_hooks_for_agent", _noop_list)
    monkeypatch.setattr(
        "daemon.daemon.load_accessible_managed_tools_for_agent", _noop_list
    )
    monkeypatch.setattr("daemon.daemon.build_subagent_prompt", _noop_prompt)
    monkeypatch.setattr("daemon.daemon._mint_scoped_api_key", _noop_mint)
    monkeypatch.setattr("daemon.daemon._accessible_models_for_user", _accessible)
    monkeypatch.setattr("daemon.daemon.MODEL", daemon_model)
    monkeypatch.setattr(
        "lucent.settings.auto_selection_enabled",
        lambda **_kw: auto_enabled,
    )
    monkeypatch.setattr("lucent.llm.model_auto.decide", _decide)
    monkeypatch.setattr("lucent.llm.model_auto._resolve_arbiter_model", _resolve_nimble)
    monkeypatch.setattr("daemon.dispatch.model_auto._record_auto_usage", _capture_usage)
    return {
        "calls": calls,
        "events": events,
        "decide_requests": decide_requests,
        "daemon": daemon,
    }


def _accessible_models(
    model_ids: list[str], context_window: int = 100_000
) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            id=model_id,
            name=f"Model {model_id}",
            category="general",
            model_class="language",
            context_window=context_window,
            supports_tools=True,
            supports_vision=False,
            tags=[],
            notes="",
        )
        for model_id in model_ids
    ]


async def _noop_return_none(*_a, **_k):
    return None


async def _noop_agent(**_kw):
    return {
        "id": "55555555-5555-5555-5555-555555555555",
        "name": "code",
        "content": "agent",
    }


async def _noop_list(*_a, **_k):
    return []


async def _noop_prompt(*_a, **_kw):
    return "system prompt"


async def _noop_mint(**_kw):
    return "hs_task_scoped"


# ── dispatch behavior ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_sentinel_degrades_to_chain_with_event(monkeypatch):
    harness = await _harness(
        monkeypatch, _task(model="auto"), auto_enabled=False
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    events = [e for e, _d, _m in harness["events"]]
    assert "model_auto_disabled" in events
    assert [(n, m) for n, m in harness["calls"]["sessions"]] == [
        ("code-11111111", "chat-a")
    ]
    metadata = next(m for e, _d, m in harness["events"] if e == "model_auto_disabled")
    assert metadata["trigger"] == "sentinel"


@pytest.mark.asyncio
async def test_flag_on_sentinel_routes_through_arbiter(monkeypatch):
    harness = await _harness(
        monkeypatch,
        _task(model="auto"),
        auto_enabled=True,
        decide_responses=[_choice("chat-b", {"chat-a": 0.15, "chat-b": 0.8})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    event, detail, metadata = next(
        (e, d, m) for e, d, m in harness["events"] if e == "model_auto_selected"
    )
    assert metadata["chosen"] == "chat-b"
    assert metadata["source"] == "arbiter"
    assert metadata["trigger"] == "sentinel"
    assert metadata["arbiter_model"] == NIMBLE
    assert metadata["usage"] == {"input_tokens": 100, "output_tokens": 10}
    assert metadata["survivors"] == ["chat-a", "chat-b"]
    # The resolved concrete model is persisted and the session uses it.
    assert harness["calls"]["update_model"][0][1]["model"] == "chat-b"
    assert harness["calls"]["sessions"][0] == ("code-11111111", "chat-b")
    assert "chat-b" in detail


@pytest.mark.asyncio
async def test_flag_on_unset_model_also_routes_through_arbiter(monkeypatch):
    harness = await _harness(
        monkeypatch,
        _task(),
        auto_enabled=True,
        decide_responses=[_choice("chat-a", {"chat-a": 0.9, "chat-b": 0.05})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    metadata = next(m for e, _d, m in harness["events"] if e == "model_auto_selected")
    assert metadata["trigger"] == "unset_model"
    assert metadata["chosen"] == "chat-a"
    assert harness["calls"]["sessions"][0] == ("code-11111111", "chat-a")


@pytest.mark.asyncio
async def test_flag_off_unset_model_never_engages(monkeypatch):
    harness = await _harness(monkeypatch, _task(), auto_enabled=False)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    events = [e for e, _d, _m in harness["events"]]
    assert "model_auto_selected" not in events
    assert "model_auto_disabled" not in events
    # The daemon model is not in the faked pool, so the chain scores the
    # accessible models directly — still the old behavior, no arbiter call.
    assert harness["calls"]["sessions"][0] == ("code-11111111", "chat-a")
    assert harness["decide_requests"] == []


@pytest.mark.asyncio
async def test_arbiter_error_falls_back_to_exact_chain(monkeypatch):
    harness = await _harness(
        monkeypatch,
        _task(model="auto"),
        auto_enabled=True,
        decide_responses=[DecisionError("ollama down"), DecisionError("still down")],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    metadata = next(m for e, _d, m in harness["events"] if e == "model_auto_selected")
    assert metadata["source"] == "fallback"
    assert "still down" in metadata["fallback_reason"]
    # The chain scores the accessible pool (the daemon model is not faked in).
    assert harness["calls"]["sessions"][0][1] == "chat-a"


@pytest.mark.asyncio
async def test_below_floor_probability_uses_the_chain(monkeypatch):
    harness = await _harness(
        monkeypatch,
        _task(model="auto"),
        auto_enabled=True,
        decide_responses=[_choice("chat-b", {"chat-a": 0.45, "chat-b": 0.4})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    metadata = next(m for e, _d, m in harness["events"] if e == "model_auto_selected")
    assert metadata["source"] == "fallback"
    assert "below confidence floor" in metadata["fallback_reason"]


@pytest.mark.asyncio
async def test_explicit_model_never_consults_the_arbiter(monkeypatch):
    harness = await _harness(monkeypatch, _task(model="chat-b"), auto_enabled=True)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    events = [e for e, _d, _m in harness["events"]]
    assert "model_auto_selected" not in events
    assert harness["decide_requests"] == []
    assert harness["calls"]["sessions"][0] == ("code-11111111", "chat-b")


@pytest.mark.asyncio
async def test_no_accessible_models_still_denies(monkeypatch):
    harness = await _harness(monkeypatch, _task(model="auto"), auto_enabled=True)

    async def _empty(_user_id, _org_id):
        return []

    monkeypatch.setattr("daemon.daemon._accessible_models_for_user", _empty)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    assert harness["calls"].get("failed")
    events = [e for e, _d, _m in harness["events"]]
    assert "dispatch_denied" in events


@pytest.mark.asyncio
async def test_single_survivor_skips_the_arbiter_call(monkeypatch):
    harness = await _harness(monkeypatch, _task(model="auto"), auto_enabled=True)
    solo = _accessible_models(["chat-a"], context_window=1_000_000)

    async def _single(_user_id, _org_id):
        return solo

    monkeypatch.setattr("daemon.daemon._accessible_models_for_user", _single)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    assert harness["decide_requests"] == []
    metadata = next(m for e, _d, m in harness["events"] if e == "model_auto_selected")
    assert metadata["source"] == "arbiter"
    assert metadata["chosen"] == "chat-a"
    assert harness["calls"]["sessions"][0] == ("code-11111111", "chat-a")

async def _noop_ctx(*_a, **_k):
    return "", ""
