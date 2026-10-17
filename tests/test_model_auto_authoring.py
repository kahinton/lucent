"""Auto model selection: sentinel authoring gates (Phase 2).

Every authoring surface accepting a task model — REST create/update/edit,
the web edit form, workflow action authoring, and the workflow schedule
model override — accepts ``model="auto"`` while the flag is on (stored
verbatim, validated at dispatch) and 422s while it is off, mirroring the
fusion authoring gates. Decision-step tasks never take the sentinel.
"""

import json
from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.auth_providers import CSRF_COOKIE_NAME, CSRF_FIELD_NAME, SESSION_COOKIE_NAME
from lucent.llm.model_auto import AUTO_DISABLED_MESSAGE, split_auto_sentinel


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_seeded_orgs(request, cleanup_test_organizations):
    """Delete the organizations this suite seeds inline, after each test.

    The seed helpers build org/user/request rows by hand rather than through a
    per-test prefix fixture, so without this their orgs outlive the test and
    pile up in the shared test database. Gated on the integration marker so the
    module's pure unit tests never open a connection.
    """
    is_integration = request.node.get_closest_marker("integration") is not None
    yield
    if is_integration:
        await cleanup_test_organizations(["auto_%"])


async def _set_flag(db_pool, org_id, user_id, on: bool) -> None:
    from lucent.db.runtime_settings import RuntimeSettingsRepository
    from lucent.settings import set_runtime_setting_cache

    await RuntimeSettingsRepository(db_pool).upsert_setting(
        organization_id=str(org_id),
        key="models.auto_selection_enabled",
        value=on,
        value_type="boolean",
        user_id=str(user_id),
    )
    set_runtime_setting_cache(org_id, "models.auto_selection_enabled", on)


async def _clear_flag(db_pool, org_id) -> None:
    from lucent.db.runtime_settings import RuntimeSettingsRepository
    from lucent.settings import clear_runtime_setting_cache

    await RuntimeSettingsRepository(db_pool).delete_setting(
        str(org_id), "models.auto_selection_enabled"
    )
    clear_runtime_setting_cache(org_id, "models.auto_selection_enabled")


async def _seed(db_pool, prefix: str):
    from tests.test_decision_tasks import _seed_decision_org

    return await _seed_decision_org(db_pool, prefix)


async def _approve_code_agent(db_pool, org, user) -> None:
    """MCP create_task defaults agent_type='code'; the seed only adds
    'decision-step' — approve a chat agent the user owns."""
    from lucent.db.definitions import DefinitionRepository

    await DefinitionRepository(db_pool).create_agent(
        name="code",
        description="Chat code agent",
        content="Documentation for the code agent.",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        status="active",
        owner_user_id=str(user["id"]),
    )


def _client(user) -> httpx.AsyncClient:
    from lucent.api.app import create_app
    from tests.test_decision_tasks import _override_user

    app = _override_user(create_app(), user)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── unit: split_auto_sentinel ───────────────────────────────────────────


def test_split_auto_sentinel_matrix():
    # Ordinary values never trip the gate, flag on or off.
    assert split_auto_sentinel(None, enabled=False) == (False, None)
    assert split_auto_sentinel("", enabled=False) == (False, None)
    assert split_auto_sentinel("gpt-5.6-terra", enabled=False) == (False, None)
    assert split_auto_sentinel("gpt-5.6-terra", enabled=True) == (False, None)
    # Sentinel + flag off → the single authoring rejection message.
    assert split_auto_sentinel("auto", enabled=False) == (
        True,
        AUTO_DISABLED_MESSAGE,
    )
    # Sentinel + flag on → accepted (validate/access/effort are skipped).
    assert split_auto_sentinel(" auto ", enabled=True) == (True, None)
    assert split_auto_sentinel("Auto", enabled=True) == (True, None)


# ── REST create_task ────────────────────────────────────────────────────


async def test_rest_create_task_auto_flag_off_422(db_pool):
    org, user, req = await _seed(db_pool, f"auto_off_{str(uuid4())[:8]}")
    async with _client(user) as client:
        resp = await client.post(
            f"/api/requests/{str(req['id'])}/tasks",
            json={"title": "auto step", "model": "auto"},
        )
    assert resp.status_code == 422
    assert "Auto model selection is disabled" in resp.json()["detail"]


async def test_rest_create_task_auto_stored_when_enabled(db_pool):
    org, user, req = await _seed(db_pool, f"auto_on_{str(uuid4())[:8]}")
    try:
        await _set_flag(db_pool, str(org["id"]), user["id"], True)
        async with _client(user) as client:
            resp = await client.post(
                f"/api/requests/{str(req['id'])}/tasks",
                json={"title": "auto step", "model": "auto"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "auto"
    finally:
        await _clear_flag(db_pool, org["id"])


async def test_rest_create_task_auto_rejected_on_decision_step(db_pool):
    org, user, req = await _seed(db_pool, f"auto_dec_{str(uuid4())[:8]}")
    try:
        await _set_flag(db_pool, str(org["id"]), user["id"], True)
        async with _client(user) as client:
            resp = await client.post(
                f"/api/requests/{str(req['id'])}/tasks",
                json={
                    "title": "decide",
                    "model": "auto",
                    "decision_config": {
                        "questions": [
                            {"name": "ok", "type": "noul", "instructions": "Complete?"}
                        ],
                        "threshold_policy": {"questions": {"ok": {"min": 0.7}}},
                    },
                },
            )
        assert resp.status_code == 422
        assert "decision-step" in resp.json()["detail"]
    finally:
        await _clear_flag(db_pool, org["id"])


# ── REST update_task_model ──────────────────────────────────────────────


async def _tracked_task(db_pool, prefix: str):
    """Seeded org + request + a pending task to poke via REST."""
    org, user, req = await _seed(db_pool, prefix)
    from lucent.db.requests import RequestRepository

    repo = RequestRepository(db_pool)
    task = await repo.create_task(
        request_id=str(req["id"]),
        title="model probe",
        org_id=str(org["id"]),
        agent_type="code",
        requesting_user_id=str(user["id"]),
    )
    return org, user, req, task


async def test_rest_update_task_model_auto_matrix(db_pool):
    org, user, req, task = await _tracked_task(db_pool, f"auto_upd_{str(uuid4())[:8]}")
    url = f"/api/requests/tasks/{task['id']}/model"
    try:
        # Flag off — the sentinel is refused.
        async with _client(user) as client:
            resp = await client.post(url, json={"model": "auto", "reasoning_effort": ""})
        assert resp.status_code == 422
        assert "Auto model selection is disabled" in resp.json()["detail"]

        # Flag on — stored verbatim.
        await _set_flag(db_pool, str(org["id"]), user["id"], True)
        async with _client(user) as client:
            resp = await client.post(url, json={"model": "auto", "reasoning_effort": ""})
        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "auto"
    finally:
        await _clear_flag(db_pool, org["id"])


# ── MCP create_task ─────────────────────────────────────────────────────


def _mcp():
    from mcp.server import MCPServer as FastMCP

    from lucent.tools.requests import register_request_tools

    mcp = FastMCP("test")
    register_request_tools(mcp)
    return mcp


async def _call_mcp(mcp, args: dict) -> dict:
    """create_task MCP tool reads the current-user auth context."""
    from lucent.auth import set_current_user

    set_current_user(args.pop("_user"))
    try:
        result = await mcp._tool_manager.call_tool("create_task", args, None)
    finally:
        set_current_user(None)
    return json.loads(result)


def _auto_user(user) -> dict:
    return {
        "id": str(user["id"]),
        "organization_id": str(user["organization_id"]),
        "role": "owner",
        "display_name": "Auto Probe",
        "email": f"auto-probe-{str(user['id'])[:8]}@test.com",
    }


async def test_mcp_create_task_auto_matrix(db_pool):
    mcp = _mcp()
    org, user, req = await _seed(db_pool, f"auto_mcp_{str(uuid4())[:8]}")
    await _approve_code_agent(db_pool, org, user)
    base = {"request_id": str(req["id"]), "title": "mcp probe", "model": "auto"}
    try:
        # Flag off — refused with the same message as REST.
        payload = await _call_mcp(mcp, {**base, "_user": _auto_user(user)})
        assert "error" in payload
        assert "Auto model selection is disabled" in payload["error"]

        # Flag on — stored verbatim, resolved at dispatch.
        await _set_flag(db_pool, str(org["id"]), user["id"], True)
        payload = await _call_mcp(mcp, {**base, "_user": _auto_user(user)})
        assert "error" not in payload, payload
        assert payload["model"] == "auto"
    finally:
        await _clear_flag(db_pool, org["id"])


async def test_mcp_create_task_auto_decision_step_rejected(db_pool):
    mcp = _mcp()
    org, user, req = await _seed(db_pool, f"auto_mcpd_{str(uuid4())[:8]}")
    try:
        await _set_flag(db_pool, str(org["id"]), user["id"], True)
        payload = await _call_mcp(
            mcp,
            {
                "request_id": str(req["id"]),
                "title": "mcp decide",
                "agent_type": "decision-step",
                "model": "auto",
                "decision_config": {
                    "questions": [
                        {"name": "ok", "type": "noul", "instructions": "Done?"}
                    ],
                    "threshold_policy": {"questions": {"ok": {"min": 0.7}}},
                },
                "_user": _auto_user(user),
            },
        )
        assert "error" in payload
        assert "decision-step" in payload["error"]
    finally:
        await _clear_flag(db_pool, org["id"])


# ── Web form: edit pending task ─────────────────────────────────────────


def _web_client(user, session_token: str) -> httpx.AsyncClient:
    from lucent.api.app import create_app

    csrf_token = "test-csrf-auto-123"
    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: session_token, CSRF_COOKIE_NAME: csrf_token},
    )
    client._csrf_token = csrf_token
    return client


def _csrf_form(client, extra: dict | None = None) -> dict:
    data = {CSRF_FIELD_NAME: client._csrf_token}
    if extra:
        data.update(extra)
    return data


async def test_web_form_edit_task_auto_matrix(db_pool):
    from lucent.auth_providers import create_session, set_user_password
    from lucent.db import OrganizationRepository, UserRepository
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.requests import RequestRepository

    prefix = f"auto_web_{str(uuid4())[:8]}"
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}u",
        provider="basic",
        organization_id=org["id"],
        email=f"{prefix}@test.com",
        display_name="Auto Web User",
    )
    await set_user_password(db_pool, user["id"], "TestPass1")
    session_token = await create_session(db_pool, user["id"])
    await DefinitionRepository(db_pool).create_agent(
        name="code",
        description="Chat code agent",
        content="Documentation for the code agent.",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        status="active",
        owner_user_id=str(user["id"]),
    )
    req = await RequestRepository(db_pool).create_request(
        title=f"{prefix} request",
        org_id=str(org["id"]),
        created_by=str(user["id"]),
        description="web surface fixture",
    )
    task = await RequestRepository(db_pool).create_task(
        request_id=str(req["id"]),
        title="web probe",
        org_id=str(org["id"]),
        agent_type="code",
        requesting_user_id=str(user["id"]),
    )
    url = f"/requests/tasks/{task['id']}/edit"
    try:
        # Flag off — the sentinel is refused.
        async with _web_client(user, session_token) as client:
            resp = await client.post(
                url, data=_csrf_form(client, {"model": "auto"})
            )
        assert resp.status_code == 422, resp.text
        assert "Auto model selection is disabled" in resp.text

        # Flag on — stored verbatim.
        await _set_flag(db_pool, str(org["id"]), user["id"], True)
        async with _web_client(user, session_token) as client:
            resp = await client.post(
                url, data=_csrf_form(client, {"model": "auto"})
            )
        assert resp.status_code in (200, 302, 303), resp.text
        stored = await RequestRepository(db_pool).get_task(
            str(task["id"]), org_id=str(org["id"])
        )
        assert stored["model"] == "auto"
    finally:
        await _clear_flag(db_pool, org["id"])
