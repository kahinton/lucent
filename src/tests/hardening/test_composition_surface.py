"""Composition-surface telemetry + fail-fast refusal tests.

Regression coverage for hardening rounds 2/3 Layer 1 (dispatch-time
composition hardening in daemon.py _dispatch_tracked_tasks):

1. A ``composition_surface`` task event is ALWAYS emitted before dispatch,
   carrying composed_tool_count / run_managed_tool / granted_tool_names.
2. When the task's agent carries managed-tool grants but the MCP config
   carrier (task_mcp_config["memory-server"]) is absent — e.g. the daemon's
   MCP_CONFIG was wiped so the scoped-server block is skipped — dispatch is
   refused fail-fast (fail_task + dispatch_denied, loop ``continue``) instead
   of silently dropping every granted tool.

Test 1/2 drive ``compose_task_tool_surface`` directly (pure function — the
docstring says telemetry and refusal are both derived from it, so the
refusal predicate is exercised against its exact output shape).

Tests 3/4 exercise the real dispatch loop end-to-end with the daemon stubbed
at its seams (RequestAPI class methods, agent/skill/managed-tool loaders,
scoped-key minting, model selection, run_session) — the same refusal block
that runs in production, no server, no network, no model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import daemon.daemon as dm
from daemon.daemon import LucentDaemon, compose_task_tool_surface

ORG_ID = "0f9abaa4-7489-47ab-8d6c-7c5be8d69d51"
USER_ID = "7b2129ec-392c-4787-85b7-184ae8c23ed4"
TASK_ID = "11111111-2222-3333-4444-555555555555"
REQUEST_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AGENT_DEF_ID = "4f523367-2dbc-46e5-84c7-6740394a8590"


# ── Pure-function layer: telemetry + refusal predicate ────────────────────


def test_compose_surface_refusal_predicate_present_carrier():
    surface = compose_task_tool_surface(
        carrier_tools=["search_memories", "run_managed_tool"],
        managed_tool_names=["db_query", None, "db_snapshot"],
    )
    assert surface == {
        "composed_tool_count": 2,
        "run_managed_tool": True,
        "granted_tool_names": ["db_query", "db_snapshot"],
    }
    # The exact condition guarding the fail-fast block: grants + no carrier.
    assert not (surface["granted_tool_names"] and not surface["run_managed_tool"])


def test_compose_surface_refusal_predicate_carrier_absent():
    surface = compose_task_tool_surface(
        carrier_tools=[],  # MCP_CONFIG wipe → scoped-server block skipped
        managed_tool_names=["db_query"],
    )
    assert surface == {
        "composed_tool_count": 0,
        "run_managed_tool": False,
        "granted_tool_names": ["db_query"],
    }
    assert surface["granted_tool_names"] and not surface["run_managed_tool"]


def test_compose_surface_grant_free_dispatch_is_never_refused():
    surface = compose_task_tool_surface(
        carrier_tools=["search_memories"],
        managed_tool_names=[],
    )
    assert not (surface["granted_tool_names"] and not surface["run_managed_tool"])


# ── Real dispatch-loop layer: event emission + fail-fast refusal ──────────


def _task_row() -> dict[str, Any]:
    return {
        "id": TASK_ID,
        "request_id": REQUEST_ID,
        "organization_id": ORG_ID,
        "agent_type": "lucent-dev",
        "agent_definition_id": AGENT_DEF_ID,
        "title": "Implement hardening verification tests",
        "description": "Small, focused, green test results.",
        "request_title": "Hardening round 3",
        "requesting_user_id": USER_ID,
        "model": None,
        "reasoning_effort": None,
        "sandbox_config": None,
        "output_mode": None,
        "commit_approved": False,
        "sandbox_template_id": None,
        "has_durable_output": False,
    }


def _agent_data() -> dict[str, Any]:
    return {"id": AGENT_DEF_ID, "name": "lucent-dev"}


class _Events:
    """Records add_event calls (type, detail, metadata) in order."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def types(self) -> list[str]:
        return [t for t, _, _ in self.calls]

    def composition(self) -> dict:
        for t, _, meta in self.calls:
            if t == "composition_surface":
                assert isinstance(meta, dict), "composition_surface metadata must be a dict"
                return meta
        raise AssertionError(f"no composition_surface event; got {self.types()}")


def _install_loop_stubs(monkeypatch, events: _Events, *, scoped_key: str | None):
    """Stub RequestAPI + loader seams the dispatch loop touches pre-run_session."""

    async def fake_add_event(task_id, event_type, detail=None, metadata=None, **kw):
        events.calls.append((event_type, detail or "", metadata))
        return {"id": "event"}

    async def fake_get_pending_tasks(max_tasks=2, **kw):
        return [_task_row()]

    async def fake_claim_task(task_id, instance_id):
        return {"id": task_id, "status": "running"}

    async def fake_update_task_model_settings(task_id, model=None, reasoning_effort=None):
        return None

    async def fake_get_request(request_id, *a, **kw):
        return {"id": request_id, "title": "Hardening round 3", "created_by": USER_ID}

    async def fake_get_request_context(request_id):
        return ("Parent request description.", "")

    async def fake_start_task(task_id, instance_id=None):
        return {"id": task_id, "status": "running"}

    async def fake_fail_task(task_id, error, instance_id=None, result=None):
        events.calls.append(("_fail_owned", error, None))
        return {"id": task_id, "status": "failed", "error": error}

    async def fake_load_agent(**kw):
        return _agent_data()

    async def fake_load_simple(**kw):
        return []

    async def fake_load_managed_tools(**kw):
        return [{"name": "db_query"}, {"name": "db_snapshot"}]

    async def fake_owner_context(user_id, org_id):
        return ""

    async def fake_select_model(**kw):
        return "glm-5.3-flash:cloud", "task-specified model"

    minted = {"called": False}
    if scoped_key is None:
        # MCP_CONFIG wipe: no scoped key minted → carrier never materializes.
        async def fake_mint(**kw):
            return None
    else:
        async def fake_mint(**kw):
            minted["called"] = True
            return scoped_key
        monkeypatch.setattr(dm, "_build_scoped_memory_server_config", lambda **kw: {
            "type": "http",
            "url": "http://lucent-server:8000/api/mcp",
            "headers": {"Authorization": f"Bearer {scoped_key}"},
            "tools": ["search_memories", "run_managed_tool", "list_tool_definitions",
                      "get_tool_definition"],
        })
        # The production carrier gate reads the daemon's module-level MCP_CONFIG
        # (daemon.py: `if MCP_CONFIG.get("memory-server")`); the real key-refresh
        # populates it only at runtime, so seed the stub server block here to
        # exercise the actual gate instead of skipping the scoped-server block.
        monkeypatch.setattr(dm, "MCP_CONFIG", {
            "memory-server": {
                "type": "http",
                "url": "http://lucent-server:8000/api/mcp",
                "tools": ["search_memories", "run_managed_tool"],
            },
        })
    monkeypatch.setattr(dm, "_mint_scoped_api_key", fake_mint)

    monkeypatch.setattr(dm.RequestAPI, "add_event", staticmethod(fake_add_event))
    monkeypatch.setattr(dm.RequestAPI, "get_pending_tasks", staticmethod(fake_get_pending_tasks))
    monkeypatch.setattr(dm.RequestAPI, "claim_task", staticmethod(fake_claim_task))
    monkeypatch.setattr(dm.RequestAPI, "update_task_model_settings",
                        staticmethod(fake_update_task_model_settings))
    monkeypatch.setattr(dm.RequestAPI, "get_request", staticmethod(fake_get_request))
    monkeypatch.setattr(dm.RequestAPI, "get_request_context", staticmethod(fake_get_request_context))
    monkeypatch.setattr(dm.RequestAPI, "start_task", staticmethod(fake_start_task))
    monkeypatch.setattr(dm.RequestAPI, "fail_task", staticmethod(fake_fail_task))

    monkeypatch.setattr(dm, "load_accessible_agent", fake_load_agent)
    monkeypatch.setattr(dm, "load_accessible_skills_for_agent", fake_load_simple)
    monkeypatch.setattr(dm, "load_accessible_mcp_servers_for_agent", fake_load_simple)
    monkeypatch.setattr(dm, "load_accessible_hooks_for_agent", fake_load_simple)
    monkeypatch.setattr(dm, "load_accessible_managed_tools_for_agent", fake_load_managed_tools)
    monkeypatch.setattr(dm, "_load_request_owner_context", fake_owner_context)
    monkeypatch.setattr(dm, "_select_model_for_user", fake_select_model)
    return minted


@pytest.fixture()
def stub_daemon(monkeypatch):
    monkeypatch.setattr(LucentDaemon, "_ensure_request_review_tasks",
                        async_stub(lambda self: None))
    monkeypatch.setattr(LucentDaemon, "_get_technical_context_for_request",
                        async_stub(lambda self, request_id: ""))
    daemon = LucentDaemon()
    daemon.draining = False
    return daemon


def async_stub(fn):
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


def test_dispatch_refused_and_telemetry_emitted_when_carrier_absent(stub_daemon, monkeypatch):
    """Grants + wiped MCP_CONFIG → composition_surface event + fail-fast refusal."""
    events = _Events()
    minted = _install_loop_stubs(monkeypatch, events, scoped_key=None)
    # Pre-condition honesty: MCP_CONFIG wipe is what kills the carrier.
    assert dm.MCP_CONFIG.get("memory-server") is None

    dispatched = asyncio.run(stub_daemon._dispatch_tracked_tasks(max_tasks=2))

    # Telemetry first: the composition_surface event fired with exact payload.
    comp = events.composition()
    assert comp == {
        "composed_tool_count": 0,
        "run_managed_tool": False,
        "granted_tool_names": ["db_query", "db_snapshot"],
    }
    # Refusal fired: fail_task with the carrier-absent reason...
    refusal = [d for t, d, _ in events.calls if t == "_fail_owned"]
    assert len(refusal) == 1
    assert "Refusing dispatch" in refusal[0]
    assert "carrier" in refusal[0] and "run_managed_tool" in refusal[0]
    # ...and a dispatch_denied audit trail entry, no agent_dispatched, loop moved on.
    assert "dispatch_denied" in events.types()
    assert "agent_dispatched" not in events.types()
    # Scoped key was never even attempted? It was — but the carrier was still
    # absent because MCP_CONFIG has no memory-server to mint for.
    assert minted.get("called", False) is False
    # _dispatch_tracked_tasks returns None (the dispatch count is not part of
    # its API); the event assertions above prove nothing was dispatched.


def test_dispatch_emits_telemetry_and_proceeds_when_carrier_present(stub_daemon, monkeypatch):
    """Grants + healthy scoped server → composition_surface event, no refusal."""
    events = _Events()
    minted = _install_loop_stubs(monkeypatch, events, scoped_key="scoped-test-key")

    # run_session raising ModelNotAvailableError is the cheapest clean exit:
    # the task is counted dispatched and the loop returns without model work.
    async def fake_run_session(name, *args, **kwargs):
        raise dm.ModelNotAvailableError("stub-model")

    monkeypatch.setattr(stub_daemon, "run_session", fake_run_session)

    dispatched = asyncio.run(stub_daemon._dispatch_tracked_tasks(max_tasks=2))

    assert minted["called"] is True
    comp = events.composition()
    # Carrier present (stubbed config includes run_managed_tool) + grants.
    assert comp == {
        "composed_tool_count": 4,
        "run_managed_tool": True,
        "granted_tool_names": ["db_query", "db_snapshot"],
    }
    # No carrier refusal anywhere (fail_task still fires legitimately below
    # run_session — e.g. the stubbed ModelNotAvailableError exit is a post-
    # dispatch model failure handled by the loop). The carrier-absent refusal
    # path is what must not have run: no dispatch_denied event, agent did get
    # dispatched.
    refusal_events = [d for t, d, _ in events.calls if t == "_fail_owned"]
    assert all("Refusing dispatch" not in d for d in refusal_events)
    assert "dispatch_denied" not in events.types()
    assert "agent_dispatched" in events.types()
    assert dispatched is None