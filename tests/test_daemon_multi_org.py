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

    ``_resolved_*`` are assigned directly because they may not exist on the
    module at all: ``monkeypatch.setattr`` cannot create-then-remove an
    attribute here, and these two are process caches rather than configuration.
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


@pytest.mark.asyncio
async def test_unbound_controller_is_not_logged_as_an_error(monkeypatch):
    """A multi-org controller having no single tenant is not a failure.

    ``_get_daemon_org_id`` is polled once per scheduler interval (60s by
    default) by the request-decomposition loop. It used to log
    ``Daemon org resolution returned none`` at ERROR every time, so a healthy
    multi-org controller produced a permanent once-a-minute error and buried
    real failures. ``resolve_daemon_org`` already reports the genuine
    misconfiguration (a worker started without its pin) itself.
    """
    from daemon.runtime.loops import RuntimeLoopsMixin
    from daemon.runtime.module_proxy import runtime as daemon_runtime

    logged: list[tuple[str, str]] = []

    def _capture(message, level="INFO"):
        logged.append((str(message), level))

    class _Conn:
        async def close(self):
            return None

    async def _connect(*_args, **_kwargs):
        return _Conn()

    async def _no_binding(_conn):
        return None

    monkeypatch.setattr("daemon.db_scope.connect_scoped", _connect)
    monkeypatch.setattr(daemon_runtime, "_resolve_daemon_org", _no_binding)
    monkeypatch.setattr(daemon_runtime, "log", _capture, raising=False)
    monkeypatch.setattr(daemon_runtime, "DATABASE_URL", "postgresql://unused", raising=False)

    loops = RuntimeLoopsMixin()
    assert loops._get_daemon_org_id

    assert await loops._get_daemon_org_id() is None

    errors = [m for m, level in logged if level == "ERROR"]
    assert not errors, f"an unbound controller must not log ERROR: {errors}"
