"""Repository-layer admin-audit coverage tests.

Regression tests for the admin_audit_log cessation (runtime hardening scope 4):
definition, grant, and sandbox-template mutations must leave a trace in
admin_audit_log even when the caller constructs the repository without an
explicit audit repo (the MCP-tool / daemon path), and even when no explicit
actor is wired (system-originated mutations).

Strategy: the repositories only need ``pool.acquire()`` — a fake connection
that records executed statements and returns canned rows exercises the full
mutation + audit funnel without Postgres.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from lucent.auth import set_current_user
from lucent.db.admin_audit import AdminAuditRepository
from lucent.db.audit import AuditRepository
from lucent.db.definitions import DefinitionRepository
from lucent.db.sandbox_template import SandboxTemplateRepository

ORG_ID = "0f9abaa4-7489-47ab-8d6c-7c5be8d69d51"
USER_ID = "7b2129ec-392c-4787-85b7-184ae8c23ed4"
TEMPLATE_ID = "d5566e2e-7538-4d44-9d0e-0ada3aa7fa78"
AGENT_ID = "4f523367-2dbc-46e5-84c7-6740394a8590"
TOOL_ID = "a6e34d50-4907-41cd-9982-e8f6ba07c6ff"


class FakeConn:
    """Minimal asyncpg-connection stand-in recording executed statements.

    Result rows are synthesized by query shape instead of a single canned
    row, because these repositories run heterogeneous statements over one
    pool: COUNT probes need ``total``, INSERT ... RETURNING needs a row
    shaped like the statement's own RETURNING clause, and UPDATE ...
    RETURNING needs the post-mutation state.
    """

    def __init__(self, pool: "FakePool"):
        self.pool = pool
        self.executed = pool.recorded

    def _record(self, kind: str, query: str, *params: Any) -> None:
        self.executed.append(
            {"kind": kind, "query": " ".join(str(query).split()), "params": list(params)}
        )

    async def fetchrow(self, query: str, *params: Any) -> dict | None:
        self._record("fetchrow", query, *params)
        if self.pool.fail_next_fetchrow:
            self.pool.fail_next_fetchrow = False
            raise RuntimeError("simulated db failure")
        q = " ".join(str(query).split())
        uq = q.upper()

        # COUNT probes (list_all, list_for_org) → {"total": N}
        if "COUNT(*) AS TOTAL" in uq:
            return {"total": 1}

        # INSERT ... RETURNING: hand back the row the statement itself
        # returns. RETURNING columns are resolved against the INSERT's own
        # declared column list — the same semantics asyncpg applies — so
        # memory-audit _row_to_dict and admin dict() both see the columns
        # they expect. "RETURNING *" yields the canned row; unknown columns
        # fall back to the canned row, and id/created_at are synthesized so
        # downstream accessors (e.g. str(row["id"])) never hit a missing key.
        if uq.startswith("INSERT") and "RETURNING" in uq:
            declared: dict[str, Any] = {}
            m = re.search(r"INSERT\s+INTO\s+\S+\s*\(([^)]*)\)", q, re.IGNORECASE)
            if m:
                names = [c.strip().strip('"').lower() for c in m.group(1).split(",")]
                declared = {n: p for n, p in zip(names, params) if n}
            out_row: dict[str, Any] = dict(self.pool.result_row)
            for col in q[q.rfind("RETURNING") + len("RETURNING"):].split(","):
                col = col.strip().strip('"').lower()
                if col == "*":
                    # RETURNING * returns the statement's full row, but the
                    # canned row may be empty — synthesize the columns
                    # downstream accessors always read (see docstring above)
                    # so they never hit a missing key.
                    out_row.setdefault("id", str(uuid.uuid4()))
                    out_row.setdefault("created_at", datetime.now(timezone.utc))
                    continue
                if not col:
                    continue
                if col in declared:
                    out_row[col] = declared[col]
                elif col == "id":
                    out_row[col] = str(uuid.uuid4())
                elif col in {"created_at", "updated_at"}:
                    out_row[col] = datetime.now(timezone.utc)
                elif col not in out_row:
                    out_row[col] = None
            return out_row

        # Template UPDATE ... RETURNING: reconstruct the post-update state
        # from the SET clauses so set_status asserts see the new status.
        # The real pool registers a jsonb codec (pool.py _init_connection,
        # decoder=json.loads), so `col = $n::jsonb` columns come back from
        # a real connection as decoded objects — mirror that here.
        if uq.startswith("UPDATE") and "RETURNING" in uq:
            row = dict(self.pool.result_row)
            set_clause = re.search(r"\bSET\b(.*?)\bWHERE\b", q, re.IGNORECASE | re.DOTALL)
            if set_clause:
                for col, idx, cast in re.findall(
                    r"([a-z_][a-z0-9_]*)\s*=\s*\$(\d+)(::[a-z]+)?",
                    set_clause.group(1),
                    re.IGNORECASE,
                ):
                    i = int(idx) - 1
                    if 0 <= i < len(params):
                        val = params[i]
                        if cast and "jsonb" in cast and isinstance(val, str):
                            try:
                                val = json.loads(val)
                            except (ValueError, TypeError):
                                pass
                        row[col] = val
            return row

        return dict(self.pool.result_row) if self.pool.result_row else None

    async def execute(self, query: str, *params: Any) -> str:
        self._record("execute", query, *params)
        q = " ".join(str(query).split())
        if q.upper().startswith("DELETE"):
            return "DELETE 1"
        if q.upper().startswith("UPDATE"):
            return "UPDATE 1"
        return "INSERT 0 1"

    async def fetch(self, query: str, *params: Any) -> list[dict]:
        self._record("fetch", query, *params)
        if self.pool.result_row is None:
            return []
        return [dict(self.pool.result_row)]


class FakePool:
    """asyncpg.Pool stand-in handing out one FakeConn per acquire()."""

    def __init__(self, result_row: dict | None = None):
        self.recorded: list[dict[str, Any]] = []
        self.result_row = result_row or {}
        self.fail_next_fetchrow = False

    def acquire(self):
        return _AcquireCtx(self)

    async def close(self) -> None:  # pragma: no cover - unused
        pass


class _AcquireCtx:
    def __init__(self, pool: FakePool):
        self.pool = pool
        self.conn = FakeConn(pool)
        self.conn.fail_next_fetchrow = pool.fail_next_fetchrow

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _template_row(**overrides: Any) -> dict:
    row = {
        "id": TEMPLATE_ID,
        # Coherent with the create() assertions below (probe-template).
        "name": "probe-template",
        "description": "self-dev template",
        "image": "python:3.12-slim",
        "repo_url": None,
        "branch": None,
        "setup_commands": "[]",
        "env_vars": "{}",
        "working_dir": "/workspace",
        "docker_bind_mounts": "[]",
        "extra_hosts": "{}",
        "memory_limit": "2g",
        "cpu_limit": 2.0,
        "disk_limit": "10g",
        "network_mode": "none",
        "allowed_hosts": "[]",
        "timeout_seconds": 1800,
        "organization_id": ORG_ID,
        "created_by": USER_ID,
        "owner_user_id": USER_ID,
        "owner_group_id": None,
        "scope": "instance",
        "status": "approved",
        "proposed_by": None,
        "proposal_reason": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "created_at": "2026-09-04T15:11:13.864039",
        "updated_at": "2026-09-04T17:31:06.383301",
        "last_used_at": None,
    }
    row.update(overrides)
    return row


def _definition_row(entity: str, **overrides: Any) -> dict:
    row = {
        "id": TOOL_ID if entity == "managed_tool" else AGENT_ID,
        "name": f"test-{entity}",
        "description": "",
        "content": "content" if entity != "managed_tool" else "def handler(): pass",
        # managed_tool_definitions execution contract (migration 087) —
        # update paths validate source_code/entrypoint from the canned row.
        "runtime_type": "python",
        "source_code": "content" if entity == "managed_tool" else "",
        "entrypoint": "handler" if entity == "managed_tool" else "",
        "requirements": "[]",
        "runtime_config": "{}",
        "input_schema": '{"type": "object", "properties": {}}',
        "output_schema": None,
        "auth_policy": '{"mode": "agent_grant", "require_user_access": true}',
        "network_policy": '{"network_mode": "none", "allowed_hosts": []}',
        "resource_limits": '{"memory_limit": "512m", "cpu_limit": 1.0, "disk_limit": "1g", "timeout_seconds": 300}',
        "timeout_seconds": 300,
        "env_vars": "{}",
        "headers": "{}",
        "args": "[]",
        "discovered_tools": None,
        "tools_discovered_at": None,
        "allowed_tools": None,
        "status": "proposed",
        "scope": "instance",
        "organization_id": ORG_ID,
        "created_by": USER_ID,
        "approved_by": None,
        "approved_at": None,
        "owner_user_id": USER_ID,
        "owner_group_id": None,
        "proposal_reason": None,
        "proposal_evidence": "{}",
        "created_at": "2026-09-04T18:00:00",
        "updated_at": "2026-09-04T18:00:00",
    }
    row.update(overrides)
    return row


def _admin_inserts(recorded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract (query, params) pairs that INSERT INTO admin_audit_log."""
    out = []
    for rec in recorded:
        if "insert into admin_audit_log" in rec["query"].lower():
            out.append(rec)
    return out


def _admin_row(rec: dict[str, Any]) -> dict[str, Any]:
    """Map the admin-audit INSERT params to column names (matches repo SQL)."""
    p = rec["params"]
    keys = (
        "organization_id", "actor_user_id", "impersonator_user_id",
        "entity_type", "entity_id", "entity_label", "action",
        "changed_fields", "old_values", "new_values", "context", "notes", "outcome",
    )
    return dict(zip(keys, p))


@pytest.fixture(autouse=True)
def _clean_auth_context():
    set_current_user(None)
    yield
    set_current_user(None)


@pytest.fixture
def actor_owner():
    set_current_user(
        {"id": USER_ID, "organization_id": ORG_ID, "role": "owner", "memory_scope": None}
    )
    return {"id": USER_ID, "organization_id": ORG_ID, "role": "owner"}


# ---------------------------------------------------------------------------
# 1. Definition mutations (MCP/agent path — NO audit_repo passed) are audited
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entity,mutate",
    [
        ("agent", lambda repo: repo.update_agent(AGENT_ID, ORG_ID, name="renamed")),
        ("agent", lambda repo: repo.delete_agent(AGENT_ID, ORG_ID)),
        ("skill", lambda repo: repo.update_skill(AGENT_ID, ORG_ID, content="v2")),
        ("skill", lambda repo: repo.delete_skill(AGENT_ID, ORG_ID)),
        ("hook", lambda repo: repo.update_hook(AGENT_ID, ORG_ID, content="v2")),
        ("hook", lambda repo: repo.delete_hook(AGENT_ID, ORG_ID)),
        ("managed_tool", lambda repo: repo.update_managed_tool(TOOL_ID, ORG_ID, description="v2")),
        ("managed_tool", lambda repo: repo.delete_managed_tool(TOOL_ID, ORG_ID)),
        ("mcp_server", lambda repo: repo.update_mcp_server(AGENT_ID, ORG_ID, url="http://x")),
        ("mcp_server", lambda repo: repo.delete_mcp_server(AGENT_ID, ORG_ID)),
    ],
)
def test_definition_mutations_audited_without_audit_repo(entity, mutate, actor_owner):
    """MCP layer constructs DefinitionRepository(pool) with no audit_repo —
    admin audit must still fire (default-on, actor from auth context)."""
    pool = FakePool(_definition_row(entity))
    repo = DefinitionRepository(pool)
    if entity == "mcp_server":
        pool.result_row = _definition_row(entity, id=AGENT_ID)
    result = asyncio.run(mutate(repo))
    assert result is not None, "mutation must still succeed"
    rows = _admin_inserts(pool.recorded)
    assert rows, "admin_audit_log insert missing"
    audit = _admin_row(rows[0])
    assert audit["entity_type"] == entity
    assert audit["organization_id"] == ORG_ID
    assert audit["action"].startswith("definition.")
    assert audit["actor_user_id"] == USER_ID


@pytest.mark.parametrize(
    "mutate",
    [
        lambda repo: repo.approve_agent(AGENT_ID, ORG_ID, USER_ID),
        lambda repo: repo.reject_agent(AGENT_ID, ORG_ID, USER_ID),
        lambda repo: repo.approve_skill(AGENT_ID, ORG_ID, USER_ID),
        lambda repo: repo.approve_hook(AGENT_ID, ORG_ID, USER_ID),
        lambda repo: repo.approve_managed_tool(TOOL_ID, ORG_ID, USER_ID),
        lambda repo: repo.reject_managed_tool(TOOL_ID, ORG_ID, USER_ID),
        lambda repo: repo.approve_mcp_server(AGENT_ID, ORG_ID, USER_ID),
    ],
)
def test_definition_approvals_audited(mutate, actor_owner):
    pool = FakePool(_definition_row("skill", status="active"))
    repo = DefinitionRepository(pool)
    result = asyncio.run(mutate(repo))
    assert result is not None
    rows = _admin_inserts(pool.recorded)
    assert rows
    audit = _admin_row(rows[0])
    assert audit["action"] in ("definition.approve", "definition.reject")
    assert audit["actor_user_id"] == USER_ID


# ---------------------------------------------------------------------------
# 2. Grants and revocations are audited
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "grant_call,revoke_call",
    [
        (
            lambda repo: repo.grant_skill(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
            lambda repo: repo.revoke_skill(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
        ),
        (
            lambda repo: repo.grant_managed_tool(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
            lambda repo: repo.revoke_managed_tool(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
        ),
        (
            lambda repo: repo.grant_mcp_server(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
            lambda repo: repo.revoke_mcp_server(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
        ),
        (
            lambda repo: repo.grant_hook(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
            lambda repo: repo.revoke_hook(AGENT_ID, TOOL_ID, org_id=ORG_ID, user_id=USER_ID),
        ),
    ],
)
def test_grants_and_revocations_audited(grant_call, revoke_call, actor_owner):
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)
    assert asyncio.run(grant_call(repo)) is True
    rows = _admin_inserts(pool.recorded)
    assert rows, "grant must be audited"
    audit = _admin_row(rows[0])
    assert audit["action"] == "definition.grant"
    assert audit["context"] and json.loads(audit["context"]).get("agent_id") == AGENT_ID

    pool.recorded.clear()
    assert asyncio.run(revoke_call(repo)) is True
    rows = _admin_inserts(pool.recorded)
    assert rows, "revocation must be audited"
    audit = _admin_row(rows[0])
    assert audit["action"] == "definition.revoke"


# ---------------------------------------------------------------------------
# 3. The d5566e2e silent-wipe path is audited
# ---------------------------------------------------------------------------

def test_template_update_audited_with_old_values(actor_owner):
    """Exactly the surface that silently wiped template d5566e2e's
    docker_bind_mounts on 2026-09-04 17:31:06 UTC."""
    before = _template_row(docker_bind_mounts='[{"hostPath": "/Users/kyle/Dev/lucent", "containerPath": "/workspace/lucent"}]')
    pool = FakePool(before)
    repo = SandboxTemplateRepository(pool)

    result = asyncio.run(repo.update(TEMPLATE_ID, ORG_ID, docker_bind_mounts=[]))
    assert result is not None
    assert result["docker_bind_mounts"] == []

    rows = _admin_inserts(pool.recorded)
    assert rows, "template update must be audited"
    audit = _admin_row(rows[0])
    assert audit["entity_type"] == "sandbox_template"
    assert audit["entity_id"] == TEMPLATE_ID
    assert audit["action"] == "sandbox_template.update"
    assert audit["actor_user_id"] == USER_ID
    old = json.loads(audit["old_values"])
    new = json.loads(audit["new_values"])
    assert old["docker_bind_mounts"], "old_values must capture the wiped mounts"
    assert new["docker_bind_mounts"] == []


def test_template_create_delete_status_audited(actor_owner):
    pool = FakePool(_template_row())
    repo = SandboxTemplateRepository(pool)

    created = asyncio.run(
        repo.create(
            name="probe-template", organization_id=ORG_ID,
            created_by=USER_ID, description="x", image="python:3.12-slim",
        )
    )
    assert created["name"] == "probe-template"
    rows = _admin_inserts(pool.recorded)
    assert rows
    assert _admin_row(rows[0])["action"] == "sandbox_template.create"

    pool.recorded.clear()
    deleted = asyncio.run(repo.delete(TEMPLATE_ID, ORG_ID))
    assert deleted is True
    rows = _admin_inserts(pool.recorded)
    assert rows
    assert _admin_row(rows[0])["action"] == "sandbox_template.delete"

    pool.recorded.clear()
    rejected = asyncio.run(repo.set_status(TEMPLATE_ID, ORG_ID, "rejected", USER_ID))
    assert rejected is not None
    rows = _admin_inserts(pool.recorded)
    assert rows
    audit = _admin_row(rows[0])
    assert audit["action"] == "sandbox_template.status_change"
    assert json.loads(audit["new_values"])["status"] == "rejected"
    assert json.loads(audit["old_values"])["status"] == "approved"


def test_template_read_and_mark_used_not_audited(actor_owner):
    """Noise guard: reads, mark_used, and status-guard reads never audit."""
    pool = FakePool(_template_row())
    repo = SandboxTemplateRepository(pool)
    asyncio.run(repo.get(TEMPLATE_ID, ORG_ID))
    asyncio.run(repo.mark_used(TEMPLATE_ID, organization_id=ORG_ID))
    asyncio.run(repo.list_all(ORG_ID))
    assert _admin_inserts(pool.recorded) == []


def test_template_sync_built_in_not_audited(actor_owner):
    """Noise guard: mechanical built-in template sync (startup) must not
    flood admin_audit_log. It goes through the explicit audit=False path."""
    pool = FakePool(None)
    repo = SandboxTemplateRepository(pool, audit=False)
    # create() inside the sync path must not audit when audit=False
    asyncio.run(
        repo.create(
            name="builtin-probe", organization_id=ORG_ID, scope="built-in",
            created_by=None, audit=False,
        )
    )
    assert _admin_inserts(pool.recorded) == []


# ---------------------------------------------------------------------------
# 4. Actor attribution fallbacks
# ---------------------------------------------------------------------------

def test_actor_falls_back_to_auth_context(actor_owner):
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)
    asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    audit = _admin_row(_admin_inserts(pool.recorded)[0])
    assert audit["actor_user_id"] == USER_ID
    ctx = json.loads(audit["context"])
    assert ctx.get("auth_source") == "request_context"


def test_actor_none_when_unauthenticated():
    """No auth context (system-originated) → actor NULL, row still written."""
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)
    result = asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    assert result is not None
    rows = _admin_inserts(pool.recorded)
    assert rows, "unauthenticated mutation must still be audited"
    audit = _admin_row(rows[0])
    assert audit["actor_user_id"] is None
    assert json.loads(audit["context"]).get("actor_source") == "system"


def test_impersonator_recorded():
    impersonator = USER_ID
    set_current_user(
        {
            "id": AGENT_ID,
            "organization_id": ORG_ID,
            "role": "member",
            "impersonator_id": impersonator,
        }
    )
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)
    asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    audit = _admin_row(_admin_inserts(pool.recorded)[0])
    assert audit["actor_user_id"] == AGENT_ID
    assert audit["impersonator_user_id"] == impersonator


def test_bad_actor_id_does_not_break_mutation(actor_owner):
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)
    asyncio.run(
        repo.update_skill(AGENT_ID, ORG_ID, content="v2")
    )
    # Sanity only — malformed auth context is exercised below.
    assert _admin_inserts(pool.recorded)


def test_malformed_actor_context_mutation_still_succeeds():
    set_current_user({"id": "not-a-uuid", "organization_id": "also-bad", "role": "member"})
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)
    result = asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    assert result is not None, "audit attribution failure must never break the mutation"
    audit = _admin_row(_admin_inserts(pool.recorded)[0])
    assert audit["actor_user_id"] is None
    assert json.loads(audit["context"]).get("actor_source") == "system"


# ---------------------------------------------------------------------------
# 5. Audit failure isolation + memory audit pairing
# ---------------------------------------------------------------------------

def test_admin_audit_failure_does_not_break_mutation(actor_owner, monkeypatch):
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool)

    async def explode(*args, **kwargs):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(AdminAuditRepository, "log", explode, raising=True)
    result = asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    assert result is not None, "mutation must survive admin-audit failure"


def test_memory_audit_and_admin_audit_both_fire_when_paired(actor_owner):
    """When an explicit memory audit repo is wired (API/web router path),
    mutations must produce BOTH the memory audit event and the admin row."""
    pool = FakePool(_definition_row("skill"))
    admin = AdminAuditRepository(pool)
    memory = AuditRepository(pool)
    repo = DefinitionRepository(pool, audit_repo=memory, admin_audit=True)
    asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    admin_rows = _admin_inserts(pool.recorded)
    assert admin_rows, "admin audit must fire alongside memory audit"
    memory_rows = [
        r for r in pool.recorded
        if "insert into memory_audit_log" in r["query"].lower()
    ]
    assert memory_rows, "memory audit must still fire"


def test_explicit_audit_false_disables_admin_audit(actor_owner):
    pool = FakePool(_definition_row("skill"))
    repo = DefinitionRepository(pool, admin_audit=False)
    asyncio.run(repo.update_skill(AGENT_ID, ORG_ID, content="v2"))
    assert _admin_inserts(pool.recorded) == []


# ---------------------------------------------------------------------------
# 6. Noise guards on mechanical update paths
# ---------------------------------------------------------------------------

def test_discovery_cache_updates_not_audited(actor_owner):
    pool = FakePool(_definition_row("mcp_server", id=AGENT_ID))
    repo = DefinitionRepository(pool)
    asyncio.run(repo.save_discovered_tools(AGENT_ID, [{"name": "t"}], ORG_ID))
    assert _admin_inserts(pool.recorded) == []
    memory_rows = [
        r for r in pool.recorded
        if "insert into memory_audit_log" in r["query"].lower()
    ]
    assert memory_rows, "discovery caching keeps its memory-audit behavior"


def test_builtin_sync_methods_not_audited(actor_owner):
    """The raw-SQL built-in syncs never touched the audit funnel; ensure
    default-on audit does not add noise to them (they don't call _audit)."""
    pool = FakePool(None)
    repo = DefinitionRepository(pool)
    count = asyncio.run(repo.sync_built_in_hooks(ORG_ID))
    assert count >= 0
    assert _admin_inserts(pool.recorded) == []