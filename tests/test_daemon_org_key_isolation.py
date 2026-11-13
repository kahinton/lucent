"""Per-org daemon credentials: no key may act for more than one organization.

The daemon API key is org-scoped server-side (``api_keys.organization_id``), and
every tenant call resolves the caller's org from that key. A controller that
acted for several orgs on one key would therefore perform every call after the
first as the wrong tenant. The isolation boundary is: one key per org, and each
call carries the credential of the org it is acting for.
"""

import pytest

import daemon.daemon as daemon_module
from daemon.api import daemon_keys

ORG_A = "aaaaaaaa-1111-4111-8111-111111111111"
ORG_B = "bbbbbbbb-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _clean_key_state():
    """Isolate per-org key state between tests."""
    saved = (
        dict(daemon_module.API_HEADERS_BY_ORG),
        dict(daemon_module.MCP_CONFIG_BY_ORG),
        dict(daemon_module._KEY_BY_ORG),
        dict(daemon_keys._key_ids),
        dict(daemon_keys._key_expiries),
        dict(daemon_module.API_HEADERS),
    )
    daemon_module.API_HEADERS_BY_ORG.clear()
    daemon_module.MCP_CONFIG_BY_ORG.clear()
    daemon_module._KEY_BY_ORG.clear()
    daemon_keys._key_ids.clear()
    daemon_keys._key_expiries.clear()
    yield
    (
        headers_by_org,
        mcp_by_org,
        key_by_org,
        key_ids,
        expiries,
        headers,
    ) = saved
    daemon_module.API_HEADERS_BY_ORG.clear()
    daemon_module.API_HEADERS_BY_ORG.update(headers_by_org)
    daemon_module.MCP_CONFIG_BY_ORG.clear()
    daemon_module.MCP_CONFIG_BY_ORG.update(mcp_by_org)
    daemon_module._KEY_BY_ORG.clear()
    daemon_module._KEY_BY_ORG.update(key_by_org)
    daemon_keys._key_ids.clear()
    daemon_keys._key_ids.update(key_ids)
    daemon_keys._key_expiries.clear()
    daemon_keys._key_expiries.update(expiries)
    daemon_module.API_HEADERS.clear()
    daemon_module.API_HEADERS.update(headers)


def test_each_org_resolves_to_its_own_credential():
    daemon_module.API_HEADERS_BY_ORG[ORG_A] = {"Authorization": "Bearer key-a"}
    daemon_module.API_HEADERS_BY_ORG[ORG_B] = {"Authorization": "Bearer key-b"}
    daemon_module.API_HEADERS = {"Authorization": "Bearer process-key"}

    assert daemon_module.api_headers_for(ORG_A)["Authorization"] == "Bearer key-a"
    assert daemon_module.api_headers_for(ORG_B)["Authorization"] == "Bearer key-b"


def test_an_org_without_its_own_key_does_not_borrow_another_orgs_key():
    """The one case where a borrowed key would be a cross-tenant read."""
    daemon_module.API_HEADERS_BY_ORG[ORG_A] = {"Authorization": "Bearer key-a"}
    daemon_module.API_HEADERS = {"Authorization": "Bearer process-key"}

    resolved = daemon_module.api_headers_for(ORG_B)

    assert resolved["Authorization"] == "Bearer process-key"
    assert resolved["Authorization"] != "Bearer key-a"


def test_unknown_org_falls_back_to_the_process_credential():
    daemon_module.API_HEADERS = {"Authorization": "Bearer process-key"}

    assert daemon_module.api_headers_for(None)["Authorization"] == "Bearer process-key"
    assert daemon_module.api_headers_for("")["Authorization"] == "Bearer process-key"


@pytest.mark.asyncio
async def test_rotation_rewrites_only_the_affected_org(monkeypatch):
    """Rotating org A must leave org B's live credential untouched."""
    daemon_module.API_HEADERS_BY_ORG[ORG_A] = {"Authorization": "Bearer old-a"}
    daemon_module.API_HEADERS_BY_ORG[ORG_B] = {"Authorization": "Bearer old-b"}
    daemon_module._KEY_BY_ORG[ORG_A] = "old-a"
    daemon_module._KEY_BY_ORG[ORG_B] = "old-b"
    daemon_keys._key_ids[ORG_A] = "id-a"
    daemon_keys._key_ids[ORG_B] = "id-b"

    async def _rejected(_key):
        return False

    async def _provision(instance_id, organization_id=None):
        daemon_keys._key_ids[str(organization_id)] = "id-a-new"
        return "new-a"

    async def _service_org(_conn=None):
        return (ORG_A, "Org A")

    monkeypatch.setattr(daemon_module, "_verify_api_key", _rejected)
    monkeypatch.setattr(daemon_module, "_provision_daemon_api_key", _provision)
    monkeypatch.setattr(daemon_module, "_resolve_service_org_conn", _service_org)

    assert (
        await daemon_module._verify_and_provision_key(
            "instance", organization_id=ORG_A
        )
        is True
    )

    assert daemon_module.api_headers_for(ORG_A)["Authorization"] == "Bearer new-a"
    # Org B is untouched: its key was never rejected and never re-minted.
    assert daemon_module.api_headers_for(ORG_B)["Authorization"] == "Bearer old-b"
    assert daemon_module._KEY_BY_ORG[ORG_B] == "old-b"


@pytest.mark.asyncio
async def test_worker_is_bound_to_exactly_one_org(monkeypatch):
    """A worker must never provision keys for orgs it does not serve."""
    monkeypatch.setattr(daemon_module, "DAEMON_WORKER", True)
    monkeypatch.setattr(daemon_module, "DAEMON_ORG", ORG_B)

    async def _resolve(_conn=None):
        return (ORG_B, "Org B")

    monkeypatch.setattr(daemon_module, "_resolve_daemon_org_conn", _resolve)

    assert await daemon_module._served_org_ids() == [ORG_B]


@pytest.mark.asyncio
async def test_controller_serves_every_org(monkeypatch):
    monkeypatch.setattr(daemon_module, "DAEMON_WORKER", False)

    async def _list(_conn):
        return [(ORG_A, "Org A"), (ORG_B, "Org B")]

    monkeypatch.setattr(daemon_module, "_list_daemon_orgs", _list)

    import asyncpg

    class _Conn:
        async def close(self):
            return None

    async def _connect(*_a, **_k):
        return _Conn()

    # asyncpg is imported inside the function (module-level import is
    # TYPE_CHECKING-only), so patch the library the function will import.
    monkeypatch.setattr(asyncpg, "connect", _connect)

    assert await daemon_module._served_org_ids() == [ORG_A, ORG_B]
