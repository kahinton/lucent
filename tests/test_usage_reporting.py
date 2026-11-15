"""Usage quantities for instance operators (migrations 154-156).

Two different things are billed, and the tests keep them apart because their
semantics genuinely differ:

* **Seats** are a *live count* — "who is on the account right now".
* **Resource usage** is *history* read from the append-only ledger, filtered by
  period.

The properties that matter for a usage report, and that these tests pin down:

* service identities and operator staff are never seats (counting them would
  inflate every invoice and make the count depend on internal machinery);
* derived quantities come from primitives so every consumer agrees on
  rounding, and no price is stored anywhere;
* a stopped-then-destroyed sandbox bills the interval up to the *stop*, not to
  the teardown (the first close wins);
* usage survives tenant deletion, which is exactly when a final invoice is
  still needed;
* the ledger is idempotent, so a retried teardown cannot double-bill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.resource_usage import ResourceUsageRepository, parse_memory_limit_mb
from lucent.services import usage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def usage_prefix(db_pool, delete_test_organizations):
    prefix = f"bill_{str(uuid4())[:8]}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM resource_usage_events WHERE organization_name LIKE $1",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await delete_test_organizations(conn, [f"{prefix}%"])


@pytest_asyncio.fixture
async def usage_org(db_pool, usage_prefix):
    return await OrganizationRepository(db_pool).create(name=f"{usage_prefix}org")


@pytest_asyncio.fixture
async def operator_org(db_pool, usage_prefix):
    return await OrganizationRepository(db_pool).create(name=f"{usage_prefix}oporg")


@pytest_asyncio.fixture
async def operator(db_pool, operator_org, usage_prefix):
    return await UserRepository(db_pool).create(
        external_id=f"{usage_prefix}operator",
        provider="local",
        organization_id=operator_org["id"],
        email=f"{usage_prefix}operator@test.com",
        display_name="Usage Operator",
        role="hyperadmin",
    )


@pytest_asyncio.fixture
async def operator_client(operator):
    app = create_app()
    fake = CurrentUser(
        id=operator["id"],
        organization_id=operator["organization_id"],
        role="hyperadmin",
        email=operator.get("email"),
        display_name=operator.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _add_user(db_pool, org_id, prefix, role="member", active=True):
    user = await UserRepository(db_pool).create(
        external_id=f"{prefix}{role}-{uuid4().hex[:8]}",
        provider="local",
        organization_id=org_id,
        email=f"{prefix}{uuid4().hex[:8]}@test.com",
        display_name=f"{role} user",
        role=role,
    )
    if not active:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_active = FALSE WHERE id = $1", user["id"]
            )
        user = await UserRepository(db_pool).get_by_id(user["id"])
    return user


def _interval(*, org_name, sandbox_id=None, hours=2.0, cpu=2.0, memory_mb=2048,
              source="task", outcome="destroyed"):
    started = datetime.now(UTC) - timedelta(hours=hours)
    return dict(
        organization_name=org_name,
        sandbox_id=sandbox_id or str(uuid4()),
        started_at=started,
        ended_at=started + timedelta(hours=hours),
        cpu_limit=cpu,
        memory_limit_mb=memory_mb,
        source=source,
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Memory parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("512m", 512),
        ("2g", 2048),
        ("1.5g", 1536),
        ("256MB", 256),
        ("2gb", 2048),
        ("1t", 1024 * 1024),
        (1024 * 1024, 1),  # bare numbers are bytes
        (None, None),
        ("", None),
        ("lots", None),
        ("12", 0),  # 12 bytes -> 0 MiB, not "unknown"
    ],
)
def test_memory_limit_parsing(value, expected):
    """Unknown and zero must stay distinguishable.

    ``None`` means "we do not know this reservation", which a usage caller must
    not invoice as free; ``0`` is a genuinely tiny limit. Collapsing them would
    silently under-bill.
    """
    assert parse_memory_limit_mb(value) == expected


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seats_exclude_service_and_staff_identities(db_pool, usage_org, usage_prefix):
    """Seat count must not depend on Lucent's internal machinery.

    Every organization gets a ``daemon-service`` identity on creation, and
    operator staff may belong to a tenant. Counting either would inflate every
    invoice and make the number depend on how Lucent is deployed.
    """
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="owner")
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="member")
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="member")
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="hyperadmin")
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="daemon")

    counts = await usage.count_metered_users(db_pool, usage_org["id"])

    # The org's own daemon-service row is created by OrganizationRepository.
    assert counts["metered"] == 3
    assert counts["by_role"] == {"owner": 1, "member": 2}
    assert "daemon" in counts["excluded_roles"]


@pytest.mark.asyncio
async def test_inactive_members_are_not_metered_by_default(
    db_pool, usage_org, usage_prefix
):
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="member")
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="member", active=False)

    active_only = await usage.count_metered_users(db_pool, usage_org["id"])
    everyone = await usage.count_metered_users(
        db_pool, usage_org["id"], active_only=False
    )

    assert active_only["metered"] == 1
    assert active_only["total"] == 2
    # The choice is reported back so a caller cannot misread which was used.
    assert active_only["active_only"] is True
    assert everyone["metered"] == 2


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derived_totals_from_primitives(db_pool, usage_org, usage_prefix):
    """CPU-seconds and MB-seconds are derived server-side, not stored.

    Every consumer would otherwise recompute them and disagree on rounding, and
    a stored total would bake a pricing model into the schema.
    """
    repo = ResourceUsageRepository(db_pool)
    await repo.record_interval(
        organization_id=usage_org["id"],
        **_interval(
            org_name=usage_org["name"], hours=2, cpu=2.0, memory_mb=2048
        ),
    )

    totals = await repo.totals_for_organization(usage_org["id"])
    summary = totals["summary"]
    assert summary["intervals"] == 1
    assert summary["duration_seconds"] == 7200
    assert float(summary["cpu_seconds"]) == 14400.0  # 2 cpus * 7200s
    assert float(summary["cpu_hours"]) == 4.0
    assert summary["memory_mb_seconds"] == 2048 * 7200


@pytest.mark.asyncio
async def test_recording_the_same_sandbox_twice_does_not_double_bill(
    db_pool, usage_org, usage_prefix
):
    """Teardown is reachable more than once (stop then destroy, retried destroy)."""
    repo = ResourceUsageRepository(db_pool)
    sandbox_id = str(uuid4())
    interval = _interval(org_name=usage_org["name"], sandbox_id=sandbox_id, hours=1)

    await repo.record_interval(organization_id=usage_org["id"], **interval)
    await repo.record_interval(organization_id=usage_org["id"], **interval)

    rows = await repo.list_events(usage_org["id"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_first_close_wins_so_a_later_destroy_does_not_extend_the_interval(
    db_pool, usage_org, usage_prefix
):
    """A sandbox stopped at 10:00 and destroyed at 14:00 consumed until 10:00.

    Taking the later end would bill four idle hours. This is the single most
    expensive mistake the ledger could make, so it is asserted directly.
    """
    repo = ResourceUsageRepository(db_pool)
    sandbox_id = str(uuid4())
    started = datetime.now(UTC) - timedelta(hours=10)

    await repo.record_interval(
        organization_id=usage_org["id"],
        organization_name=usage_org["name"],
        sandbox_id=sandbox_id,
        started_at=started,
        ended_at=started + timedelta(hours=1),
        cpu_limit=1.0,
        memory_limit_mb=1024,
        outcome="stopped",
    )
    # The destroy arrives four hours later.
    row = await repo.record_interval(
        organization_id=usage_org["id"],
        organization_name=usage_org["name"],
        sandbox_id=sandbox_id,
        started_at=started,
        ended_at=started + timedelta(hours=5),
        cpu_limit=1.0,
        memory_limit_mb=1024,
        outcome="destroyed",
    )

    assert row["duration_seconds"] == 3600
    assert row["outcome"] == "destroyed"
    # And the recorded duration agrees with the stored end time.
    assert (row["ended_at"] - row["started_at"]).total_seconds() == 3600


@pytest.mark.asyncio
async def test_a_second_close_cannot_blank_out_recorded_limits(
    db_pool, usage_org, usage_prefix
):
    """A sweeper that no longer holds the config must not erase known limits."""
    repo = ResourceUsageRepository(db_pool)
    sandbox_id = str(uuid4())
    interval = _interval(
        org_name=usage_org["name"], sandbox_id=sandbox_id, cpu=4.0, memory_mb=4096
    )
    await repo.record_interval(organization_id=usage_org["id"], **interval)

    # A later close with no limits and no tenant name.
    await repo.record_interval(
        organization_id=usage_org["id"],
        organization_name="",
        sandbox_id=sandbox_id,
        started_at=interval["started_at"],
        ended_at=interval["ended_at"],
        outcome="destroyed",
    )

    row = (await repo.list_events(usage_org["id"]))[0]
    assert float(row["cpu_limit"]) == 4.0
    assert row["memory_limit_mb"] == 4096
    assert row["organization_name"] == usage_org["name"]


@pytest.mark.asyncio
async def test_period_filter_excludes_out_of_range_intervals(
    db_pool, usage_org, usage_prefix
):
    repo = ResourceUsageRepository(db_pool)
    old = datetime.now(UTC) - timedelta(days=30)
    await repo.record_interval(
        organization_id=usage_org["id"],
        organization_name=usage_org["name"],
        sandbox_id=str(uuid4()),
        started_at=old,
        ended_at=old + timedelta(hours=1),
        cpu_limit=1.0,
    )
    await repo.record_interval(
        organization_id=usage_org["id"],
        **_interval(org_name=usage_org["name"], hours=1, cpu=1.0),
    )

    recent = await repo.totals_for_organization(
        usage_org["id"], starts_at=datetime.now(UTC) - timedelta(days=1)
    )
    assert recent["summary"]["intervals"] == 1


# ---------------------------------------------------------------------------
# Usage survives tenant deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_survives_organization_deletion(db_pool, usage_prefix):
    """A final invoice must still be producible after a tenant is offboarded.

    This is why ``organization_id`` is ON DELETE SET NULL rather than CASCADE,
    and why the tenant name is snapshotted.
    """
    org = await OrganizationRepository(db_pool).create(name=f"{usage_prefix}leaving")
    org_name = org["name"]
    repo = ResourceUsageRepository(db_pool)
    await repo.record_interval(
        organization_id=org["id"],
        **_interval(org_name=org_name, hours=1, cpu=2.0, memory_mb=2048),
    )

    await OrganizationRepository(db_pool).delete(org["id"])

    rows = await repo.list_events()
    surviving = [r for r in rows if r["organization_name"] == org_name]
    assert len(surviving) == 1
    # The FK is nulled, but the row is still attributable by name.
    assert surviving[0]["organization_id"] is None
    assert surviving[0]["organization_name"] == org_name


# ---------------------------------------------------------------------------
# Emission from the sandbox manager
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, **kw):
        self.organization_id = kw.get("organization_id")
        self.task_id = kw.get("task_id")
        self.request_id = kw.get("request_id")
        self.cpu_limit = kw.get("cpu_limit", 1.0)
        self.memory_limit = kw.get("memory_limit", "1g")
        self.disk_limit = kw.get("disk_limit", "10g")
        self.name = kw.get("name", "sb")


@pytest.mark.asyncio
async def test_managed_tool_sandboxes_are_classified_before_task(
    db_pool, usage_org
):
    """A managed-tool sandbox carries a task_id (for its bridge).

    If classification checked ``task_id`` first, every tool run would be billed
    as task work and a customer querying "my custom tool usage" would see
    nothing.
    """
    from lucent.services.usage import record_sandbox_usage

    await record_sandbox_usage(
        db_pool,
        sandbox_id=str(uuid4()),
        config=_FakeConfig(
            organization_id=usage_org["id"],
            task_id=str(uuid4()),
            name="managed-tool-my-tool",
        ),
        started_at=datetime.now(UTC) - timedelta(minutes=5),
        outcome="destroyed",
    )

    rows = await ResourceUsageRepository(db_pool).list_events(usage_org["id"])
    assert rows[0]["source"] == "managed_tool"


@pytest.mark.asyncio
async def test_task_and_interactive_sources_are_distinguished(db_pool, usage_org):
    from lucent.services.usage import record_sandbox_usage

    await record_sandbox_usage(
        db_pool,
        sandbox_id=str(uuid4()),
        config=_FakeConfig(organization_id=usage_org["id"], task_id=str(uuid4())),
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await record_sandbox_usage(
        db_pool,
        sandbox_id=str(uuid4()),
        config=_FakeConfig(organization_id=usage_org["id"]),
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    rows = await ResourceUsageRepository(db_pool).list_events(usage_org["id"])
    assert {r["source"] for r in rows} == {"task", "interactive"}


@pytest.mark.asyncio
async def test_usage_without_an_org_or_start_time_is_not_recorded(db_pool):
    """Recording nothing is correct here; guessing would corrupt the ledger."""
    from lucent.services.usage import record_sandbox_usage

    no_org = await record_sandbox_usage(
        db_pool,
        sandbox_id=str(uuid4()),
        config=_FakeConfig(organization_id=None),
        started_at=datetime.now(UTC),
    )
    no_start = await record_sandbox_usage(
        db_pool,
        sandbox_id=str(uuid4()),
        config=_FakeConfig(organization_id=str(uuid4())),
        started_at=None,
    )
    assert no_org is None
    assert no_start is None


@pytest.mark.asyncio
async def test_record_usage_never_raises_on_a_bad_organization(db_pool):
    """A usage write must not be able to fail a teardown and leak a container."""
    from lucent.services.usage import record_sandbox_usage

    result = await record_sandbox_usage(
        db_pool,
        sandbox_id=str(uuid4()),
        config=_FakeConfig(organization_id=str(uuid4())),  # does not exist
        started_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert result is None


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_reconstructs_finished_sandboxes(db_pool, usage_org):
    """Usage recorded before the ledger existed survives only as rows + config."""
    started = datetime.now(UTC) - timedelta(hours=3)
    ended = started + timedelta(hours=1)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sandboxes
                (id, name, status, image, config, organization_id,
                 ready_at, stopped_at, created_at)
            VALUES ($1, 'legacy', 'stopped', 'img',
                    $2::jsonb, $3, $4, $5, $4)
            """,
            uuid4(),
            '{"cpu_limit": 2.0, "memory_limit": "2g", "disk_limit": "5g",'
            ' "task_id": null, "request_id": null}',
            usage_org["id"],
            started,
            ended,
        )

    result = await usage.backfill_from_sandboxes(db_pool, organization_id=usage_org["id"])
    assert result["recorded"] == 1

    rows = await ResourceUsageRepository(db_pool).list_events(usage_org["id"])
    assert len(rows) == 1
    assert float(rows[0]["cpu_limit"]) == 2.0
    assert rows[0]["memory_limit_mb"] == 2048
    assert rows[0]["duration_seconds"] == 3600


@pytest.mark.asyncio
async def test_backfill_is_idempotent(db_pool, usage_org):
    started = datetime.now(UTC) - timedelta(hours=2)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sandboxes
                (id, name, status, image, config, organization_id,
                 ready_at, destroyed_at, created_at)
            VALUES ($1, 'legacy', 'destroyed', 'img', '{}'::jsonb, $2, $3, $4, $3)
            """,
            uuid4(),
            usage_org["id"],
            started,
            started + timedelta(hours=1),
        )

    first = await usage.backfill_from_sandboxes(db_pool, organization_id=usage_org["id"])
    second = await usage.backfill_from_sandboxes(db_pool, organization_id=usage_org["id"])
    assert first["recorded"] == 1
    # Already closed by the first run, so nothing new to do.
    assert second["recorded"] == 0
    assert len(await ResourceUsageRepository(db_pool).list_events(usage_org["id"])) == 1


@pytest.mark.asyncio
async def test_backfill_skips_live_sandboxes(db_pool, usage_org):
    """A live sandbox has no end; including it would write a growing interval."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sandboxes
                (id, name, status, image, config, organization_id, created_at)
            VALUES ($1, 'running', 'ready', 'img', '{}'::jsonb, $2, NOW())
            """,
            uuid4(),
            usage_org["id"],
        )
    result = await usage.backfill_from_sandboxes(db_pool, organization_id=usage_org["id"])
    assert result["recorded"] == 0


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(db_pool, usage_org):
    started = datetime.now(UTC) - timedelta(hours=2)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sandboxes
                (id, name, status, image, config, organization_id,
                 ready_at, destroyed_at, created_at)
            VALUES ($1, 'legacy', 'destroyed', 'img', '{}'::jsonb, $2, $3, $4, $3)
            """,
            uuid4(),
            usage_org["id"],
            started,
            started + timedelta(hours=1),
        )

    result = await usage.backfill_from_sandboxes(
        db_pool, organization_id=usage_org["id"], dry_run=True
    )
    assert result["dry_run"] is True
    assert result["would_record"] == 1
    assert await ResourceUsageRepository(db_pool).list_events(usage_org["id"]) == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_returns_seats_and_resources_per_tenant(
    operator_client, db_pool, usage_org, usage_prefix
):
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="owner")
    await _add_user(db_pool, usage_org["id"], usage_prefix, role="member")
    await ResourceUsageRepository(db_pool).record_interval(
        organization_id=usage_org["id"],
        **_interval(org_name=usage_org["name"], hours=1, cpu=2.0, memory_mb=2048),
    )

    resp = await operator_client.get("/api/organizations/instance/usage/resources")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    tenant = next(
        t for t in body["tenants"] if str(t["organization_id"]) == str(usage_org["id"])
    )
    assert tenant["seats"]["metered"] == 2
    assert tenant["resources"]["intervals"] == 1
    assert tenant["resources"]["duration_seconds"] == 3600
    assert body["instance_totals"]["seats"] >= 2


@pytest.mark.asyncio
async def test_report_includes_offboarded_tenants_with_usage(
    operator_client, db_pool, usage_prefix
):
    """A deleted tenant still owes for its final period."""
    org = await OrganizationRepository(db_pool).create(name=f"{usage_prefix}gone")
    await ResourceUsageRepository(db_pool).record_interval(
        organization_id=org["id"],
        **_interval(org_name=org["name"], hours=1, cpu=1.0, memory_mb=1024),
    )
    await OrganizationRepository(db_pool).delete(org["id"])

    resp = await operator_client.get("/api/organizations/instance/usage/resources")
    assert resp.status_code == 200, resp.text
    tenant = next(
        (t for t in resp.json()["tenants"] if t["organization_name"] == org["name"]),
        None,
    )
    assert tenant is not None
    assert tenant["status"] == "deleted"
    assert tenant["seats"] is None
    assert tenant["resources"]["duration_seconds"] == 3600


@pytest.mark.asyncio
async def test_report_keeps_separate_offboarded_tenants_apart(operator_client, db_pool):
    """Two deleted tenants must not merge into one billable line.

    ``resource_usage_events.organization_id`` is ``ON DELETE SET NULL`` so the
    ledger outlives the tenant — which means every offboarded tenant has a NULL
    id. Grouping the totals on that id alone collapsed all of them into a single
    NULL row, so a second offboarded tenant's period disappeared into the
    first's (or was dropped entirely when the report keyed by ``str(id)``).
    """
    org_repo = OrganizationRepository(db_pool)
    created = []
    for suffix in ("gone_a", "gone_b"):
        org = await org_repo.create(name=f"{usage_prefix}{suffix}")
        await ResourceUsageRepository(db_pool).record_interval(
            organization_id=org["id"],
            **_interval(org_name=org["name"], hours=1, cpu=1.0, memory_mb=1024),
        )
        created.append(org)
    for org in created:
        await org_repo.delete(org["id"])

    resp = await operator_client.get("/api/organizations/instance/usage/resources")
    assert resp.status_code == 200, resp.text
    by_name = {t["organization_name"]: t for t in resp.json()["tenants"]}

    for org in created:
        tenant = by_name.get(org["name"])
        assert tenant is not None, f"{org['name']} is missing from the report"
        assert tenant["status"] == "deleted"
        assert tenant["resources"]["intervals"] == 1
        assert tenant["resources"]["duration_seconds"] == 3600


@pytest.mark.asyncio
async def test_report_rejects_an_inverted_period(operator_client):
    resp = await operator_client.get(
        "/api/organizations/instance/usage/resources",
        params={
            "starts_at": "2026-10-02T00:00:00Z",
            "ends_at": "2026-10-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_organization_usage_splits_by_source(
    operator_client, db_pool, usage_org
):
    repo = ResourceUsageRepository(db_pool)
    await repo.record_interval(
        organization_id=usage_org["id"],
        **_interval(org_name=usage_org["name"], hours=1, cpu=1.0, source="task"),
    )
    await repo.record_interval(
        organization_id=usage_org["id"],
        **_interval(
            org_name=usage_org["name"], hours=1, cpu=4.0, source="managed_tool"
        ),
    )

    resp = await operator_client.get(
        f"/api/organizations/{usage_org['id']}/usage/resources"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sources = {row["source"]: row for row in body["resources_by_source"]}
    assert set(sources) == {"task", "managed_tool"}
    assert float(sources["managed_tool"]["cpu_seconds"]) > float(
        sources["task"]["cpu_seconds"]
    )


@pytest.mark.asyncio
async def test_organization_usage_unknown_tenant_is_404(operator_client):
    resp = await operator_client.get(f"/api/organizations/{uuid4()}/usage/resources")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_usage_events_expose_the_raw_intervals(
    operator_client, db_pool, usage_org
):
    """A disputed invoice must be traceable to specific intervals."""
    sandbox_id = str(uuid4())
    await ResourceUsageRepository(db_pool).record_interval(
        organization_id=usage_org["id"],
        **_interval(org_name=usage_org["name"], sandbox_id=sandbox_id, hours=1),
    )

    resp = await operator_client.get(
        f"/api/organizations/{usage_org['id']}/usage/ledger"
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["sandbox_id"] == sandbox_id
    assert rows[0]["duration_seconds"] == 3600


@pytest.mark.asyncio
async def test_backfill_endpoints(operator_client, db_pool, usage_org):
    started = datetime.now(UTC) - timedelta(hours=2)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sandboxes
                (id, name, status, image, config, organization_id,
                 ready_at, destroyed_at, created_at)
            VALUES ($1, 'legacy', 'destroyed', 'img', '{}'::jsonb, $2, $3, $4, $3)
            """,
            uuid4(),
            usage_org["id"],
            started,
            started + timedelta(hours=1),
        )

    preview = await operator_client.get(
        "/api/organizations/instance/usage/backfill-preview",
        params={"organization_id": str(usage_org["id"])},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["would_record"] == 1

    run = await operator_client.post(
        "/api/organizations/instance/usage/backfill",
        params={"organization_id": str(usage_org["id"])},
    )
    assert run.status_code == 200, run.text
    assert run.json()["recorded"] == 1


@pytest.mark.asyncio
async def test_usage_routes_require_hyperadmin(db_pool, usage_org, usage_prefix):
    """A tenant owner must not be able to read other tenants' usage."""
    owner = await _add_user(db_pool, usage_org["id"], usage_prefix, role="owner")
    app = create_app()
    fake = CurrentUser(
        id=owner["id"],
        organization_id=usage_org["id"],
        role="owner",
        email=owner.get("email"),
        display_name=owner.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        report = await c.get("/api/organizations/instance/usage/resources")
        own = await c.get(f"/api/organizations/{usage_org['id']}/usage/resources")
        backfill = await c.post("/api/organizations/instance/usage/backfill")
    app.dependency_overrides.clear()

    assert report.status_code == 403
    assert own.status_code == 403
    assert backfill.status_code == 403


@pytest.mark.asyncio
async def test_instance_usage_path_is_not_parsed_as_an_organization_id(
    operator_client,
):
    """``instance`` must not be swallowed by ``/{organization_id}``.

    The static routes are declared above the parameterised ones; if that ordering
    regresses this returns 422 instead of 200.
    """
    resp = await operator_client.get("/api/organizations/instance/usage/resources")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Managed tool runs (migration 155)
# ---------------------------------------------------------------------------
#
# A tool run and the sandbox it created describe the same resources. Recording
# both as metered compute would double every custom-tool charge, so the ledger
# splits the two row types and the aggregations must respect the split. These
# tests exist mostly to make that mistake impossible to reintroduce silently.

TOOL_OUTCOMES_OK = ("stopped", "destroyed")


@pytest.mark.asyncio
async def test_tool_run_is_recorded_as_an_invocation(db_pool, usage_org):
    tool_id = uuid4()
    run_id = str(uuid4())
    result = await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=run_id,
        tool_id=str(tool_id),
        organization_id=usage_org["id"],
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        outcome="completed",
    )
    assert result is not None
    assert result["resource_type"] == "managed_tool_run"
    assert result["sandbox_id"] is None
    # The compute columns must stay empty: the run's CPU/memory lives on the
    # sandbox row it created.
    assert result["cpu_limit"] is None
    assert result["memory_limit_mb"] is None


@pytest.mark.asyncio
async def test_tool_invocations_never_add_to_compute_totals(db_pool, usage_org):
    """The double-usage guard.

    One tool run (invocation) plus the sandbox it ran in (compute) must produce
    one interval of compute and one invocation — not two of either.
    """
    repo = ResourceUsageRepository(db_pool)

    # The sandbox the tool ran in, carrying the compute.
    await repo.record_interval(
        organization_id=usage_org["id"],
        **_interval(org_name=usage_org["name"], hours=1, cpu=2.0, memory_mb=2048),
    )
    # The invocation itself.
    await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=str(uuid4()),
        tool_id=str(uuid4()),
        organization_id=usage_org["id"],
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        outcome="completed",
    )

    totals = await repo.totals_for_organization(usage_org["id"])
    summary = totals["summary"]
    assert summary["intervals"] == 1, "the tool run must not count as an interval"
    assert summary["duration_seconds"] == 3600
    assert float(summary["cpu_seconds"]) == 7200.0  # 2 cpus * 3600s, once
    assert summary["memory_mb_seconds"] == 2048 * 3600
    assert summary["tool_invocations"] == 1


@pytest.mark.asyncio
async def test_tool_run_completion_is_idempotent(db_pool, usage_org):
    """A retried completion must not create a second invocable unit."""
    run_id = str(uuid4())
    started = datetime.now(UTC) - timedelta(seconds=20)
    for _ in range(3):
        await usage.record_managed_tool_usage(
            db_pool,
            managed_tool_run_id=run_id,
            tool_id=str(uuid4()),
            organization_id=usage_org["id"],
            started_at=started,
            outcome="completed",
        )

    rows = await ResourceUsageRepository(db_pool).list_events(usage_org["id"])
    matching = [r for r in rows if str(r["managed_tool_run_id"]) == run_id]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_later_outcome_replaces_the_provisional_one(db_pool, usage_org):
    """A run's last status is authoritative, unlike a sandbox interval.

    A "running"-ish first write turning into "failed" must not leave the earlier
    outcome in place, or failed invocations would be invoiced as successes.
    """
    run_id = str(uuid4())
    started = datetime.now(UTC) - timedelta(seconds=20)
    await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=run_id,
        tool_id=str(uuid4()),
        organization_id=usage_org["id"],
        started_at=started,
        outcome="unknown",
    )
    await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=run_id,
        tool_id=str(uuid4()),
        organization_id=usage_org["id"],
        started_at=started,
        outcome="failed",
    )

    rows = await ResourceUsageRepository(db_pool).list_events(usage_org["id"])
    row = next(r for r in rows if str(r["managed_tool_run_id"]) == run_id)
    assert row["outcome"] == "failed"


@pytest.mark.asyncio
async def test_per_tool_usage_counts_invocations_and_successes(db_pool, usage_org):
    tool_a, tool_b = str(uuid4()), str(uuid4())
    # Tool runs use the TOOL outcome vocabulary: completed / failed / blocked.
    # An earlier version of this test used the sandbox words (destroyed/stopped),
    # which is how the coercion bug stayed hidden.
    for tool_id, outcome in (
        (tool_a, "completed"),
        (tool_a, "completed"),
        (tool_a, "failed"),
        (tool_b, "completed"),
    ):
        await usage.record_managed_tool_usage(
            db_pool,
            managed_tool_run_id=str(uuid4()),
            tool_id=tool_id,
            organization_id=usage_org["id"],
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            outcome=outcome,
        )

    totals = await ResourceUsageRepository(db_pool).totals_for_organization(
        usage_org["id"]
    )
    by_tool = {str(row["tool_id"]): row for row in totals["by_tool"]}
    assert by_tool[tool_a]["invocations"] == 3
    assert by_tool[tool_a]["succeeded"] == 2
    assert by_tool[tool_b]["invocations"] == 1


@pytest.mark.asyncio
async def test_tool_usage_survives_organization_deletion(db_pool, usage_prefix):
    """A per-invocation price still needs the final period after offboarding.

    This is the gap ``managed_tool_runs`` could not fill: that table cascades on
    the organization, so its rows vanish with the tenant.
    """
    org = await OrganizationRepository(db_pool).create(name=f"{usage_prefix}tools")
    org_name = org["name"]
    await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=str(uuid4()),
        tool_id=str(uuid4()),
        organization_id=org["id"],
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        outcome="completed",
    )

    await OrganizationRepository(db_pool).delete(org["id"])

    rows = await ResourceUsageRepository(db_pool).list_events()
    surviving = [
        r
        for r in rows
        if r["organization_name"] == org_name
        and r["resource_type"] == "managed_tool_run"
    ]
    assert len(surviving) == 1
    assert surviving[0]["organization_id"] is None
    assert surviving[0]["tool_id"] is not None


@pytest.mark.asyncio
async def test_record_interval_requires_one_identity(db_pool, usage_org):
    """A row with neither identity cannot be keyed, so it is refused outright."""
    with pytest.raises(ValueError, match="sandbox_id or managed_tool_run_id"):
        await ResourceUsageRepository(db_pool).record_interval(
            organization_id=usage_org["id"],
            organization_name=usage_org["name"],
            started_at=datetime.now(UTC) - timedelta(minutes=1),
            ended_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_database_rejects_compute_on_a_tool_run_row(db_pool, usage_org):
    """The split is enforced in the schema, not only in the writer.

    A future caller that writes compute onto an invocation row would silently
    double-bill; the CHECK constraint makes that impossible.
    """
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resource_usage_events
                    (organization_id, organization_name, resource_type, source,
                     managed_tool_run_id, cpu_limit, started_at, ended_at,
                     duration_seconds, outcome)
                VALUES ($1, $2, 'managed_tool_run', 'managed_tool', $3, 2.0,
                        NOW() - interval '1 minute', NOW(), 60, 'completed')
                """,
                usage_org["id"],
                usage_org["name"],
                uuid4(),
            )


@pytest.mark.asyncio
async def test_database_rejects_a_tool_row_naming_a_sandbox(db_pool, usage_org):
    """Both identities at once would make the conflict target ambiguous."""
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resource_usage_events
                    (organization_id, organization_name, resource_type, source,
                     sandbox_id, managed_tool_run_id, started_at, ended_at,
                     duration_seconds, outcome)
                VALUES ($1, $2, 'managed_tool_run', 'managed_tool', 'sb-1', $3,
                        NOW() - interval '1 minute', NOW(), 60, 'completed')
                """,
                usage_org["id"],
                usage_org["name"],
                uuid4(),
            )


@pytest.mark.asyncio
async def test_org_api_exposes_tool_invocations(operator_client, db_pool, usage_org):
    tool_id = str(uuid4())
    await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=str(uuid4()),
        tool_id=tool_id,
        organization_id=usage_org["id"],
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        outcome="completed",
    )

    resp = await operator_client.get(
        f"/api/organizations/{usage_org['id']}/usage/resources"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resources"]["tool_invocations"] == 1
    assert body["resources"]["intervals"] == 0, "an invocation is not an interval"
    assert len(body["tool_usage"]) == 1
    assert body["tool_usage"][0]["tool_id"] == tool_id
    assert body["tool_usage"][0]["invocations"] == 1


@pytest.mark.asyncio
async def test_instance_report_counts_tool_invocations(operator_client, db_pool, usage_org):
    await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=str(uuid4()),
        tool_id=str(uuid4()),
        organization_id=usage_org["id"],
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        outcome="completed",
    )

    resp = await operator_client.get("/api/organizations/instance/usage/resources")
    assert resp.status_code == 200, resp.text
    tenant = next(
        t for t in resp.json()["tenants"] if str(t["organization_id"]) == str(usage_org["id"])
    )
    assert tenant["resources"]["tool_invocations"] == 1


@pytest.mark.asyncio
async def test_a_completed_run_is_stored_as_completed_not_coerced(db_pool, usage_org):
    """Regression: the sandbox outcome vocabulary silently swallowed "completed".

    ``record_managed_tool_usage`` coerces an unrecognised outcome to ``unknown``
    so an unexpected value cannot break the write. Before the tool vocabulary was
    allowed by the CHECK constraint, a *successful* run was therefore stored as
    ``unknown`` and any per-invocation metering that priced successes counted zero.
    Caught on the live instance, not by the first pass of tests, because the write
    succeeded and simply reported the wrong thing.
    """
    run_id = str(uuid4())
    result = await usage.record_managed_tool_usage(
        db_pool,
        managed_tool_run_id=run_id,
        tool_id=str(uuid4()),
        organization_id=usage_org["id"],
        started_at=datetime.now(UTC) - timedelta(seconds=10),
        outcome="completed",
    )
    assert result is not None
    assert result["outcome"] == "completed", "a success must not be recorded as unknown"


@pytest.mark.asyncio
async def test_blocked_and_failed_runs_are_also_stored_verbatim(db_pool, usage_org):
    for outcome in ("blocked", "failed"):
        result = await usage.record_managed_tool_usage(
            db_pool,
            managed_tool_run_id=str(uuid4()),
            tool_id=str(uuid4()),
            organization_id=usage_org["id"],
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            outcome=outcome,
        )
        assert result["outcome"] == outcome


@pytest.mark.asyncio
async def test_succeeded_counts_completed_runs(db_pool, usage_org):
    """The headline number a per-invocation price needs."""
    tool_id = str(uuid4())
    for outcome in ("completed", "completed", "failed", "blocked"):
        await usage.record_managed_tool_usage(
            db_pool,
            managed_tool_run_id=str(uuid4()),
            tool_id=tool_id,
            organization_id=usage_org["id"],
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            outcome=outcome,
        )

    totals = await ResourceUsageRepository(db_pool).totals_for_organization(
        usage_org["id"]
    )
    row = next(r for r in totals["by_tool"] if str(r["tool_id"]) == tool_id)
    assert row["invocations"] == 4
    assert row["succeeded"] == 2
