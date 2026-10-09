"""Fusion configs: schema validation and task pairing rules."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from daemon.daemon import LucentDaemon
from lucent.db.requests import validate_fusion_config
from lucent.llm.decision import DecisionResponse, DecisionUsage, NoulAnswer


def _noul(name: str = "fulfilled"):
    return {"name": name, "type": "noul", "instructions": "Is it complete?"}


def _arbiter(**overrides):
    arbiter = {
        "questions": [_noul()],
        "threshold_policy": {"questions": {"fulfilled": {"min": 0.7}}},
    }
    arbiter.update(overrides)
    return arbiter


def _best_of_n(**config_overrides):
    config = {
        "mode": "best_of_n",
        "candidates": {"count": 3},
        "arbiter": _arbiter(),
    }
    config.update(config_overrides)
    return config


# ── structural validation ───────────────────────────────────────────────


def test_accepts_minimal_best_of_n():
    validate_fusion_config(_best_of_n())


def test_accepts_explicit_models_and_refine():
    validate_fusion_config(
        {
            "mode": "best_of_n",
            "candidates": {"models": ["m-a", "m-b"]},
            "arbiter": {"questions": [_noul()]},
            "refine": {"max_refine_passes": 2},
        }
    )


def test_accepts_synthesize_without_arbiter():
    validate_fusion_config(
        {
            "mode": "synthesize",
            "candidates": {"count": 2},
            "synthesis": {"model": "m-a"},
        }
    )


def test_rejects_missing_and_unknown_mode():
    with pytest.raises(ValueError):
        validate_fusion_config({"candidates": {"count": 2}})
    with pytest.raises(ValueError):
        validate_fusion_config({"mode": "cascade", "candidates": {"count": 2}})
    with pytest.raises(ValueError):
        validate_fusion_config("not a dict")


def test_rejects_unknown_keys():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(junk=1))
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"count": 2, "temperatures": [0.7, 0.9]}))


def test_rejects_candidates_without_models_or_count():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={}))
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"models": ["m-a"], "count": 2}))


def test_rejects_single_candidate_model():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"models": ["m-a"]}))


def test_rejects_repeated_candidate_models():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"models": ["m-a", "m-a"]}))


def test_rejects_candidates_beyond_max():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"count": 5}), max_candidates=4)
    with pytest.raises(ValueError):
        validate_fusion_config(
            _best_of_n(candidates={"models": ["a", "b", "c", "d", "e"]}),
            max_candidates=4,
        )
    validate_fusion_config(_best_of_n(candidates={"count": 5}), max_candidates=5)


def test_rejects_bad_candidate_count_type():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"count": True}))
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"count": 1}))
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(candidates={"count": "3"}))


def test_rejects_best_of_n_without_arbiter():
    with pytest.raises(ValueError):
        validate_fusion_config({"mode": "best_of_n", "candidates": {"count": 2}})


def test_validates_arbiter_questions_like_decision_questions():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(arbiter={"questions": []}))
    with pytest.raises(ValueError):
        validate_fusion_config(
            _best_of_n(arbiter={"questions": [{"name": "a", "type": "bogus", "instructions": "x"}]})
        )
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(arbiter={"questions": [_noul(), _noul()]}))


def test_validates_arbiter_threshold_policy():
    with pytest.raises(ValueError):
        validate_fusion_config(
            _best_of_n(
                arbiter={
                    "questions": [_noul()],
                    "threshold_policy": {"questions": {"missing": {"min": 0.5}}},
                }
            )
        )
    with pytest.raises(ValueError):
        validate_fusion_config(
            _best_of_n(
                arbiter={
                    "questions": [
                        {
                            "name": "a",
                            "type": "choice",
                            "instructions": "x",
                            "criteria": {"A": "", "B": ""},
                        }
                    ],
                    "threshold_policy": {"questions": {"a": {"min": 0.5}}},
                }
            )
        )


def test_synthesize_requires_synthesis_object():
    with pytest.raises(ValueError):
        validate_fusion_config({"mode": "synthesize", "candidates": {"count": 2}})


def test_synthesis_only_allowed_with_synthesize():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(synthesis={"model": "m-a"}))


def test_refine_only_allowed_with_best_of_n():
    with pytest.raises(ValueError):
        validate_fusion_config(
            {
                "mode": "synthesize",
                "candidates": {"count": 2},
                "synthesis": {},
                "refine": {"max_refine_passes": 1},
            }
        )


def test_rejects_bad_refine_passes():
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(refine={"max_refine_passes": 3}))
    with pytest.raises(ValueError):
        validate_fusion_config(_best_of_n(refine={"max_refine_passes": 0}))
    validate_fusion_config(_best_of_n(refine={"max_refine_passes": 1}))


# ── flag gating ─────────────────────────────────────────────────────────


def test_flag_disabled_rejects_mode():
    with pytest.raises(ValueError, match="best-of-n is not enabled"):
        validate_fusion_config(_best_of_n(), best_of_n_enabled=False)
    with pytest.raises(ValueError, match="synthesis is not enabled"):
        validate_fusion_config(
            {
                "mode": "synthesize",
                "candidates": {"count": 2},
                "synthesis": {},
            },
            synthesis_enabled=False,
        )


def test_flag_checks_do_not_block_the_other_mode():
    validate_fusion_config(
        {
            "mode": "synthesize",
            "candidates": {"count": 2},
            "synthesis": {"model": "m-a"},
        },
        best_of_n_enabled=False,
    )
    validate_fusion_config(_best_of_n(), synthesis_enabled=False)


# ── REST surface ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_create_task_fusion_flag_off_422(db_pool):
    from lucent.api.app import create_app
    from tests.test_decision_tasks import _override_user, _seed_decision_org

    prefix = f"fusion_off_{str(uuid4())[:8]}"
    _org, user, req = await _seed_decision_org(db_pool, prefix)
    app = _override_user(create_app(), user)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/requests/{str(req['id'])}/tasks",
            json={
                "title": "fan-out step",
                "fusion_config": {
                    "mode": "best_of_n",
                    "candidates": {"count": 2},
                    "arbiter": {"questions": [_noul()]},
                },
            },
        )
    assert resp.status_code == 422
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rest_create_task_fusion_accepted_when_enabled(db_pool):
    from lucent.api.app import create_app
    from lucent.db.runtime_settings import RuntimeSettingsRepository
    from lucent.settings import clear_runtime_setting_cache, set_runtime_setting_cache
    from tests.test_decision_tasks import _override_user, _seed_decision_org

    prefix = f"fusion_on_{str(uuid4())[:8]}"
    org, user, req = await _seed_decision_org(db_pool, prefix)
    await RuntimeSettingsRepository(db_pool).upsert_setting(
        organization_id=str(org["id"]),
        key="fusion.best_of_n_enabled",
        value=True,
        value_type="boolean",
        user_id=str(user["id"]),
    )
    set_runtime_setting_cache(org["id"], "fusion.best_of_n_enabled", True)
    app = _override_user(create_app(), user)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/requests/{str(req['id'])}/tasks",
                json={
                    "title": "fan-out step",
                    "fusion_config": {
                        "mode": "best_of_n",
                        "candidates": {"count": 2},
                        "arbiter": {"questions": [_noul()]},
                    },
                },
            )
        assert resp.status_code == 200, resp.text
        stored = resp.json()["fusion_config"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["mode"] == "best_of_n"
        assert stored["candidates"] == {"count": 2}
    finally:
        await RuntimeSettingsRepository(db_pool).delete_setting(
            str(org["id"]), "fusion.best_of_n_enabled"
        )
        clear_runtime_setting_cache(org["id"], "fusion.best_of_n_enabled")


@pytest.mark.asyncio
async def test_rest_create_task_fusion_pairing_422s(db_pool):
    from lucent.api.app import create_app
    from lucent.db.runtime_settings import RuntimeSettingsRepository
    from lucent.settings import clear_runtime_setting_cache, set_runtime_setting_cache
    from tests.test_decision_tasks import _override_user, _seed_decision_org

    prefix = f"fusion_pair_{str(uuid4())[:8]}"
    org, user, req = await _seed_decision_org(db_pool, prefix)
    await RuntimeSettingsRepository(db_pool).upsert_setting(
        organization_id=str(org["id"]),
        key="fusion.best_of_n_enabled",
        value=True,
        value_type="boolean",
        user_id=str(user["id"]),
    )
    set_runtime_setting_cache(org["id"], "fusion.best_of_n_enabled", True)
    app = _override_user(create_app(), user)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            base = f"/api/requests/{str(req['id'])}/tasks"

            # fusion_config alongside decision_config — the decision block
            # fires first (agent_type defaults to None), then the repo's
            # both-configs rule would catch it anyway.
            resp = await client.post(
                base,
                json={
                    "title": "step",
                    "decision_config": {"questions": [_noul()]},
                    "fusion_config": {"mode": "best_of_n", "candidates": {"count": 2}},
                },
            )
            assert resp.status_code == 422

            # fusion on a decision-step agent — the decision block fires
            # first (agent requires decision_config), and the repo's pairing
            # rule would block the remainder anyway
            resp = await client.post(
                base,
                json={
                    "title": "step",
                    "agent_type": "decision-step",
                    "fusion_config": {"mode": "best_of_n", "candidates": {"count": 2}},
                },
            )
            assert resp.status_code == 422
            detail = resp.json()["detail"]
            assert "chat agent" in detail or "requires decision_config" in detail

            # malformed config (mode gate passes via flag, shape still hard)
            resp = await client.post(
                base,
                json={"title": "step", "fusion_config": {"mode": "best_of_n"}},
            )
            assert resp.status_code == 422
            detail = resp.json()["detail"]
            assert "arbiter" in detail or "candidates" in detail
    finally:
        await RuntimeSettingsRepository(db_pool).delete_setting(
            str(org["id"]), "fusion.best_of_n_enabled"
        )
        clear_runtime_setting_cache(org["id"], "fusion.best_of_n_enabled")


# ── dispatch executor (Phase 3) ─────────────────────────────────────────
#
# Mirror of test_decision_tasks._dispatch_harness, extended for the fusion
# branch: the task needs the full chat-agent scaffolding (agent defs, scoped
# key, MCP carrier) faked like
# TestDispatchRequeuesInterruptedProviderSession does, plus canned candidate
# sessions and canned arbiter responses.

TASK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"


def _fusion_task(fusion_config, agent_type="code"):
    return {
        "id": UUID(TASK_ID),
        "request_id": UUID(REQUEST_ID),
        "organization_id": UUID(ORG_ID),
        "title": "Analyze the request",
        "description": "Produce the analysis",
        "agent_type": agent_type,
        "requesting_user_id": UUID(USER_ID),
        "fusion_config": fusion_config,
    }


def _set_flags(
    monkeypatch,
    *,
    best_of_n=False,
    synthesis=False,
    refine=False,
    max_candidates=4,
    arbiter_model=None,
):
    for name, value in (
        ("fusion_best_of_n_enabled", best_of_n),
        ("fusion_synthesis_enabled", synthesis),
        ("fusion_refine_loop_enabled", refine),
        ("fusion_max_candidates", max_candidates),
        ("fusion_arbiter_model_id", arbiter_model),
    ):
        monkeypatch.setattr(f"lucent.settings.{name}", lambda _value=value, **_kw: _value)


def _decide_response(answers):
    return DecisionResponse(
        model="nimble", answers=answers, usage=DecisionUsage(input_tokens=100, output_tokens=50)
    )


async def _fusion_harness(
    monkeypatch,
    fusion_config,
    *,
    best_of_n=True,
    synthesis=False,
    refine_flag=False,
    candidate_outcomes=None,
    synth_outcome="synthesized response",
    refine_outcome=None,
    decide_answers=None,
    decide_errors=None,
    accessible_models=("chat-a", "chat-b"),
    candidate_default_text="plain text",
):
    """Fake transport for _dispatch_tracked_tasks fusion tests.

    ``candidate_outcomes``: list of texts (or Exceptions) returned by
    -cand-N sessions in run order. ``decide_answers``: canned answers maps
    for arbiter calls in order (None entries raise DecisionError);
    keyed by QUESTION NAME (fulfilled) — the harness prefixes by candidate
    position automatically so tests stay readable.
    """
    from lucent.llm.decision import DecisionError

    events: list[tuple[str, str, dict | None]] = []
    calls: dict[str, list] = {
        "completed": [],
        "failed": [],
        "needs_review": [],
        "started": [],
        "sessions": [],
        "arbiter_usage": [],
        "validate_tool_counts": [],
        "select_model": [],
    }
    decide_requests: list = []
    queue = [_fusion_task(fusion_config)]

    async def _noop():
        return None

    async def _pending():
        return list(queue)

    async def _claim(task_id, _instance_id):
        return {"id": task_id, "claim_version": 1}

    async def _update_model(_task_id, **_kw):
        return {"ok": True}

    async def _start(task_id, _instance_id=None):
        calls["started"].append(task_id)
        return {"id": task_id}

    async def _complete(task_id, result, instance_id=None, **kwargs):
        calls["completed"].append((task_id, result, kwargs))
        return {"id": task_id}

    async def _fail(task_id, error, instance_id=None, result=None):
        calls["failed"].append((task_id, error, result))
        return {"id": task_id}

    async def _needs_review(task_id, error, instance_id=None, result=None):
        calls["needs_review"].append((task_id, error, result))
        return {"id": task_id}

    async def _event(task_id, event_type, detail=None, metadata=None):
        events.append((event_type, detail, metadata))
        return {"id": task_id}

    async def _run_session(session_name, system_message, prompt, **kwargs):
        calls["sessions"].append((session_name, kwargs.get("model"), prompt))
        daemon._session_terminal_outcomes[session_name] = {"completed": True, "error": None}
        if "-cand-" in session_name and candidate_outcomes:
            outcome = candidate_outcomes.pop(0)
        elif "-synth-" in session_name:
            outcome = synth_outcome
        elif "-refine-" in session_name:
            outcome = refine_outcome
        else:
            outcome = candidate_default_text
        if isinstance(outcome, Exception):
            raise outcome
        if "-cand-" in session_name:
            daemon._session_tool_trackers[session_name] = {"write_file": 1}
        return outcome

    async def _decide(request, **_kwargs):
        decide_requests.append(request)
        if decide_errors:
            error = decide_errors.pop(0)
            if error is not None:
                raise DecisionError(error)
        if not isinstance(decide_answers, list) or not decide_answers:
            raise DecisionError("no canned arbiter response")
        answers = decide_answers.pop(0)
        if answers is None:
            raise DecisionError("canned None")
        # Prefixed mapping: the harness hands the authoring answers keyed by
        # base question name; the executor asks prefixed names per candidate
        # (cand1_fulfilled...). Detect prefixes from the request and expand.
        already_prefixed = any(k.startswith("cand") for k in answers)
        if already_prefixed:
            # Explicit per-candidate scores keyed by the full prefixed name.
            expanded = {}
            for question_name in request.questions:
                if question_name not in answers:
                    raise DecisionError(f"no canned answer for {question_name}")
                expanded[question_name] = answers[question_name]
            return _decide_response(expanded)
        expanded = {}
        for question_name in request.questions:
            # The executor always prefixes scoring questions "cand{index}_".
            if question_name.startswith("cand") and "_" in question_name:
                base = question_name[question_name.index("_") + 1 :]
            else:
                base = question_name
            if base not in answers:
                raise DecisionError(f"no canned answer for {question_name}")
            expanded[question_name] = answers[base]
        return _decide_response(expanded)

    async def _usage(**kwargs):
        calls["arbiter_usage"].append(kwargs)

    async def _select_model(**_kw):
        calls["select_model"].append(True)
        return "chat-a", "selected"

    async def _accessible(_user_id, _org_id):
        return [SimpleNamespace(id=m, supports_tools=True) for m in accessible_models]

    def _validate(result, task=None, tool_counts=None):
        calls["validate_tool_counts"].append({"tool_counts": tool_counts})
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
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model_settings", _update_model)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.mark_task_needs_review", _needs_review)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _noop_ctx)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request", _noop_request)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _noop_agent)
    monkeypatch.setattr("daemon.daemon.load_accessible_skills_for_agent", _noop_list)
    monkeypatch.setattr("daemon.daemon.load_accessible_mcp_servers_for_agent", _noop_list)
    monkeypatch.setattr("daemon.daemon.load_accessible_hooks_for_agent", _noop_list)
    monkeypatch.setattr("daemon.daemon.load_accessible_managed_tools_for_agent", _noop_list)
    monkeypatch.setattr("daemon.daemon.build_subagent_prompt", _noop_prompt)
    monkeypatch.setattr("daemon.daemon._mint_scoped_api_key", _noop_mint)
    monkeypatch.setattr("daemon.daemon._select_model_for_user", _select_model)
    monkeypatch.setattr("daemon.daemon._accessible_models_for_user", _accessible)
    monkeypatch.setattr("daemon.dispatch.fusion.decide", _decide)
    monkeypatch.setattr("daemon.dispatch.fusion._fetch_request", _noop_fetch)
    monkeypatch.setattr("daemon.dispatch.fusion.record_decision_usage", _usage)
    monkeypatch.setattr("daemon.dispatch.decision_step.resolve_user_decision_model", _resolve_model)
    _set_flags(
        monkeypatch,
        best_of_n=best_of_n,
        synthesis=synthesis,
        refine=refine_flag,
        arbiter_model="nimble",
    )
    return {"calls": calls, "events": events, "decide_requests": decide_requests, "daemon": daemon}


async def _noop_ctx(*_a, **_k):
    return "", ""


async def _noop_request(*_a, **_k):
    return None


async def _noop_agent(**_kw):
    return {"id": "55555555-5555-5555-5555-555555555555", "name": "code", "content": "agent"}


async def _noop_list(*_a, **_k):
    return []


async def _noop_prompt(*_a, **_k):
    return "system prompt"


async def _noop_mint(**_kw):
    return "hs_task_scoped"


async def _noop_fetch(*_a, **_k):
    return {"id": REQUEST_ID, "title": "Ship the thing", "description": "Make it so"}


async def _resolve_model(**_kw):
    return "nimble"


def _noul_arbiter():
    return {"questions": [_noul()], "threshold_policy": {"questions": {"fulfilled": {"min": 0.7}}}}


def _explicit_best_of_n(arbiter=None, refine=None):
    config = {
        "mode": "best_of_n",
        "candidates": {"models": ["chat-a", "chat-b"]},
        "arbiter": arbiter if arbiter is not None else _noul_arbiter(),
    }
    if refine is not None:
        config["refine"] = refine
    return config


def _answer(score):
    return {"fulfilled": NoulAnswer(noul=score)}


# ── flag-off / unsupported fallbacks ────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_falls_back_to_single_model(monkeypatch):
    harness = await _fusion_harness(monkeypatch, _explicit_best_of_n(), best_of_n=False)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    events = [e for e, _d, _m in harness["events"]]
    assert "fusion_disabled" in events
    assert "fusion_candidate_done" not in events
    # Normal path ran exactly one session under its conventional name.
    assert [(n, m) for n, m, _p in calls["sessions"]] == [("code-11111111", "chat-a")]
    assert calls["completed"]


@pytest.mark.asyncio
async def test_sandboxed_task_declines_to_normal_path(monkeypatch):
    config = _explicit_best_of_n()
    queue_task = _fusion_task(config)
    queue_task["sandbox_config"] = {"image": "node:20"}
    harness = await _fusion_harness(monkeypatch, config)

    async def _sandbox(_task_id, _config, **_kw):
        return "sandbox-1234", None, None, False

    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _returns([queue_task]))
    monkeypatch.setattr(harness["daemon"], "_create_task_sandbox", _sandbox)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    events = [e for e, _d, _m in harness["events"]]
    assert "fusion_unsupported" in events
    detail = {d for e, d, _m in harness["events"] if e == "fusion_unsupported"}
    assert any("sandboxed task" in d for d in detail)
    assert harness["calls"]["completed"]


def _returns(items):
    async def _pending():
        return list(items)

    return _pending


# ── happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_best_of_n_scores_candidates_and_completes_winner(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=["candidate a analysis", "candidate b analysis"],
        decide_answers=[
            {
                "cand1_fulfilled": NoulAnswer(noul=0.9),
                "cand2_fulfilled": NoulAnswer(noul=0.5),
            }
        ],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    sessions = [(n, m) for n, m, _p in calls["sessions"]]
    assert sessions == [("code-11111111-cand-1", "chat-a"), ("code-11111111-cand-2", "chat-b")]
    (task_id, result, kwargs) = calls["completed"][0]
    assert task_id == TASK_ID
    assert result == "candidate a analysis"  # higher aggregate wins
    assert kwargs["validation_status"] == "not_applicable"  # no output_contract
    assert kwargs["result_summary"].startswith("[fusion best_of_n]")
    assert "winner=chat-a" in kwargs["result_summary"]
    assert kwargs["result_structured"]["fusion"]["winner"]["aggregate"] == 0.9
    assert kwargs["result_structured"]["fusion"]["threshold_failed"] == []
    # The winner's tool counts feed required-tool validation.
    assert calls["validate_tool_counts"][0]["tool_counts"] == {"write_file": 1}
    events = {e for e, _d, _m in harness["events"]}
    assert {"fusion_dispatched", "fusion_candidate_done", "fusion_completed"} <= events
    assert harness["calls"]["arbiter_usage"], "decision usage recorded"


@pytest.mark.asyncio
async def test_tie_breaks_toward_earlier_candidate(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=["candidate a analysis", "candidate b analysis"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.7)}],  # equal scores
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    (task_id, result, _kw) = harness["calls"]["completed"][0]
    assert result == "candidate a analysis"


@pytest.mark.asyncio
async def test_zero_candidates_fails_the_task(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=[RuntimeError("engine down"), RuntimeError("engine down")],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    assert calls["completed"] == []
    task_id, reason, _result = calls["failed"][0]
    assert task_id == TASK_ID
    assert "no candidate produced a usable result" in reason
    assert "fusion_failed" in [e for e, _d, _m in harness["events"]]


@pytest.mark.asyncio
async def test_single_successful_candidate_degrades_but_scores(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=[RuntimeError("engine down"), "candidate b analysis"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.9)}],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    events = [e for e, _d, _m in harness["events"]]
    assert "fusion_degraded_single_candidate" in events
    (task_id, result, _kw) = harness["calls"]["completed"][0]
    assert result == "candidate b analysis"
    assert harness["decide_requests"], "degraded candidate is still scored"


# ── threshold policy ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threshold_fail_routes_to_fail_task(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=["candidate a", "candidate b"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.3)}],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    assert calls["completed"] == []
    task_id, reason, _result = calls["failed"][0]
    assert "Fusion threshold policy failed" in reason
    assert "below min 0.7" in reason
    fusion_event = [m for e, _d, m in harness["events"] if e == "fusion_completed"][0]
    assert fusion_event["threshold_failed"] is True
    # Even the threshold-fail completion carries the token comparison totals.
    assert fusion_event["totals"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_threshold_needs_review_routes_to_review(monkeypatch):
    arbiter = _noul_arbiter()
    arbiter["threshold_policy"]["on_fail"] = "needs_review"
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(arbiter=arbiter),
        candidate_outcomes=["candidate a", "candidate b"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.3)}],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    assert calls["failed"] == []
    task_id, reason, _result = calls["needs_review"][0]
    assert task_id == TASK_ID
    assert "Fusion threshold policy failed" in reason


# ── count-based candidate resolution ────────────────────────────────────


@pytest.mark.asyncio
async def test_count_based_candidates_fill_from_accessible_models(monkeypatch):
    config = _explicit_best_of_n()
    config["candidates"] = {"count": 3}
    harness = await _fusion_harness(
        monkeypatch,
        config,
        candidate_outcomes=["a", "b", "c"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.9)}],
        accessible_models=("chat-a", "chat-b", "chat-c"),
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    models = [m for _n, m, _p in harness["calls"]["sessions"]]
    assert models == ["chat-a", "chat-b", "chat-c"]


# ── tracker hygiene + usage totals ──────────────────────────────────────


@pytest.mark.asyncio
async def test_tracker_keys_pop_only_candidate_sessions(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=["candidate a", "candidate b"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.9)}],
    )
    daemon = harness["daemon"]
    # Pre-seed a NORMAL-path session's bookkeeping (same task!): fusion must
    # never clobber it.
    daemon._session_terminal_outcomes["code-11111111"] = {"completed": True, "error": None}
    daemon._session_usage_totals["code-11111111"] = {"input_tokens": 5, "output_tokens": 5}
    await daemon._dispatch_tracked_tasks(max_tasks=1)

    # Fusion sessions' own keys are popped by the executor...
    assert "code-11111111-cand-1" not in daemon._session_usage_totals
    assert "code-11111111-cand-2" not in daemon._session_tool_trackers
    # ...and the normal-path key survives untouched.
    assert daemon._session_terminal_outcomes["code-11111111"] == {"completed": True, "error": None}
    assert daemon._session_usage_totals["code-11111111"] == {"input_tokens": 5, "output_tokens": 5}


@pytest.mark.asyncio
async def test_usage_totals_reported_in_fusion_completed(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _explicit_best_of_n(),
        candidate_outcomes=["candidate a", "candidate b"],
        decide_answers=[{"fulfilled": NoulAnswer(noul=0.9)}],
    )
    daemon = harness["daemon"]

    async def _usage_session(session_name, *_a, **_kw):
        daemon._session_terminal_outcomes[session_name] = {"completed": True, "error": None}
        daemon._session_usage_totals[session_name] = {"input_tokens": 1200, "output_tokens": 800}
        if "-cand-" in session_name:
            daemon._session_tool_trackers[session_name] = {"write_file": 1}
        return "cand text"

    monkeypatch.setattr(daemon, "run_session", _usage_session)
    await daemon._dispatch_tracked_tasks(max_tasks=1)

    fusion_event = [m for e, _d, m in harness["events"] if e == "fusion_completed"][0]
    totals = fusion_event["totals"]
    assert totals["candidates_input"] == 2400 and totals["candidates_output"] == 1600
    # The canned arbiter response itself carries usage (100 in / 50 out).
    assert totals["arbiter_input"] == 100 and totals["arbiter_output"] == 50
    assert totals["total_tokens"] == 4150
    assert fusion_event["arbiter"]["input_tokens"] == 100


# ── synthesis ───────────────────────────────────────────────────────────


def _synthesize_config():
    return {
        "mode": "synthesize",
        "candidates": {"models": ["chat-a", "chat-b"]},
        "arbiter": _noul_arbiter(),
        "synthesis": {"model": "chat-s"},
    }


@pytest.mark.asyncio
async def test_synthesize_runs_third_session_and_re_scores(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _synthesize_config(),
        synthesis=True,
        candidate_outcomes=["candidate a analysis", "candidate b analysis"],
        decide_answers=[
            {"cand1_fulfilled": NoulAnswer(noul=0.4), "cand2_fulfilled": NoulAnswer(noul=0.6)},
            {"cand0_fulfilled": NoulAnswer(noul=0.85)},
        ],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    sessions = [(n, m) for n, m, _p in calls["sessions"]]
    assert sessions[-1] == ("code-11111111-synth-0", "chat-s")
    synth_prompt = calls["sessions"][-1][2]
    assert "Blend them into one best response" in synth_prompt
    assert "candidate a analysis" in synth_prompt
    (task_id, result, kwargs) = calls["completed"][0]
    assert result == "synthesized response"
    assert kwargs["result_structured"]["fusion"]["winner"]["aggregate"] == 0.85
    # Two arbiter decide() calls: bulk scoring + synthesis re-score.
    assert len(harness["decide_requests"]) == 2


@pytest.mark.asyncio
async def test_synthesize_without_arbiter_ships_synthesis(monkeypatch):
    config = _synthesize_config()
    config.pop("arbiter")
    harness = await _fusion_harness(
        monkeypatch, config, synthesis=True, candidate_outcomes=["a", "b"]
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    calls = harness["calls"]
    assert calls["completed"], "synthesis ships when the arbiter is not configured"
    assert harness["decide_requests"] == []


# ── refine loop ─────────────────────────────────────────────────────────


def _refine_best_of_n(**overrides):
    return _explicit_best_of_n(refine={"model": "chat-a", "max_refine_passes": 1}, **overrides)


@pytest.mark.asyncio
async def test_refine_taken_when_strictly_better(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _refine_best_of_n(),
        refine_flag=True,
        candidate_outcomes=["candidate a", "candidate b"],
        refine_outcome="refined response",
        decide_answers=[
            {"cand1_fulfilled": NoulAnswer(noul=0.5), "cand2_fulfilled": NoulAnswer(noul=0.4)},
            {"cand0_fulfilled": NoulAnswer(noul=0.95)},
        ],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    (task_id, result, kwargs) = calls["completed"][0]
    assert result == "refined response"
    assert kwargs["result_structured"]["fusion"]["winner"]["aggregate"] == 0.95
    sessions = [n for n, _m, _p in calls["sessions"]]
    assert "code-11111111-refine-0" in sessions
    refine_event = [(_d, m) for e, _d, m in harness["events"] if e == "fusion_refine_done"]
    assert refine_event and "TAKEN" in refine_event[0][0]
    # Thresholds still pass (0.95 >= 0.7) so completion happened.


@pytest.mark.asyncio
async def test_refine_discarded_when_not_strictly_better(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _refine_best_of_n(),
        refine_flag=True,
        candidate_outcomes=["candidate a wins", "candidate b"],
        refine_outcome="refined response",
        decide_answers=[
            {"cand1_fulfilled": NoulAnswer(noul=0.9), "cand2_fulfilled": NoulAnswer(noul=0.5)},
            {"cand0_fulfilled": NoulAnswer(noul=0.9)},  # tie — never adopted
        ],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    (task_id, result, _kw) = harness["calls"]["completed"][0]
    assert result == "candidate a wins"  # original winner ships
    assert "discarded" in [d for e, d, _m in harness["events"] if e == "fusion_refine_done"][0]


@pytest.mark.asyncio
async def test_refine_flag_off_skips_the_pass(monkeypatch):
    harness = await _fusion_harness(
        monkeypatch,
        _refine_best_of_n(),
        refine_flag=False,
        candidate_outcomes=["candidate a", "candidate b"],
        decide_answers=[
            {
                "cand1_fulfilled": NoulAnswer(noul=0.5),
                "cand2_fulfilled": NoulAnswer(noul=0.4),
            }
        ],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    sessions = [n for n, _m, _p in harness["calls"]["sessions"]]
    assert not any("refine" in s for s in sessions)
    assert "fusion_refine_skipped" in [e for e, _d, _m in harness["events"]]
    # Below threshold → task fails exactly as without refine config.
    assert harness["calls"]["completed"] == []
    assert "Fusion threshold policy failed" in harness["calls"]["failed"][0][1]
