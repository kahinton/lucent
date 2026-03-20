"""Request-scoped dispatch ACL tests for daemon resource selection."""

from uuid import UUID

import pytest

from daemon.daemon import LucentDaemon, load_accessible_agent
from lucent.db.definitions import DefinitionRepository
from lucent.db.user import UserRepository


@pytest.mark.asyncio
async def test_load_accessible_agent_filters_by_requesting_user(
    db_pool, test_organization, test_user, clean_test_data
):
    org_id = str(test_organization["id"])
    user_repo = UserRepository(db_pool)
    other_user = await user_repo.create(
        external_id=f"{clean_test_data}other-user",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}other@test.com",
        display_name="Other User",
    )

    repo = DefinitionRepository(db_pool)
    agent = await repo.create_agent(
        name=f"{clean_test_data}code",
        description="Owned agent",
        content="agent content",
        org_id=org_id,
        created_by=str(test_user["id"]),
        status="active",
        owner_user_id=str(test_user["id"]),
    )

    accessible = await load_accessible_agent(
        org_id=org_id,
        requester_user_id=str(test_user["id"]),
        agent_type=f"{clean_test_data}code",
    )
    blocked = await load_accessible_agent(
        org_id=org_id,
        requester_user_id=str(other_user["id"]),
        agent_type=f"{clean_test_data}code",
    )

    assert accessible is not None
    assert accessible["id"] == agent["id"]
    assert blocked is None


@pytest.mark.asyncio
async def test_dispatch_fails_gracefully_when_no_accessible_agent(monkeypatch):
    daemon = LucentDaemon()
    failed: list[str] = []
    events: list[tuple[str, str, str | None]] = []
    starts: list[str] = []

    async def _pending():
        return [
            {
                "id": UUID("11111111-1111-1111-1111-111111111111"),
                "request_id": UUID("22222222-2222-2222-2222-222222222222"),
                "organization_id": UUID("33333333-3333-3333-3333-333333333333"),
                "title": "Restricted task",
                "description": "Should fail cleanly",
                "agent_type": "code",
                "requesting_user_id": UUID("44444444-4444-4444-4444-444444444444"),
            }
        ]

    async def _claim(task_id, _instance_id):
        return {"id": task_id}

    async def _update_model(_task_id, _model):
        return {"ok": True}

    async def _role(_user_id, _org_id):
        return "member"

    async def _ctx(_request_id):
        return "", ""

    async def _fail(task_id, error):
        failed.append(error)
        return {"id": task_id, "error": error}

    async def _event(task_id, event_type, detail=None, metadata=None):
        events.append((task_id, event_type, detail))
        return {"id": task_id, "event_type": event_type, "metadata": metadata}

    async def _start(task_id):
        starts.append(task_id)
        return {"id": task_id}

    async def _no_agent(**_kwargs):
        return None

    monkeypatch.setattr("daemon.daemon.RequestAPI.get_pending_tasks", _pending)
    monkeypatch.setattr("daemon.daemon.RequestAPI.claim_task", _claim)
    monkeypatch.setattr("daemon.daemon.RequestAPI.update_task_model", _update_model)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_user_role", _role)
    monkeypatch.setattr("daemon.daemon.RequestAPI.get_request_context", _ctx)
    monkeypatch.setattr("daemon.daemon.RequestAPI.fail_task", _fail)
    monkeypatch.setattr("daemon.daemon.RequestAPI.add_event", _event)
    monkeypatch.setattr("daemon.daemon.RequestAPI.start_task", _start)
    monkeypatch.setattr("daemon.daemon.load_accessible_agent", _no_agent)

    await daemon._dispatch_tracked_tasks(max_tasks=1)

    assert starts == []
    assert failed
    assert "No accessible approved agent definition" in failed[0]
    assert any(event_type == "agent_not_found" for _, event_type, _ in events)
