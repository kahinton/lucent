"""The controller is a work distributor across organizations, not a tenant.

A worker is single-tenant by construction — the launcher pins it to one org. The
always-on controller is the opposite: it supervises every org that has work and
spawns a correctly-bound worker per org. Conflating the two made the controller
refuse to resolve any org when more than one existed, which broke its API key
provisioning, its heartbeats, and therefore dispatch for *every* org.
"""

import pytest

from daemon.api import organization
from daemon.runtime.module_proxy import runtime

ORG_A = ("aaaaaaaa-1111-4111-8111-111111111111", "Alpha Org")
ORG_B = ("bbbbbbbb-2222-4222-8222-222222222222", "Beta Org")


class _Repo:
    """Stands in for DaemonRepository's org reads."""

    def __init__(self, rows):
        self._rows = rows

    async def list_organizations_except(self, _system_org_name):
        return [{"id": org_id, "name": name} for org_id, name in self._rows]

    async def resolve_organization(self, value):
        for org_id, name in self._rows:
            if value in (org_id, name):
                return {"id": org_id, "name": name}
        return None


@pytest.fixture(autouse=True)
def _reset_resolution(monkeypatch):
    """Each test starts from a cold process and an unbound daemon.

    State is assigned directly rather than via ``monkeypatch.setattr``:
    ``runtime`` is a proxy whose ``__setattr__`` writes through to the live
    daemon module but which defines no ``__delattr__``, so monkeypatch's
    "attribute did not exist, delete it on teardown" path raises there.
    """
    previous = {
        name: getattr(runtime, name, None)
        for name in ("_resolved_daemon_org", "_resolved_service_org")
    }
    runtime._resolved_daemon_org = None
    runtime._resolved_service_org = None
    monkeypatch.setattr(runtime, "DAEMON_ORG", "")
    monkeypatch.setattr(runtime, "DAEMON_WORKER", False)
    monkeypatch.setattr(organization, "DaemonRepository", _Repo)
    yield
    for name, value in previous.items():
        setattr(runtime, name, value)


@pytest.mark.asyncio
async def test_controller_resolves_no_single_tenant_with_many_orgs(monkeypatch):
    """A multi-org controller must not be an error state."""
    monkeypatch.setattr(
        organization, "DaemonRepository", lambda _conn: _Repo([ORG_A, ORG_B])
    )

    assert await organization.resolve_daemon_org(object()) is None
    # And it must be cached as "resolved to no single tenant", not retried.
    assert runtime._resolved_daemon_org is None


@pytest.mark.asyncio
async def test_controller_still_provisions_a_service_identity(monkeypatch):
    """No single tenant does not mean no identity: it must be able to auth."""
    monkeypatch.setattr(
        organization, "DaemonRepository", lambda _conn: _Repo([ORG_A, ORG_B])
    )

    identity = await organization.resolve_service_org(object())

    # Deterministic: first org by creation, so restarts reuse the same identity.
    assert identity == ORG_A


@pytest.mark.asyncio
async def test_service_identity_is_stable_across_calls(monkeypatch):
    monkeypatch.setattr(
        organization, "DaemonRepository", lambda _conn: _Repo([ORG_A, ORG_B])
    )

    first = await organization.resolve_service_org(object())
    second = await organization.resolve_service_org(object())

    assert first == second == ORG_A


@pytest.mark.asyncio
async def test_worker_without_a_pin_refuses_to_guess(monkeypatch):
    """A worker must never pick an org itself — that would be the wrong tenant."""
    monkeypatch.setattr(runtime, "DAEMON_WORKER", True, raising=False)
    monkeypatch.setattr(
        organization, "DaemonRepository", lambda _conn: _Repo([ORG_A])
    )

    assert await organization.resolve_daemon_org(object()) is None


@pytest.mark.asyncio
async def test_worker_uses_its_pin(monkeypatch):
    monkeypatch.setattr(runtime, "DAEMON_WORKER", True, raising=False)
    monkeypatch.setattr(runtime, "DAEMON_ORG", ORG_B[1], raising=False)
    monkeypatch.setattr(
        organization, "DaemonRepository", lambda _conn: _Repo([ORG_A, ORG_B])
    )

    assert await organization.resolve_daemon_org(object()) == ORG_B


@pytest.mark.asyncio
async def test_single_org_deployment_auto_binds(monkeypatch):
    """The simple local case keeps working without any configuration."""
    monkeypatch.setattr(
        organization, "DaemonRepository", lambda _conn: _Repo([ORG_A])
    )

    assert await organization.resolve_daemon_org(object()) == ORG_A


@pytest.mark.asyncio
async def test_no_orgs_at_all_resolves_to_none(monkeypatch):
    monkeypatch.setattr(organization, "DaemonRepository", lambda _conn: _Repo([]))

    assert await organization.resolve_daemon_org(object()) is None
    assert await organization.resolve_service_org(object()) is None
