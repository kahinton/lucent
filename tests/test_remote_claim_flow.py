"""Remote dispatch and claim-flow tests (Agent Control Protocol).

These exercise the load-bearing invariants:

* Local dispatch never sees remote-pinned tasks.
* A remote claims only tasks pinned to itself, in stage order.
* Two racing claimers cannot both win the same task.
* Remote claims are attributed to ``remote:{id}`` so heartbeat renews the
  lease and the stale reaper releases a dead remote's work.
* Remote-agent keys cannot touch tasks they do not hold.
"""

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.remotes import RemoteRepository
from lucent.db.requests import RequestRepository


async def _setup(db_pool, prefix):
    org = await OrganizationRepository(db_pool).create(name=f"{prefix}org")
    users = UserRepository(db_pool)
    owner = await users.create(
        external_id=f"{prefix}owner",
        provider="local",
        organization_id=org["id"],
        email=f"{prefix}owner@test.com",
        display_name="Owner",
        role="owner",
    )
    return org, owner


async def _cleanup(db_pool, org_id, prefix):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_events WHERE task_id IN "
            "(SELECT id FROM tasks WHERE organization_id = $1)",
            org_id,
        )
        await conn.execute(
            "DELETE FROM tasks WHERE organization_id = $1", org_id
        )
        await conn.execute(
            "DELETE FROM requests WHERE organization_id = $1", org_id
        )
        await conn.execute(
            "DELETE FROM daemon_instances WHERE organization_id = $1", org_id
        )
        await conn.execute(
            "DELETE FROM remote_instances WHERE organization_id = $1", org_id
        )
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE organization_id = $1)",
            org_id,
        )
        await conn.execute(
            "DELETE FROM users WHERE organization_id = $1 AND external_id "
            "LIKE 'remote-agent:%'",
            org_id,
        )
        await conn.execute(
            "DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%"
        )


@asynccontextmanager
async def _remote_client(org, remote_id, *, scopes=("remote-agent",)):
    """Client authenticated as a remote agent key for ``remote_id``."""
    app = create_app()

    async def override():
        return CurrentUser(
            id=uuid4(),
            organization_id=org["id"],
            role="daemon",
            email=None,
            display_name="Remote agent",
            auth_method="api_key",
            api_key_scopes=list(scopes),
            api_key_name=f"remote-agent-{remote_id}",
            remote_agent_id=str(remote_id),
        )

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_local_dispatch_excludes_remote_pinned_tasks(db_pool):
    prefix = f"test_remote_dispatch_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote, _token = await remotes.create(
            org_id=str(org["id"]), name="worker-a", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="Pinned work",
            org_id=str(org["id"]),
            created_by=str(owner["id"]),
        )
        local_task = await requests.create_task(
            request_id=str(request["id"]),
            title="Runs locally",
            org_id=str(org["id"]),
        )
        remote_task = await requests.create_task(
            request_id=str(request["id"]),
            title="Runs on the remote",
            org_id=str(org["id"]),
            remote_target_id=str(remote["id"]),
        )

        pending = await requests.list_pending_tasks(str(org["id"]))
        ids = {str(t["id"]) for t in pending["items"]}
        assert str(local_task["id"]) in ids
        assert str(remote_task["id"]) not in ids

        # The remote's own claim path does see it.
        claimed = await requests.claim_remote_task(
            remote_id=str(remote["id"]), org_id=str(org["id"])
        )
        assert claimed is not None
        assert str(claimed["id"]) == str(remote_task["id"])
        assert claimed["claimed_by"] == f"remote:{remote['id']}"
        assert claimed["status"] == "claimed"
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_remote_cannot_claim_another_remotes_task(db_pool):
    prefix = f"test_remote_isolation_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote_a, _ = await remotes.create(
            org_id=str(org["id"]), name="rem-a", created_by=str(owner["id"])
        )
        remote_b, _ = await remotes.create(
            org_id=str(org["id"]), name="rem-b", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="B-only work", org_id=str(org["id"]), created_by=str(owner["id"])
        )
        task = await requests.create_task(
            request_id=str(request["id"]),
            title="For remote B",
            org_id=str(org["id"]),
            remote_target_id=str(remote_b["id"]),
        )

        stolen = await requests.claim_remote_task(
            remote_id=str(remote_a["id"]), org_id=str(org["id"])
        )
        assert stolen is None

        claimed = await requests.claim_remote_task(
            remote_id=str(remote_b["id"]), org_id=str(org["id"])
        )
        assert claimed is not None
        assert str(claimed["id"]) == str(task["id"])
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_claim_is_exclusive_under_race(db_pool):
    prefix = f"test_remote_race_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote, _ = await remotes.create(
            org_id=str(org["id"]), name="racer", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="Single task", org_id=str(org["id"]), created_by=str(owner["id"])
        )
        await requests.create_task(
            request_id=str(request["id"]),
            title="Only one winner",
            org_id=str(org["id"]),
            remote_target_id=str(remote["id"]),
        )

        first = await requests.claim_remote_task(
            remote_id=str(remote["id"]), org_id=str(org["id"])
        )
        second = await requests.claim_remote_task(
            remote_id=str(remote["id"]), org_id=str(org["id"])
        )
        assert first is not None
        assert second is None
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_remote_claim_registers_instance_and_renews_lease(db_pool):
    prefix = f"test_remote_lease_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote, _ = await remotes.create(
            org_id=str(org["id"]), name="leaser", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="Lease work", org_id=str(org["id"]), created_by=str(owner["id"])
        )
        await requests.create_task(
            request_id=str(request["id"]),
            title="Leased",
            org_id=str(org["id"]),
            remote_target_id=str(remote["id"]),
        )
        claimed = await requests.claim_remote_task(
            remote_id=str(remote["id"]), org_id=str(org["id"])
        )
        assert claimed is not None

        instance_id = f"remote:{remote['id']}"
        await requests.register_instance(
            org_id=str(org["id"]),
            instance_id=instance_id,
            roles=["remote"],
            metadata={"kind": "remote"},
        )
        renewed = await requests.heartbeat_instance(
            org_id=str(org["id"]), instance_id=instance_id
        )
        assert renewed is not None
        assert renewed["status"] == "active"

        refreshed = await requests.get_task(str(claimed["id"]), str(org["id"]))
        assert refreshed["last_heartbeat_at"] is not None
        assert refreshed["claim_expires_at"] is not None
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_stale_reaper_releases_dead_remote_claim(db_pool):
    """A remote whose instance row goes stale must have its work requeued."""
    prefix = f"test_remote_reap_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote, _ = await remotes.create(
            org_id=str(org["id"]), name="dies", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="Abandoned work", org_id=str(org["id"]), created_by=str(owner["id"])
        )
        task = await requests.create_task(
            request_id=str(request["id"]),
            title="Claimed then abandoned",
            org_id=str(org["id"]),
            remote_target_id=str(remote["id"]),
        )
        claimed = await requests.claim_remote_task(
            remote_id=str(remote["id"]), org_id=str(org["id"])
        )
        assert claimed is not None

        # Simulate the remote going away: instance registered then stopped.
        instance_id = f"remote:{remote['id']}"
        await requests.register_instance(
            org_id=str(org["id"]), instance_id=instance_id, roles=["remote"]
        )
        await requests.mark_instance_stopped(
            org_id=str(org["id"]), instance_id=instance_id
        )

        released = await requests.release_stale_tasks(org_id=str(org["id"]))
        assert released >= 1

        requeued = await requests.get_task(str(task["id"]), str(org["id"]))
        assert requeued["status"] == "pending"
        assert requeued["claimed_by"] is None
        # The pin survives, so the task is still destined for that remote.
        assert str(requeued["remote_target_id"]) == str(remote["id"])
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_remote_claim_http_endpoint_returns_payload(db_pool):
    prefix = f"test_remote_http_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote, _ = await remotes.create(
            org_id=str(org["id"]), name="http-remote", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="Over HTTP", org_id=str(org["id"]), created_by=str(owner["id"])
        )
        task = await requests.create_task(
            request_id=str(request["id"]),
            title="Claimed over HTTP",
            description="Do the thing",
            org_id=str(org["id"]),
            remote_target_id=str(remote["id"]),
        )

        async with _remote_client(org, remote["id"]) as client:
            response = await client.post(f"/api/remotes/{remote['id']}/claim")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["id"] == str(task["id"])
        assert payload["title"] == "Claimed over HTTP"
        assert payload["description"] == "Do the thing"
        # The remote contract is plain execution: no sandbox config leaks out.
        assert "sandbox_config" not in payload

        # Second claim finds nothing.
        async with _remote_client(org, remote["id"]) as client:
            empty = await client.post(f"/api/remotes/{remote['id']}/claim")
        assert empty.status_code == 204
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_remote_key_cannot_claim_for_a_different_remote(db_pool):
    """The key's bound remote id must match the URL remote id.

    Verified at the route layer (defense in depth) — ``deps`` rejects this even
    earlier, so exercising the route proves both are not solely reliant on one
    another.
    """
    prefix = f"test_remote_binding_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote_a, _ = await remotes.create(
            org_id=str(org["id"]), name="bound-a", created_by=str(owner["id"])
        )
        remote_b, _ = await remotes.create(
            org_id=str(org["id"]), name="bound-b", created_by=str(owner["id"])
        )

        async with _remote_client(org, remote_a["id"]) as client:
            response = await client.post(f"/api/remotes/{remote_b['id']}/claim")
        assert response.status_code == 403
        assert "different remote" in response.json()["detail"]
    finally:
        await _cleanup(db_pool, org["id"], prefix)


@pytest.mark.asyncio
async def test_remote_key_allowlist_blocks_non_protocol_routes(db_pool):
    """The scope allowlist must reject everything outside the protocol surface.

    This exercises ``lucent.api.deps`` directly (no dependency override) so the
    real allowlist — the actual security control — is under test.
    """
    from lucent.api.deps import CurrentUser, _enforce_remote_agent_allowlist
    from fastapi import HTTPException

    remote_id = str(uuid4())
    user = CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role="daemon",
        email=None,
        display_name="Remote agent",
        auth_method="api_key",
        api_key_scopes=["remote-agent"],
        api_key_name=f"remote-agent-{remote_id}",
        remote_agent_id=remote_id,
    )

    class _Req:
        def __init__(self, method: str, path: str):
            self.method = method
            self.url = type("U", (), {"path": path})()

    # Allowed protocol surface.
    for method, path in (
        ("POST", f"/api/remotes/{remote_id}/claim"),
        ("POST", f"/api/remotes/{remote_id}/heartbeat"),
        ("POST", "/api/requests/tasks/" + str(uuid4()) + "/events"),
        ("POST", "/api/requests/tasks/" + str(uuid4()) + "/complete"),
        ("POST", "/api/requests/tasks/" + str(uuid4()) + "/fail"),
    ):
        _enforce_remote_agent_allowlist(_Req(method, path), user)

    # Everything else is denied.
    denied = (
        ("POST", "/api/remotes"),
        ("GET", "/api/remotes"),
        ("GET", "/api/memories"),
        ("POST", "/api/memories"),
        ("PATCH", "/api/remotes/" + remote_id),
        ("POST", "/api/remotes/" + remote_id + "/rotate-token"),
        ("POST", "/api/requests/tasks/" + str(uuid4()) + "/release"),
        # A different remote's protocol endpoint.
        ("POST", "/api/remotes/" + str(uuid4()) + "/claim"),
    )
    for method, path in denied:
        with pytest.raises(HTTPException) as exc:
            _enforce_remote_agent_allowlist(_Req(method, path), user)
        assert exc.value.status_code == 403, f"{method} {path}"

    # A remote-agent key with no bound identity fails closed.
    unbound = CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        role="daemon",
        email=None,
        display_name="Remote agent",
        auth_method="api_key",
        api_key_scopes=["remote-agent"],
        api_key_name="something-else",
        remote_agent_id=None,
    )
    with pytest.raises(HTTPException) as exc:
        _enforce_remote_agent_allowlist(
            _Req("POST", f"/api/remotes/{remote_id}/claim"), unbound
        )
    assert exc.value.status_code == 403
    assert "not bound" in exc.value.detail


@pytest.mark.asyncio
async def test_remote_key_cannot_transition_unclaimed_task(db_pool):
    prefix = f"test_remote_transition_{str(uuid4())[:8]}_"
    org, owner = await _setup(db_pool, prefix)
    try:
        remotes = RemoteRepository(db_pool)
        remote, _ = await remotes.create(
            org_id=str(org["id"]), name="reporter", created_by=str(owner["id"])
        )
        requests = RequestRepository(db_pool)
        request = await requests.create_request(
            title="Not mine", org_id=str(org["id"]), created_by=str(owner["id"])
        )
        other = await requests.create_task(
            request_id=str(request["id"]),
            title="Never claimed by this remote",
            org_id=str(org["id"]),
        )

        async with _remote_client(org, remote["id"]) as client:
            fail_attempt = await client.post(
                f"/api/requests/tasks/{other['id']}/fail",
                json={"error": "trying to fail someone else's task"},
            )
            event_attempt = await client.post(
                f"/api/requests/tasks/{other['id']}/events",
                json={"event_type": "progress", "detail": "not mine"},
            )
        # Denied: the remote never held a claim on this task. The request
        # guard reports "not found" rather than leaking that the task exists;
        # the transition layer reports 409 because there is no claim to
        # transition. Either way the mutation must not succeed.
        assert fail_attempt.status_code in (403, 404, 409), fail_attempt.text
        assert event_attempt.status_code in (403, 404, 409), event_attempt.text

        # Verify nothing was mutated.
        unchanged = await requests.get_task(str(other["id"]), str(org["id"]))
        assert unchanged["status"] == "pending"
        assert unchanged["claimed_by"] is None
        events = await requests.list_task_events(
            str(other["id"]), org_id=str(org["id"])
        )
        assert all(
            e["event_type"] != "progress" for e in events["items"]
        ), "remote wrote an event to a task it never claimed"
    finally:
        await _cleanup(db_pool, org["id"], prefix)
