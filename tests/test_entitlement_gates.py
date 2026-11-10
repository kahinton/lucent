"""Integration tests for the organization entitlement gates.

The rows and their default-deny contract are covered in
``test_entitlements.py``; this module proves the *enforcement* half at the
sites the capability actually binds:

* the shared gate helper (``lucent.services.entitlements``) refuses a real
  organization with no enabled row, passes one with the row, and exempts the
  ``__lucent%``-prefixed infrastructure organizations (the system org the
  daemon's platform work runs under) the migration backfill deliberately
  omitted,
* managed-tool execution (``ManagedToolExecutor``, the single funnel for the
  API run route, the MCP ``run_managed_tool`` tool, proxy discovery, and the
  engine) is refused for a non-entitled org before any run row is written and
  succeeds for an entitled org,
* managed-tool grants (``DefinitionRepository.grant_capability``, which
  every attach path shares) refuse without the entitlement, attach with it,
  and stay open for an infrastructure org and for a ``system=True`` platform
  grant,
* dispatch-time sandbox creation (``daemon/sandbox/lifecycle``) refuses with
  the refusal shape the daemon records on the task.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from daemon.sandbox.lifecycle import _create_task_sandbox, _sandbox_entitlement_failure
from lucent.db import DefinitionRepository, EntitlementRepository, OrganizationRepository
from lucent.db.user import UserRepository
from lucent.services.entitlements import (
    CAPABILITY_MANAGED_TOOLS,
    CAPABILITY_SANDBOXES,
    CapabilityDisabledError,
    capability_gate_message,
    capability_is_granted,
    ensure_capability,
)
from lucent.services.managed_tools import ManagedToolBlockedError, ManagedToolExecutor

pytestmark = pytest.mark.integration


@pytest.fixture
async def infrastructure_organization(db_pool):
    """A real organization row with an infrastructure name (exempt from gates)."""
    repo = OrganizationRepository(db_pool)
    org = await repo.create(name=f"__lucent_test_gate_{uuid4().hex[:8]}")
    yield org
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM organizations WHERE id = $1", str(org["id"]))


# ── The shared helper ────────────────────────────────────────────────────


async def test_org_without_rows_is_refused_and_error_names_the_capability(
    db_pool, test_organization
):
    org_id = test_organization["id"]
    for capability in (CAPABILITY_SANDBOXES, CAPABILITY_MANAGED_TOOLS):
        assert await capability_is_granted(db_pool, org_id, capability) is False
        assert await capability_gate_message(org_id, capability, pool=db_pool)
        with pytest.raises(CapabilityDisabledError) as raised:
            await ensure_capability(db_pool, org_id, capability)
        message = str(raised.value)
        if capability not in message:
            pytest.fail(f"{message!r} does not name the refused capability")
        if "not enabled for this organization" not in message:
            pytest.fail(f"{message!r} does not say the capability is not enabled")


async def test_entitled_org_is_granted(db_pool, capable_organization):
    org_id = capable_organization["id"]
    for capability in (CAPABILITY_SANDBOXES, CAPABILITY_MANAGED_TOOLS):
        assert await capability_is_granted(db_pool, org_id, capability) is True
        assert (
            await capability_gate_message(org_id, capability, pool=db_pool) is None
        )
        await ensure_capability(db_pool, org_id, capability)  # no raise


async def test_one_granted_capability_does_not_leak_into_the_other(
    db_pool, test_organization
):
    org_id = str(test_organization["id"])
    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(org_id, CAPABILITY_SANDBOXES, True, source="override")
    try:
        assert await capability_is_granted(db_pool, org_id, CAPABILITY_SANDBOXES)
        assert await capability_is_granted(db_pool, org_id, CAPABILITY_MANAGED_TOOLS) is False
    finally:
        await repo.set_entitlement(org_id, CAPABILITY_SANDBOXES, False, source="override")


async def test_disabling_the_row_refuses_again(db_pool, capable_organization):
    """Flipping through `override` is the admin lever: off means refused."""
    org_id = str(capable_organization["id"])
    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(org_id, CAPABILITY_SANDBOXES, False, source="override")
    try:
        assert await capability_is_granted(db_pool, org_id, CAPABILITY_SANDBOXES) is False
    finally:
        await repo.set_entitlement(org_id, CAPABILITY_SANDBOXES, True, source="override")


async def test_infrastructure_org_is_exempt_without_a_row(
    db_pool, infrastructure_organization
):
    """The system/infra org the daemon's platform work runs under is ungated.

    It has no entitlement rows (the migration backfill excluded ``__lucent%``
    names on purpose), so without this exemption the platform would deny
    itself. This is also where "the daemon is not blocked" holds: platform
    (system) work runs under this org, and a daemon acting for a tenant is
    charged to that tenant — covered below through the disabled-org refusals.
    """
    org_id = infrastructure_organization["id"]
    for capability in (CAPABILITY_SANDBOXES, CAPABILITY_MANAGED_TOOLS):
        assert await capability_is_granted(db_pool, org_id, capability) is True
        assert (
            await capability_gate_message(org_id, capability, pool=db_pool) is None
        )


async def test_unknown_organization_id_is_not_gone_through(db_pool):
    """A UUID that names no organization is denied, not crashed on."""
    org_id = str(uuid4())
    assert await capability_is_granted(db_pool, org_id, CAPABILITY_SANDBOXES) is False


async def test_non_uuid_org_id_is_denied_without_touching_the_database(db_pool):
    assert await capability_is_granted(db_pool, "enabled-org", CAPABILITY_SANDBOXES) is (
        False
    )


# ── Managed tools: execution ─────────────────────────────────────────────


def _stub_executor(db_pool):
    """An executor over a mock repo whose pool is the real one.

    The gate must read the real entitlement rows, so the pool is real while
    the run bookkeeping stays mocked out — no runtime is involved in a refusal
    (that is the point of refusing *before* execution).
    """
    repo = AsyncMock()
    repo.pool = db_pool
    repo.create_managed_tool_run.return_value = {"id": str(uuid4())}
    return ManagedToolExecutor(repo)


async def test_executor_refuses_org_without_entitlement(db_pool, test_organization):
    executor = _stub_executor(db_pool)
    org_id = str(test_organization["id"])
    with pytest.raises(ManagedToolBlockedError, match="managed_tools"):
        await executor.execute(
            tool={"id": str(uuid4()), "status": "active", "name": "no-entitlement"},
            arguments={},
            org_id=org_id,
            user_id=str(uuid4()),
            enforce_agent_grant=False,
        )
    # No run row: the refusal happens before the action is booked.
    executor.repo.create_managed_tool_run.assert_not_awaited()


async def test_executor_refusal_names_the_capability_and_the_org(
    db_pool, test_organization
):
    executor = _stub_executor(db_pool)
    with pytest.raises(ManagedToolBlockedError) as raised:
        await executor.execute(
            tool={"id": str(uuid4()), "status": "active", "name": "no-entitlement"},
            arguments={},
            org_id=str(test_organization["id"]),
            user_id=str(uuid4()),
            enforce_agent_grant=False,
        )
    assert "managed_tools" in str(raised.value)
    assert "not enabled for this organization" in str(raised.value)


async def test_executor_runs_for_entitled_org(db_pool, capable_organization, monkeypatch):
    class _RecordingRuntime:
        def __init__(self):
            self.executed = False

        async def execute(self, context):
            self.executed = True
            from lucent.services.tool_runtimes.base import RuntimeExecutionResult

            return RuntimeExecutionResult(ok=True, result={"done": True})

    runtime = _RecordingRuntime()
    monkeypatch.setattr(
        "lucent.services.managed_tools.get_runtime", lambda _kind: runtime
    )
    executor = _stub_executor(db_pool)
    result = await executor.execute(
        tool={"id": str(uuid4()), "status": "active", "name": "entitled-tool"},
        arguments={},
        org_id=str(capable_organization["id"]),
        user_id=str(uuid4()),
        enforce_agent_grant=False,
    )
    assert result.ok is True
    assert runtime.executed is True
    # The run this execution produced was booked through the same pool.
    executor.repo.create_managed_tool_run.assert_awaited_once()


async def test_executor_runs_for_infrastructure_org_without_rows(
    db_pool, infrastructure_organization, monkeypatch
):
    class _RecordingRuntime:
        async def execute(self, context):
            from lucent.services.tool_runtimes.base import RuntimeExecutionResult

            return RuntimeExecutionResult(ok=True, result={"done": True})

    monkeypatch.setattr(
        "lucent.services.managed_tools.get_runtime", lambda _kind: _RecordingRuntime()
    )
    executor = _stub_executor(db_pool)
    result = await executor.execute(
        tool={"id": str(uuid4()), "status": "active", "name": "system-tool"},
        arguments={},
        org_id=str(infrastructure_organization["id"]),
        user_id=str(uuid4()),
        enforce_agent_grant=False,
    )
    assert result.ok is True


async def test_executor_discovery_refuses_org_without_entitlement(
    db_pool, test_organization
):
    """The discover path executes the proxy tool — same gate, not a bypass."""
    executor = _stub_executor(db_pool)
    with pytest.raises(ManagedToolBlockedError, match="managed_tools"):
        await executor.discover_mcp_proxy_tools(
            tool={
                "id": str(uuid4()),
                "status": "active",
                "runtime_config": {"mcp_proxy": {"url": "https://proxy.invalid"}},
            },
            org_id=str(test_organization["id"]),
            user_id=str(uuid4()),
            enforce_agent_grant=False,
        )


async def test_executor_python_tool_needs_both_capabilities(
    db_pool, test_organization, monkeypatch
):
    """A sandbox-backed kind charges custom_sandboxes on top of managed_tools.

    Builtin/HTTP kinds run without a sandbox, so managed_tools alone decides;
    python kinds additionally require the sandbox entitlement.
    """
    from lucent.services.tool_runtimes.base import RuntimeExecutionResult

    class _RecordingRuntime:
        async def execute(self, context):
            return RuntimeExecutionResult(ok=True, result={"done": True})

    monkeypatch.setattr(
        "lucent.services.managed_tools.get_runtime", lambda _kind: _RecordingRuntime()
    )
    org_id = str(test_organization["id"])
    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(org_id, CAPABILITY_MANAGED_TOOLS, True, source="override")
    try:
        executor = _stub_executor(db_pool)
        # builtin kind: sandbox entitlement not required
        result = await executor.execute(
            tool={
                "id": str(uuid4()),
                "status": "active",
                "runtime_type": "builtin",
                "runtime_config": {"handler": "uuid_generate"},
                "input_schema": {"type": "object", "properties": {}},
            },
            arguments={},
            org_id=org_id,
            user_id=str(uuid4()),
            enforce_agent_grant=False,
        )
        assert result.ok is True
        # python kind: still refused, refusing message names the sandboxes
        with pytest.raises(ManagedToolBlockedError, match="custom_sandboxes"):
            await executor.execute(
                tool={
                    "id": str(uuid4()),
                    "status": "active",
                    "runtime_type": "python",
                    "source_code": "def run():\n    return 1",
                },
                arguments={},
                org_id=org_id,
                user_id=str(uuid4()),
                enforce_agent_grant=False,
            )
    finally:
        await repo.set_entitlement(org_id, CAPABILITY_MANAGED_TOOLS, False, source="override")


# ── Managed tools: the grant choke point ─────────────────────────────────


@pytest.fixture
async def org_agent_and_user(db_pool, test_organization):
    """An active agent definition plus its owner user, for grant tests."""
    repo = DefinitionRepository(db_pool)
    user = await UserRepository(db_pool).create(
        external_id=f"test_entgate_{uuid4().hex[:8]}",
        provider="local",
        organization_id=test_organization["id"],
        email=f"gate_{uuid4().hex[:6]}@test.local",
        display_name="Gate user",
        role="admin",
    )
    agent = await repo.create_agent(
        name="gate-agent",
        description="Grant gate subject",
        content="# Agent",
        org_id=test_organization["id"],
        created_by=user["id"],
        status="active",
        owner_user_id=user["id"],
    )
    tool = await repo.create_managed_tool(
        name=f"gate-tool-{uuid4().hex[:6]}",
        description="Grant gate tool",
        source_code="def run():\n    return 1",
        input_schema={"type": "object", "properties": {}},
        org_id=test_organization["id"],
        created_by=user["id"],
        owner_user_id=user["id"],
        proposal_reason="grant gate test",
    )
    activated = await repo.approve_managed_tool(
        str(tool["id"]), str(test_organization["id"]), user["id"]
    )
    await repo.approve_managed_tool(str(tool["id"]), str(test_organization["id"]), user["id"])
    yield user, agent, activated
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM managed_tool_runs WHERE organization_id = $1",
            test_organization["id"],
        )
        await conn.execute(
            "DELETE FROM agent_managed_tools WHERE agent_id = $1", agent["id"]
        )
        await conn.execute(
            "DELETE FROM managed_tool_definitions WHERE organization_id = $1",
            test_organization["id"],
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE organization_id = $1",
            test_organization["id"],
        )


async def test_grant_managed_tool_refuses_org_without_entitlement(
    db_pool, test_organization, org_agent_and_user
):
    user, agent, tool = org_agent_and_user
    repo = DefinitionRepository(db_pool)
    with pytest.raises(CapabilityDisabledError) as raised:
        await repo.grant_capability(
            agent_id=str(agent["id"]),
            capability_type="managed_tool",
            capability_id=str(tool["id"]),
            org_id=str(test_organization["id"]),
            actor_user_id=str(user["id"]),
        )
    assert "managed_tools" in str(raised.value)
    grants = await repo.get_agent_managed_tools(str(agent["id"]))
    assert grants == []


async def test_grant_managed_tool_succeeds_for_entitled_org(
    db_pool, capable_organization, org_agent_and_user
):
    user, agent, tool = org_agent_and_user
    repo = DefinitionRepository(db_pool)
    outcome = await repo.grant_capability(
        agent_id=str(agent["id"]),
        capability_type="managed_tool",
        capability_id=str(tool["id"]),
        org_id=str(capable_organization["id"]),
        actor_user_id=str(user["id"]),
    )
    grants = await repo.get_agent_managed_tools(str(agent["id"]))
    if outcome.name != "ATTACHED" or [g["name"] for g in grants] != [tool["name"]]:
        pytest.fail(f"Entitled org grant failed: outcome={outcome}, grants={grants!r}")


async def test_grant_managed_tool_allowed_for_infrastructure_org(
    db_pool, infrastructure_organization
):
    repo = DefinitionRepository(db_pool)
    infra_org_id = infrastructure_organization["id"]
    user = await UserRepository(db_pool).create(
        external_id=f"test_entgate_infra_{uuid4().hex[:8]}",
        provider="local",
        organization_id=infra_org_id,
        email=f"gate_infra_{uuid4().hex[:6]}@test.local",
        display_name="Infra gate user",
        role="admin",
    )
    agent = await repo.create_agent(
        name="infra-gate-agent",
        description="Infra grant gate",
        content="# Agent",
        org_id=infra_org_id,
        created_by=user["id"],
        status="active",
        owner_user_id=user["id"],
    )
    tool = await repo.create_managed_tool(
        name=f"infra-gate-tool-{uuid4().hex[:6]}",
        description="Infra grant gate tool",
        source_code="def run():\n    return 1",
        input_schema={"type": "object", "properties": {}},
        org_id=infra_org_id,
        created_by=user["id"],
        owner_user_id=user["id"],
        proposal_reason="infra grant gate",
    )
    await repo.approve_managed_tool(str(tool["id"]), str(infra_org_id), user["id"])
    await repo.approve_managed_tool(str(tool["id"]), str(infra_org_id), user["id"])
    try:
        outcome = await repo.grant_capability(
            agent_id=str(agent["id"]),
            capability_type="managed_tool",
            capability_id=str(tool["id"]),
            org_id=str(infra_org_id),
            actor_user_id=str(user["id"]),
        )
        assert outcome.name == "ATTACHED"
        assert await repo.get_agent_managed_tools(str(agent["id"])), (
            "infrastructure org grant should not be gated"
        )
    finally:
        await repo.revoke_managed_tool(str(agent["id"]), str(tool["id"]), org_id=str(infra_org_id))
        # The infrastructure organization is outside every cleanup pattern the
        # conftest fixtures know about (its name is __lucent%-prefixed, not the
        # per-test prefix), so this test removes its own rows; the user is then
        # reachable by the session-end external_id sweep without a FK row left
        # behind to block it.
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_managed_tools WHERE agent_id = $1", agent["id"]
            )
            await conn.execute(
                "DELETE FROM managed_tool_definitions WHERE id = $1", tool["id"]
            )
            await conn.execute(
                "DELETE FROM agent_definitions WHERE id = $1", agent["id"]
            )


async def test_system_grant_bypasses_the_gate(db_pool, test_organization):
    """A platform (system=True) grant is exempt, as at bootstrap."""
    repo = DefinitionRepository(db_pool)
    org_id = str(test_organization["id"])
    user = await UserRepository(db_pool).create(
        external_id=f"test_entgate_sys_{uuid4().hex[:8]}",
        provider="local",
        organization_id=test_organization["id"],
        email=f"gate_sys_{uuid4().hex[:6]}@test.local",
        display_name="System gate user",
        role="admin",
    )
    agent = await repo.create_agent(
        name="sys-gate-agent",
        description="System grant gate",
        content="# Agent",
        org_id=org_id,
        created_by=user["id"],
        status="active",
        owner_user_id=user["id"],
    )
    tool = await repo.create_managed_tool(
        name=f"sys-gate-tool-{uuid4().hex[:6]}",
        description="System grant gate tool",
        source_code="def run():\n    return 1",
        input_schema={"type": "object", "properties": {}},
        org_id=org_id,
        created_by=user["id"],
        owner_user_id=user["id"],
        proposal_reason="system grant gate",
    )
    await repo.approve_managed_tool(str(tool["id"]), org_id, user["id"])
    await repo.approve_managed_tool(str(tool["id"]), org_id, user["id"])
    try:
        outcome = await repo.grant_capability(
            agent_id=str(agent["id"]),
            capability_type="managed_tool",
            capability_id=str(tool["id"]),
            org_id=org_id,
            actor_user_id=None,
            system=True,
        )
        assert outcome.name == "ATTACHED"
    finally:
        await repo.revoke_managed_tool(str(agent["id"]), str(tool["id"]), org_id=org_id)


# ── Sandboxes: the dispatch gate ─────────────────────────────────────────


async def test_sandbox_entitlement_failure_for_org_without_rows(
    db_pool, test_organization
):
    failure = await _sandbox_entitlement_failure(str(test_organization["id"]))
    if failure is None:
        pytest.fail("An org with no entitlement rows must be refused at dispatch")
    assert failure["stage"] == "sandbox_not_entitled"
    assert failure["capability"] == "custom_sandboxes"
    assert "custom_sandboxes" in failure["detail"]
    assert "not enabled for this organization" in failure["detail"]


async def test_sandbox_entitlement_none_for_entitled_org(db_pool, capable_organization):
    assert await _sandbox_entitlement_failure(str(capable_organization["id"])) is None


async def test_sandbox_entitlement_none_for_infrastructure_org(
    db_pool, infrastructure_organization
):
    assert await _sandbox_entitlement_failure(str(infrastructure_organization["id"])) is None


async def test_org_sandbox_grant_flips_dispatch_back_on(db_pool, test_organization):
    """Enable → the dispatch probe passes; disable → refused again."""
    org_id = str(test_organization["id"])
    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(org_id, CAPABILITY_SANDBOXES, True, source="override")
    assert await _sandbox_entitlement_failure(org_id) is None
    await repo.set_entitlement(org_id, CAPABILITY_SANDBOXES, False, source="override")
    refusal = await _sandbox_entitlement_failure(org_id)
    assert refusal is not None and refusal["stage"] == "sandbox_not_entitled"


async def test_create_task_sandbox_refuses_before_allocation(
    db_pool, test_organization, monkeypatch
):
    """The gate binds inside _create_task_sandbox, before the manager runs."""
    manager = MagicMock()
    monkeypatch.setattr("lucent.sandbox.manager.get_sandbox_manager", lambda: manager)
    sandbox_id, config, failure, reused = await _create_task_sandbox(
        MagicMock(),
        "task-entgate01",
        {},
        requesting_user_id=str(uuid4()),
        org_id=str(test_organization["id"]),
    )
    assert sandbox_id is None
    assert config is None
    assert reused is False
    assert failure["stage"] == "sandbox_not_entitled"
    assert failure["capability"] == "custom_sandboxes"
    assert manager.create.call_count == 0
