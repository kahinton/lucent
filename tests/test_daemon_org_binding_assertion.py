"""A worker refuses to run a task that is not its own org's.

Defense in depth behind the server-side org-scoped claim: an ephemeral worker's
org binding is injected by the controller, so a mismatch here means the binding
and the task disagree and the worker must not proceed.
"""

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import LucentDaemon


def _daemon(bound_org):
    daemon = LucentDaemon.__new__(LucentDaemon)
    daemon.instance_id = "worker-1"

    async def bound():
        return bound_org

    daemon._get_daemon_org_id = bound  # type: ignore[method-assign]
    return daemon


@pytest.mark.asyncio
async def test_matching_org_is_allowed():
    daemon = _daemon("org-1")
    claimed = {"id": "task-1", "organization_id": "org-1"}

    assert await daemon._task_matches_bound_org(claimed) is True


@pytest.mark.asyncio
async def test_missing_org_falls_back_to_server_side_scope():
    daemon = _daemon("org-1")

    assert await daemon._task_matches_bound_org({"id": "task-1"}) is True


@pytest.mark.asyncio
async def test_mismatched_org_releases_and_refuses(monkeypatch):
    released = []

    async def release_task(task_id, instance_id=None):
        released.append((task_id, instance_id))
        return {}

    monkeypatch.setattr(daemon_module.RequestAPI, "release_task", release_task)
    daemon = _daemon("org-1")
    claimed = {"id": "task-9", "organization_id": "org-2"}

    assert await daemon._task_matches_bound_org(claimed) is False
    assert released == [("task-9", "worker-1")]
