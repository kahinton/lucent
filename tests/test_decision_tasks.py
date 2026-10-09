"""Decision-step tasks: config validation, dispatch executor, and surfaces."""

import json
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from daemon.daemon import LucentDaemon
from daemon.daemon import ModelAccessDeniedError as ChatModelAccessDeniedError
from daemon.dispatch.decision_step import (
    ModelAccessDeniedError,
    build_task_decision_questions,
    build_task_decision_state,
    decision_result_structured,
    render_decision_result,
)
from lucent.db.requests import validate_decision_config
from lucent.llm.decision import (
    ChoiceAnswer,
    DecisionError,
    DecisionResponse,
    DecisionUsage,
    NoulAnswer,
    ScoreAnswer,
)

# ── decision_config validation ──────────────────────────────────────────


def _noul(name: str = "fulfilled"):
    return {"name": name, "type": "noul", "instructions": "Was it fulfilled?"}


def test_validate_decision_config_accepts_minimal():
    validate_decision_config({"questions": [_noul()]})


def test_validate_decision_config_rejects_empty_and_many():
    with pytest.raises(ValueError):
        validate_decision_config({"questions": []})
    too_many = [{"name": f"q{i}", "type": "noul", "instructions": "x"} for i in range(65)]
    with pytest.raises(ValueError):
        validate_decision_config({"questions": too_many})


def test_validate_decision_config_rejects_bad_type_and_bad_criteria():
    with pytest.raises(ValueError):
        validate_decision_config(
            {"questions": [{"name": "a", "type": "bogus", "instructions": "x"}]}
        )
    with pytest.raises(ValueError):
        validate_decision_config(
            {
                "questions": [
                    {
                        "name": "a",
                        "type": "choice",
                        "instructions": "x",
                        "criteria": {"only": "one"},
                    }
                ]
            }
        )
    with pytest.raises(ValueError):
        validate_decision_config(
            {
                "questions": [
                    {
                        "name": "a",
                        "type": "score",
                        "instructions": "x",
                        "criteria": [
                            "low",
                            "high",
                            "medium",
                            "high",
                            "low",
                            "x",
                            "y",
                            "z",
                            "w",
                            "v",
                            "u",
                            "t",
                            "s",
                            "r",
                            "q",
                            "p",
                            "o",
                            "n",
                            "m",
                            "l",
                            "k",
                            "j",
                            "i",
                            "h",
                            "g",
                            "f",
                            "e",
                        ],
                    }
                ]
            }
        )


def test_validate_decision_config_rejects_duplicates_and_unknown_keys():
    with pytest.raises(ValueError):
        validate_decision_config({"questions": [_noul(), _noul()]})
    with pytest.raises(ValueError):
        validate_decision_config({"questions": [_noul()], "junk": 1})
    with pytest.raises(ValueError):
        validate_decision_config({"questions": [{"name": "a", "type": "noul"}]})


def test_validate_decision_config_threshold_rules():
    good = {"questions": [_noul()]}
    with pytest.raises(ValueError):
        validate_decision_config({**good, "threshold_policy": {"questions": {"missing": 0.5}}})
    with pytest.raises(ValueError):
        validate_decision_config(
            {
                **good,
                "threshold_policy": {
                    "questions": {"fulfilled": {"min": 0.6}},
                    "on_fail": "explode",
                },
            }
        )
    choice = {
        "questions": [
            {
                "name": "route",
                "type": "choice",
                "instructions": "pick",
                "criteria": {"a": "1", "b": "2"},
            }
        ]
    }
    with pytest.raises(ValueError):
        validate_decision_config({**choice, "threshold_policy": {"questions": {"route": 0.5}}})
    with pytest.raises(ValueError):
        validate_decision_config(
            {**good, "threshold_policy": {"questions": {"fulfilled": {"min": 5}}}}
        )
    # noul threshold at the bounds is fine; score thresholds are index-based
    validate_decision_config(
        {**good, "threshold_policy": {"questions": {"fulfilled": {"min": 1.0}}}}
    )
    validate_decision_config(
        {
            "questions": [
                {
                    "name": "s",
                    "type": "score",
                    "instructions": "x",
                    "criteria": ["low", "high"],
                }
            ],
            "threshold_policy": {"questions": {"s": 3}},
        }
    )


def test_validate_decision_config_state_must_be_strings():
    with pytest.raises(ValueError):
        validate_decision_config({"questions": [_noul()], "state": {"k": 5}})


def test_validate_decision_config_none_is_valid():
    validate_decision_config(None)
    with pytest.raises(ValueError):
        validate_decision_config("not-a-dict")


# ── executor unit surface ───────────────────────────────────────────────


def _response(answers, model="nimble"):
    return DecisionResponse(
        model=model,
        answers=answers,
        usage=DecisionUsage(input_tokens=10, output_tokens=5),
    )


def _noul_config(min_value=None):
    config = {
        "questions": [
            {
                "name": "fulfilled",
                "type": "noul",
                "instructions": "Was it fulfilled?",
            }
        ]
    }
    if min_value is not None:
        config["threshold_policy"] = {
            "questions": {"fulfilled": {"min": min_value}},
            "on_fail": "fail_task",
        }
    return config


def test_build_task_decision_state_fields_and_author_pins():
    state = build_task_decision_state(
        request_data={"title": "Ship thing", "description": "Make it so"},
        task={"title": "Verify", "description": "Check output", "id": "t1"},
        sibling_tasks=[
            {
                "id": "s1",
                "status": "completed",
                "title": "Implement",
                "result": "Done, PR #12 merged",
                "result_structured": {"summary": "done"},
            },
            {"id": "pending", "status": "pending", "title": "Pending step"},
        ],
        config_state={"extra": "lit"},
    )
    assert "Title: Ship thing" in state["request"]
    assert "Check output" in state["task"]
    assert "[completed] Implement" in state["context"]
    assert "structured result" in state["context"]
    assert "[pending] Pending step" not in state["context"]
    assert state["extra"] == "lit"


def test_build_task_decision_questions_preserves_order():
    config = {
        "questions": [
            {"name": "b", "type": "noul", "instructions": "x"},
            {
                "name": "a",
                "type": "choice",
                "instructions": "x",
                "criteria": {"one": "1", "two": "2"},
            },
            {"name": "c", "type": "score", "instructions": "x", "criteria": ["l", "h"]},
        ]
    }
    questions = build_task_decision_questions(config)
    assert list(questions) == ["b", "a", "c"]
    assert questions["b"].criteria is None


def test_render_and_structured_payload_shape():
    config = _noul_config(min_value=0.6)
    response = _response({"fulfilled": NoulAnswer(noul=0.87)})
    text = render_decision_result(response, config, [])
    assert "probability 0.87" in text and "PASS" in text
    structured = decision_result_structured(response, config, [])
    assert structured["scores"] == {"fulfilled": 0.87}
    assert structured["thresholds"]["fulfilled"] == {"min": 0.6, "met": True}
    assert structured["threshold_failed"] == []
    assert structured["on_fail"] == "fail_task"

    failed = ["fulfilled"]
    text_fail = render_decision_result(response, config, failed)
    assert "FAIL" in text_fail
    structured_fail = decision_result_structured(response, config, failed)
    assert structured_fail["threshold_failed"] == ["fulfilled"]


def test_structured_payload_types():
    config = {
        "questions": [
            {
                "name": "route",
                "type": "choice",
                "instructions": "pick",
                "criteria": {"fast": "", "slow": ""},
            },
            {
                "name": "depth",
                "type": "score",
                "instructions": "x",
                "criteria": ["shallow", "deep"],
            },
        ]
    }
    response = _response(
        {
            "route": ChoiceAnswer(
                choice="fast", probabilities={"fast": 0.7, "slow": 0.3}, confidence=0.8
            ),
            "depth": ScoreAnswer(
                score=1.4, legend={"0": "shallow", "1": "deep"}, probabilities={}, confidence=0.7
            ),
        }
    )
    structured = decision_result_structured(response, config, [])
    assert structured["choices"] == {"route": "fast"}
    assert structured["scores"] == {"depth": 1.4}
    assert structured["answers"]["route"]["probabilities"]["fast"] == 0.7
    assert "depth" not in structured["choices"]
    assert "route" not in structured["scores"]


# ── repository persistence ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_task_round_trips_decision_config(db_pool):
    from lucent.db import OrganizationRepository
    from lucent.db.requests import RequestRepository

    prefix = f"dec_cfg_{str(uuid4())[:8]}"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    repo = RequestRepository(db_pool)
    req = await repo.create_request(
        title=f"{prefix} request",
        org_id=str(org["id"]),
        created_by=None,
        description="for decision config",
    )
    config = {"questions": [_noul()], "state": {"extra": "lit"}}
    task = await repo.create_task(
        request_id=str(req["id"]),
        title="Decision step",
        org_id=str(org["id"]),
        agent_type="decision-step",
        decision_config=config,
    )
    # asyncpg returns jsonb columns as strings until a row-parsing layer
    # reads them (get_request _parse_jsonb); the dispatch branch parses the
    # string form itself, so round-trip through json.loads here.
    assert json.loads(task["decision_config"]) == config
    with pytest.raises(ValueError):
        await repo.create_task(
            request_id=str(req["id"]),
            title="Decision step",
            org_id=str(org["id"]),
            agent_type="decision-step",
            output_contract={"json_schema": {"type": "object"}},
            decision_config=config,
        )


# ── dispatch branch ─────────────────────────────────────────────────────

TASK_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
USER_ID = "44444444-4444-4444-4444-444444444444"


def _pending_task(decision_config=None, model=None):
    row = {
        "id": TASK_ID,
        "request_id": REQUEST_ID,
        "organization_id": ORG_ID,
        "title": "Check fulfillment",
        "description": "Judge the sibling output",
        "agent_type": "decision-step",
        "requesting_user_id": USER_ID,
    }
    if decision_config is not None:
        row["decision_config"] = decision_config
    if model:
        row["model"] = model
    return row


async def _noop_async():
    return None


async def _dispatch_harness(
    monkeypatch,
    task,
    *,
    responses=None,
    claim_value=True,
    decision_model="nimble",
    resolve_error=None,
):
    """Shared fake transport for _dispatch_tracked_tasks decision tests."""
    events: list[tuple[str, str]] = []
    calls: dict[str, list] = {
        "completed": [],
        "failed": [],
        "needs_review": [],
        "started": [],
        "released": [],
        "models": [],
        "usage": [],
        "select_model": [],
    }
    decide_requests: list = []
    queue = [task]

    async def _pending():
        return list(queue)

    async def _claim(task_id, _instance_id):
        return {"id": task_id} if claim_value else None

    async def _update_model(_task_id, **kwargs):
        calls["models"].append(kwargs)
        return {"ok": True}

    async def _start(task_id, instance_id=None):
        calls["started"].append(task_id)
        return {"id": task_id}

    async def _release(task_id, instance_id=None):
        calls["released"].append(task_id)
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

    async def _event(task_id, event_type, detail=None):
        events.append((event_type, detail))
        return {"id": task_id}

    async def _fetch_request(request_id):
        return {
            "id": REQUEST_ID,
            "title": "Ship the thing",
            "description": "Make it so",
            "tasks": [
                {
                    "id": "99999999-9999-9999-9999-999999999999",
                    "status": "completed",
                    "title": "Implement",
                    "result": "PR merged",
                }
            ],
        }

    responses = list(responses or [])

    async def _decide(request, **_kwargs):
        decide_requests.append(request)
        if responses:
            outcome = responses.pop(0)
        else:
            outcome = DecisionError("no canned response")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _resolve(**_kwargs):
        if resolve_error is not None:
            raise resolve_error
        return decision_model

    async def _usage(**kwargs):
        calls["usage"].append(kwargs)

    def _select_model(**_kwargs):
        calls["select_model"].append(True)
        return "chat-model", "selected"

    daemon = LucentDaemon()
    monkeypatch.setattr(daemon, "_ensure_request_review_tasks", _noop_async)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model_settings", _update_model)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.RequestAPI.release_task", _release)
    monkeypatch.setattr("daemon.daemon.RequestAPI.complete_task", _complete)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.mark_task_needs_review", _needs_review)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.dispatch.decision_step._fetch_request", _fetch_request)
    monkeypatch.setattr("daemon.dispatch.decision_step.decide", _decide)
    monkeypatch.setattr("daemon.dispatch.decision_step.resolve_user_decision_model", _resolve)
    monkeypatch.setattr("daemon.dispatch.decision_step.record_decision_usage", _usage)
    monkeypatch.setattr("daemon.daemon._select_model_for_user", _select_model)
    return {"calls": calls, "events": events, "decide_requests": decide_requests, "daemon": daemon}


@pytest.mark.asyncio
async def test_decision_step_completes_without_session(monkeypatch):
    harness = await _dispatch_harness(
        monkeypatch,
        _pending_task(decision_config=_noul_config(min_value=0.6)),
        responses=[_response({"fulfilled": NoulAnswer(noul=0.87)})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    assert calls["select_model"] == []  # chat model selection never runs
    assert calls["started"] == [TASK_ID]
    assert calls["models"] and calls["models"][0]["model"] == "nimble"
    (task_id, result, kwargs) = calls["completed"][0]
    assert task_id == TASK_ID
    assert kwargs["validation_status"] == "valid"
    assert kwargs["result_structured"]["scores"] == {"fulfilled": 0.87}
    assert kwargs["result_structured"]["thresholds"]["fulfilled"]["met"] is True
    assert kwargs["result_summary"].startswith("Decision answers:")
    assert "Decision step completed" in result
    assert harness["decide_requests"][0].keep_alive == "30m"
    event_types = [e for e, _ in harness["events"]]
    assert ("decision_dispatched" in event_types) and ("decision_completed" in event_types)
    assert calls["usage"] and calls["usage"][0]["model"] == "nimble"


@pytest.mark.asyncio
async def test_decision_step_fail_task_on_threshold_miss(monkeypatch):
    harness = await _dispatch_harness(
        monkeypatch,
        _pending_task(decision_config=_noul_config(min_value=0.9)),
        responses=[_response({"fulfilled": NoulAnswer(noul=0.5)})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    assert calls["completed"] == []
    task_id, reason, result = calls["failed"][0]
    assert task_id == TASK_ID
    assert "Decision threshold policy failed" in reason
    assert "below min 0.9" in reason
    assert "probability 0.5" in result and "FAIL" in result
    assert harness["decide_requests"]  # usage happens regardless


@pytest.mark.asyncio
async def test_decision_step_needs_review_on_threshold_miss(monkeypatch):
    config = _noul_config(min_value=0.9)
    config["threshold_policy"]["on_fail"] = "needs_review"
    harness = await _dispatch_harness(
        monkeypatch,
        _pending_task(decision_config=config),
        responses=[_response({"fulfilled": NoulAnswer(noul=0.5)})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)

    calls = harness["calls"]
    assert calls["failed"] == []
    assert calls["completed"] == []
    task_id, reason, result = calls["needs_review"][0]
    assert task_id == TASK_ID
    assert "Decision threshold policy failed" in reason


@pytest.mark.asyncio
async def test_decision_step_without_policy_always_completes(monkeypatch):
    harness = await _dispatch_harness(
        monkeypatch,
        _pending_task(decision_config=_noul_config()),
        responses=[_response({"fulfilled": NoulAnswer(noul=0.2)})],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    calls = harness["calls"]
    assert calls["failed"] == []
    assert calls["completed"] and "thresholds" not in calls["completed"][0][2]["result_structured"]


@pytest.mark.asyncio
async def test_decision_step_retries_decision_error_then_fails(monkeypatch):
    harness = await _dispatch_harness(
        monkeypatch,
        _pending_task(decision_config=_noul_config()),
        responses=[DecisionError("cold load"), DecisionError("still down")],
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    calls = harness["calls"]
    assert len(calls["completed"]) == 0
    assert len(harness["decide_requests"]) == 2
    (task_id, reason, _result) = calls["failed"][0]
    assert task_id == TASK_ID
    assert "Decision step failed" in reason


@pytest.mark.asyncio
async def test_decision_step_denied_dispatch_never_claims(monkeypatch):
    harness = await _dispatch_harness(
        monkeypatch,
        _pending_task(decision_config=_noul_config()),
        resolve_error=ModelAccessDeniedError("no decision model"),
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    calls = harness["calls"]
    assert calls["completed"] == [] and calls["failed"]
    assert "Decision dispatch denied" in calls["failed"][0][1]
    assert calls["started"] == []
    assert [event_type for event_type, _ in harness["events"]] == ["dispatch_denied"]
    assert harness["decide_requests"] == []


@pytest.mark.asyncio
async def test_decision_step_claim_race_skips(monkeypatch):
    harness = await _dispatch_harness(
        monkeypatch, _pending_task(decision_config=_noul_config()), claim_value=None
    )
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    calls = harness["calls"]
    assert calls["completed"] == [] and calls["failed"] == []
    assert calls["started"] == []


@pytest.mark.asyncio
async def test_generic_task_path_never_enters_decision_branch(monkeypatch):
    """A decision-free task goes through chat model selection, never decide()."""
    task = _pending_task()
    task.pop("decision_config", None)
    task["agent_type"] = "code"
    task["model"] = "some-chat-model"

    async def _select(**_kwargs):
        harness["calls"]["select_model"].append(True)
        # Stop the regression here — the session pipeline has its own suite;
        # what matters is that the decision branch was never entered.
        raise ChatModelAccessDeniedError("denied for regression test")

    harness = await _dispatch_harness(monkeypatch, task)

    def _would_fail(**_kw):
        raise AssertionError("decision branch entered for a decision-free task")

    monkeypatch.setattr("daemon.dispatch.decision_step.resolve_user_decision_model", _would_fail)
    monkeypatch.setattr("daemon.daemon._select_model_for_user", _select)
    await harness["daemon"]._dispatch_tracked_tasks(max_tasks=1)
    assert harness["calls"]["select_model"]
    assert harness["decide_requests"] == []
    assert "Model dispatch denied" in harness["calls"]["failed"][0][1]


# ── REST surface ────────────────────────────────────────────────────────


async def _seed_decision_org(db_pool, prefix):
    """Org + user + active decision-step agent definition + seeded request."""
    from lucent.db import OrganizationRepository, UserRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.requests import RequestRepository

    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}u",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}@test.com",
        display_name="Decision User",
    )
    await DefinitionRepository(db_pool).create_agent(
        name="decision-step",
        description="System One decision step",
        content="Documentation for the decision step machinery.",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        status="active",
        owner_user_id=str(user["id"]),
    )
    req = await RequestRepository(db_pool).create_request(
        title=f"{prefix} request",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        description="rest surface fixture",
    )
    return org, user, req


def _override_user(app, user):
    from lucent.api.app import create_app
    from lucent.api.deps import CurrentUser, get_current_user

    app = app or create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user["role"],
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )
    return app


@pytest.mark.asyncio
async def test_rest_create_task_decision_pairing_422s(db_pool, monkeypatch):
    from lucent.api.app import create_app

    prefix = f"dec_rest_{str(uuid4())[:8]}"
    _org, user, req = await _seed_decision_org(db_pool, prefix)
    config = {"questions": [_noul()]}

    # The "no decision model available" probe is environment-dependent (other
    # suites seed shared decision rows with org owner NULL), so pin it empty
    # here; real availability resolution is covered at dispatch level.
    async def _empty_probe(*_args, **_kwargs):
        return []

    import lucent.db.models as db_models

    monkeypatch.setattr(db_models, "decision_models_available_for_user", _empty_probe)

    app = _override_user(create_app(), user)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        base = f"/api/requests/{str(req['id'])}/tasks"

        # decision_config on a non-decision step agent
        resp = await client.post(
            base,
            json={"title": "step", "agent_type": "code", "decision_config": config},
        )
        assert resp.status_code == 422
        assert "decision-step" in resp.json()["detail"]

        # decision-step agent without config
        resp = await client.post(base, json={"title": "step", "agent_type": "decision-step"})
        assert resp.status_code == 422
        assert "requires decision_config" in resp.json()["detail"]

        # output contract combined with decision_config
        resp = await client.post(
            base,
            json={
                "title": "step",
                "agent_type": "decision-step",
                "decision_config": config,
                "output_contract": {"json_schema": {"type": "object"}},
            },
        )
        assert resp.status_code == 422

        # malformed config
        resp = await client.post(
            base,
            json={
                "title": "step",
                "agent_type": "decision-step",
                "decision_config": {"questions": []},
            },
        )
        assert resp.status_code == 422

        # both model sources at once
        resp = await client.post(
            base,
            json={
                "title": "step",
                "agent_type": "decision-step",
                "model": "nimble",
                "decision_config": {**config, "model": "nimble"},
            },
        )
        assert resp.status_code == 422
        assert "not both" in resp.json()["detail"]

        # no decision model available to the user → 422
        resp = await client.post(
            base,
            json={"title": "step", "agent_type": "decision-step", "decision_config": config},
        )
        assert resp.status_code == 422
        assert "No enabled decision-class model" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rest_decision_task_with_explicit_model(db_pool, monkeypatch):
    from lucent import model_registry
    from lucent.api.app import create_app
    from lucent.db.models import ModelRepository
    from lucent.model_registry import ModelInfo

    prefix = f"dec_rest2_{str(uuid4())[:8]}"
    _org, user, req = await _seed_decision_org(db_pool, prefix)
    model_id = f"{prefix}-dec"
    config = {"questions": [_noul()], "model": model_id}
    # Registry mirror (conftest resets per test): expose the decision-class
    # model the same way a DB-loaded registry would, so strict validation
    # sees it without flipping the global validation mode.
    decision_model = ModelInfo(
        id=model_id,
        provider="ollama",
        name="Nimble decision",
        category="decision",
        model_class="decision",
        supports_tools=False,
        engine="langchain",
        enabled=True,
    )
    monkeypatch.setattr(model_registry, "_db_models", None)
    monkeypatch.setattr(model_registry, "_MODEL_BY_ID", {model_id: decision_model})
    # Org-shared decision row (owner NULL → accessible to org members).
    try:
        await ModelRepository(db_pool).create_model(
            model_id=model_id,
            provider="ollama",
            name="Nimble decision",
            category="decision",
            model_class="decision",
            engine="langchain",
            org_id=str(_org["id"]),
        )

        app = _override_user(create_app(), user)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/requests/{str(req['id'])}/tasks",
                json={
                    "title": "decision step",
                    "agent_type": "decision-step",
                    "decision_config": config,
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_type"] == "decision-step"
        stored = body["decision_config"]
        stored = json.loads(stored) if isinstance(stored, str) else stored
        assert stored["questions"][0]["name"] == "fulfilled"
    finally:
        await ModelRepository(db_pool).delete_model(model_id)
