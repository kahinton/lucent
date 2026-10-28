"""Integration tests for token limit policies: storage + enforcement.

Covers the behavior that only a real database can prove:

* policies and assignments round-trip and stay organization-scoped,
* the ledger aggregation partitions usage by user and by model,
* shared pools sum the whole subject set while per-person pools do not,
* preflight refuses a request that cannot fit and allows one that can,
* the tightest of several applicable policies wins,
* the engine wrapper is transparent and blocks through the registry.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from lucent.db import GroupRepository, TokenQuotaRepository
from lucent.db.token_usage import TokenUsageRepository
from lucent.llm.token_quota import (
    TokenLimitService,
    TokenQuotaExceeded,
    reset_token_limit_service,
)

HOUR = 3600
DAY = 86400


async def _record(
    pool,
    *,
    org_id,
    user_id,
    model,
    input_tokens=0,
    cache_read_tokens=0,
    output_tokens=0,
    created_at=None,
):
    """Insert one ledger row, optionally backdated.

    ``created_at`` is rewritten directly because the repository always stamps
    NOW(); window-boundary tests need rows outside the window.
    """
    from lucent.db.pool import scoped_acquire

    await TokenUsageRepository(pool).record(
        organization_id=org_id,
        user_id=user_id,
        session_id=None,
        turn_id=None,
        message_id=None,
        model=model,
        engine="langchain",
        usage={
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "output_tokens": output_tokens,
        },
    )
    if created_at is not None:
        async with scoped_acquire(organization_id=org_id) as conn:
            await conn.execute(
                """
                UPDATE llm_token_usage SET created_at = $1
                WHERE organization_id = $2 AND user_id = $3 AND model = $4
                  AND created_at > NOW() - INTERVAL '10 seconds'
                """,
                created_at,
                uuid.UUID(str(org_id)),
                uuid.UUID(str(user_id)),
                model,
            )


@pytest.fixture(autouse=True)
def _fresh_service():
    """Every test gets a service with no cached policies."""
    reset_token_limit_service()
    yield
    reset_token_limit_service()


# ── storage ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_round_trip_with_assignments(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)

    policy = await repo.create_policy(
        org_id,
        model_ids=["gpt-5.1"],
        name="Daily",
        window_seconds=DAY,
        scope_mode="individual",
        output_tokens_limit=1000,
        cached_input_tokens_limit=500,
        created_by=test_user["id"],
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    listing = await repo.list_policies(org_id)
    assert len(listing) == 1
    stored = listing[0]
    assert stored["output_tokens_limit"] == 1000
    assert stored["cached_input_tokens_limit"] == 500
    # Unbounded dimension stays NULL, never 0 — 0 would mean "no usage allowed".
    assert stored["raw_input_tokens_limit"] is None
    assert stored["scope_mode"] == "individual"
    assert len(stored["assignments"]) == 1
    assert stored["assignments"][0]["principal_type"] == "org"
    assert stored["assignments"][0]["principal_id"] is None


@pytest.mark.asyncio
async def test_unbounded_dimension_is_distinct_from_zero(db_pool, test_user, test_organization):
    """A zero limit is a hard stop; NULL is unlimited. They must not collapse."""
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id,
        model_ids=["m"],
        name="Blocked",
        window_seconds=DAY,
        scope_mode="individual",
        output_tokens_limit=0,
    )
    stored = await repo.get_policy(policy["id"], organization_id=org_id)
    assert stored["output_tokens_limit"] == 0
    assert stored["raw_input_tokens_limit"] is None


@pytest.mark.asyncio
async def test_policy_with_no_limits_is_rejected_by_the_database(
    db_pool, test_user, test_organization
):
    """A policy that caps nothing would silently do nothing; refuse it."""
    org_id = test_organization["id"]
    with pytest.raises(Exception) as excinfo:
        await TokenQuotaRepository(db_pool).create_policy(
            org_id,
            model_ids=["m"],
            name="Empty",
            window_seconds=DAY,
            scope_mode="global",
        )
    assert "some_limit_required" in str(excinfo.value) or "check" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_duplicate_policy_name_is_rejected(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    await repo.create_policy(
        org_id,
        model_ids=["m"],
        name="Dup",
        window_seconds=DAY,
        scope_mode="global",
        output_tokens_limit=10,
    )
    with pytest.raises(Exception) as excinfo:
        await repo.create_policy(
            org_id,
            model_ids=["m"],
            name="Dup",
            window_seconds=DAY,
            scope_mode="global",
            output_tokens_limit=20,
        )
    assert "unique" in str(excinfo.value).lower()

    # Names are unique per organization, not per model: a single policy can now
    # span several models, so reusing the name over a different model would be
    # an accidental duplicate rather than a legitimate separate budget.
    with pytest.raises(Exception):
        await repo.create_policy(
            org_id,
            model_ids=["m2"],
            name="Dup",
            window_seconds=DAY,
            scope_mode="global",
            output_tokens_limit=20,
        )


@pytest.mark.asyncio
async def test_assignment_and_removal(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id,
        model_ids=["m"],
        name="P",
        window_seconds=DAY,
        scope_mode="global",
        output_tokens_limit=10,
    )
    assigned = await repo.assign(
        policy["id"],
        organization_id=org_id,
        principal_type="user",
        principal_id=test_user["id"],
    )
    assert assigned["id"]
    assert await repo.unassign(assigned["id"], organization_id=org_id) is True
    assert await repo.unassign(assigned["id"], organization_id=org_id) is False
    listing = await repo.list_policies(org_id)
    assert listing[0]["assignments"] == []


@pytest.mark.asyncio
async def test_assign_is_idempotent(db_pool, test_user, test_organization):
    """A double submit must not fail or duplicate the assignment."""
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id,
        model_ids=["m"],
        name="P",
        window_seconds=DAY,
        scope_mode="global",
        output_tokens_limit=10,
    )
    first = await repo.assign(policy["id"], organization_id=org_id, principal_type="org")
    second = await repo.assign(policy["id"], organization_id=org_id, principal_type="org")
    assert first["id"] == second["id"]
    listing = await repo.list_policies(org_id)
    assert len(listing[0]["assignments"]) == 1


@pytest.mark.asyncio
async def test_policies_are_organization_scoped(db_pool, test_user, test_organization):
    from lucent.db import OrganizationRepository

    other_org = await OrganizationRepository(db_pool).create(
        name=f"{uuid.uuid4().hex[:10]}-other"
    )
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        test_organization["id"],
        model_ids=["m"],
        name="Mine",
        window_seconds=DAY,
        scope_mode="global",
        output_tokens_limit=10,
    )
    try:
        assert await repo.list_policies(other_org["id"]) == []
        assert await repo.get_policy(policy["id"], organization_id=other_org["id"]) is None
        # Deletion is organization-predicated too.
        assert await repo.delete_policy(policy["id"], organization_id=other_org["id"]) is False
    finally:
        await repo.delete_policy(policy["id"], organization_id=test_organization["id"])
        from lucent.db import get_pool

        async with (await get_pool()).acquire() as conn:
            await conn.execute("DELETE FROM organizations WHERE id = $1", other_org["id"])


@pytest.mark.asyncio
async def test_update_only_touches_allowed_columns(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id,
        model_ids=["m"],
        name="P",
        window_seconds=DAY,
        scope_mode="global",
        output_tokens_limit=10,
    )
    updated = await repo.update_policy(
        policy["id"],
        organization_id=org_id,
        name="Renamed",
        output_tokens_limit=99,
        organization_id_override="ignored",
        id="ignored",
    )
    assert updated["name"] == "Renamed"
    assert updated["output_tokens_limit"] == 99
    assert updated["id"] == policy["id"]


# ── ledger aggregation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_aggregation_partitions_by_user_and_dimension(
    db_pool, test_user, test_organization
):
    from lucent.db import UserRepository

    org_id = test_organization["id"]
    second = await UserRepository(db_pool).create(
        external_id=f"{uuid.uuid4().hex[:10]}-u2",
        provider="local",
        organization_id=org_id,
        email=f"{uuid.uuid4().hex[:10]}@test.com",
        display_name="Second",
    )
    await _record(
        db_pool,
        org_id=org_id,
        user_id=test_user["id"],
        model="m",
        input_tokens=1000,
        cache_read_tokens=400,
        output_tokens=100,
    )
    await _record(
        db_pool,
        org_id=org_id,
        user_id=second["id"],
        model="m",
        input_tokens=500,
        cache_read_tokens=0,
        output_tokens=50,
    )

    usage = await TokenQuotaRepository(db_pool).usage_for_window(
        org_id, window_seconds=DAY, model_ids=("m",)
    )
    assert usage.raw_input_tokens == 1100  # (1000-400) + 500
    assert usage.cached_input_tokens == 400
    assert usage.output_tokens == 150
    assert usage.by_user[str(test_user["id"])]["raw_input"] == 600
    assert usage.by_user[str(second["id"])]["output"] == 50


@pytest.mark.asyncio
async def test_usage_aggregation_respects_window_and_model(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    old = datetime.now(timezone.utc) - timedelta(days=3)
    await _record(
        db_pool, org_id=org_id, user_id=test_user["id"], model="m", output_tokens=1000, created_at=old
    )
    await _record(db_pool, org_id=org_id, user_id=test_user["id"], model="m", output_tokens=5)
    await _record(db_pool, org_id=org_id, user_id=test_user["id"], model="other", output_tokens=7)

    repo = TokenQuotaRepository(db_pool)
    day = await repo.usage_for_window(org_id, window_seconds=DAY, model_ids=("m",))
    assert day.output_tokens == 5

    all_models = await repo.usage_for_window(org_id, window_seconds=DAY, model_ids=("*",))
    assert all_models.output_tokens == 12

    week = await repo.usage_for_window(org_id, window_seconds=7 * DAY, model_ids=("m",))
    assert week.output_tokens == 1005


@pytest.mark.asyncio
async def test_usage_aggregation_sums_only_the_policy_models(db_pool, test_user, test_organization):
    """A multi-model policy pools exactly its models and ignores the rest."""
    org_id = test_organization["id"]
    for model, tokens in (("a", 10), ("b", 20), ("c", 40)):
        await _record(
            db_pool, org_id=org_id, user_id=test_user["id"], model=model, output_tokens=tokens
        )

    usage = await TokenQuotaRepository(db_pool).usage_for_window(
        org_id, window_seconds=DAY, model_ids=["a", "b"]
    )
    assert usage.output_tokens == 30

    # The sentinel anywhere in the list widens the pool to every model.
    everything = await TokenQuotaRepository(db_pool).usage_for_window(
        org_id, window_seconds=DAY, model_ids=["a", "*"]
    )
    assert everything.output_tokens == 70


# ── policy resolution ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolution_matches_org_group_and_user_assignments(
    db_pool, test_user, test_organization
):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    group_repo = GroupRepository(db_pool)
    group = await group_repo.create_group(
        name=f"{uuid.uuid4().hex[:8]}-team",
        description="",
        org_id=str(org_id),
        created_by=str(test_user["id"]),
    )
    await group_repo.add_member(
        str(group["id"]), str(test_user["id"]), organization_id=str(org_id)
    )

    org_policy = await repo.create_policy(
        org_id, model_ids=["*"], name="Org", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=100,
    )
    group_policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Group", window_seconds=DAY,
        scope_mode="global", output_tokens_limit=200,
    )
    user_policy = await repo.create_policy(
        org_id, model_ids=["m"], name="User", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=300,
    )
    other_model = await repo.create_policy(
        org_id, model_ids=["different"], name="Other", window_seconds=DAY,
        scope_mode="global", output_tokens_limit=400,
    )

    await repo.assign(org_policy["id"], organization_id=org_id, principal_type="org")
    await repo.assign(
        group_policy["id"], organization_id=org_id,
        principal_type="group", principal_id=group["id"],
    )
    await repo.assign(
        user_policy["id"], organization_id=org_id,
        principal_type="user", principal_id=test_user["id"],
    )
    await repo.assign(other_model["id"], organization_id=org_id, principal_type="org")

    resolved = await repo.resolve_policies(org_id, test_user["id"], "m")
    names = {entry.policy.name for entry in resolved}
    # The org wide policy matches via '*'; group and user match by id; the
    # policy scoped to a different model must not apply.
    assert names == {"Org", "Group", "User"}

    # A model with no specific policy still picks up the all-models one.
    resolved_other = await repo.resolve_policies(org_id, test_user["id"], "unlisted")
    assert {entry.policy.name for entry in resolved_other} == {"Org"}


@pytest.mark.asyncio
async def test_multi_model_policy_resolves_for_each_listed_model(
    db_pool, test_user, test_organization
):
    """One policy covering several models applies to each of them, and only them."""
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["alpha", "beta", "gamma"], name="Trio", window_seconds=DAY,
        scope_mode="global", output_tokens_limit=500,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    for model in ("alpha", "beta", "gamma"):
        resolved = await repo.resolve_policies(org_id, test_user["id"], model)
        assert [entry.policy.name for entry in resolved] == ["Trio"], model

    # A model the policy does not name is not covered.
    assert await repo.resolve_policies(org_id, test_user["id"], "delta") == []


@pytest.mark.asyncio
async def test_multi_model_policy_shares_one_pool_across_its_models(
    db_pool, test_user, test_organization
):
    """The point of a multi-model policy: usage on any listed model draws it down."""
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["alpha", "beta"], name="Trio", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=300,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    # 200 tokens on alpha plus 200 on beta is 400 against one 300 budget, so the
    # next request on either model must be refused.
    await _record(db_pool, org_id=org_id, user_id=test_user["id"], model="alpha",
                  output_tokens=200)
    await _record(db_pool, org_id=org_id, user_id=test_user["id"], model="beta",
                  output_tokens=200)

    service = TokenLimitService(db_pool)
    with pytest.raises(TokenQuotaExceeded) as excinfo:
        await service.preflight(
            organization_id=org_id, user_id=test_user["id"], model_id="beta", prompt="hi"
        )
    assert excinfo.value.used == 400
    assert excinfo.value.policy_name == "Trio"

    # A model outside the policy is unaffected by that pool.
    outside = await service.preflight(
        organization_id=org_id, user_id=test_user["id"], model_id="alpha-2", prompt="hi"
    )
    assert outside.is_open


@pytest.mark.asyncio
async def test_disabled_policies_do_not_resolve(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["*"], name="Off", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=10,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")
    await repo.update_policy(policy["id"], organization_id=org_id, is_enabled=False)
    assert await repo.resolve_policies(org_id, test_user["id"], "m") == []


# ── enforcement ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preflight_allows_when_no_policies_exist(db_pool, test_user, test_organization):
    service = TokenLimitService(db_pool)
    snapshot = await service.preflight(
        organization_id=test_organization["id"],
        user_id=test_user["id"],
        model_id="m",
        prompt="hello",
    )
    assert snapshot.is_open


@pytest.mark.asyncio
async def test_preflight_refuses_when_raw_input_budget_cannot_fit(
    db_pool, test_user, test_organization
):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Tiny", window_seconds=DAY,
        scope_mode="individual", raw_input_tokens_limit=100,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    service = TokenLimitService(db_pool)
    with pytest.raises(TokenQuotaExceeded) as excinfo:
        await service.preflight(
            organization_id=org_id,
            user_id=test_user["id"],
            model_id="m",
            # 4000 chars ≈ 1000 tokens, far beyond the 100 the policy allows.
            prompt="x" * 4000,
        )
    error = excinfo.value
    assert error.policy_name == "Tiny"
    assert error.dimension == "raw_input"
    assert error.limit == 100


@pytest.mark.asyncio
async def test_preflight_refuses_when_budget_already_spent(
    db_pool, test_user, test_organization
):
    org_id = test_organization["id"]
    await _record(
        db_pool, org_id=org_id, user_id=test_user["id"], model="m", output_tokens=600
    )
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Spent", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=500,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    service = TokenLimitService(db_pool)
    with pytest.raises(TokenQuotaExceeded) as excinfo:
        await service.preflight(
            organization_id=org_id, user_id=test_user["id"], model_id="m", prompt="hi"
        )
    assert excinfo.value.dimension == "output"
    assert excinfo.value.used == 600


@pytest.mark.asyncio
async def test_shared_pool_sums_members_but_per_person_does_not(
    db_pool, test_user, test_organization
):
    """The defining difference between the two scope modes."""
    from lucent.db import UserRepository

    org_id = test_organization["id"]
    second = await UserRepository(db_pool).create(
        external_id=f"{uuid.uuid4().hex[:10]}-shared",
        provider="local",
        organization_id=org_id,
        email=f"{uuid.uuid4().hex[:10]}@test.com",
        display_name="Member Two",
    )
    for user_id in (test_user["id"], second["id"]):
        await _record(
            db_pool, org_id=org_id, user_id=user_id, model="shared", output_tokens=400
        )

    repo = TokenQuotaRepository(db_pool)
    shared = await repo.create_policy(
        org_id, model_ids=["shared"], name="Shared", window_seconds=DAY,
        scope_mode="global", output_tokens_limit=700,
    )
    per_person = await repo.create_policy(
        org_id, model_ids=["shared"], name="Each", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=700,
    )
    for policy in (shared, per_person):
        await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    service = TokenLimitService(db_pool)

    # 800 tokens are used org-wide. The shared pool of 700 is therefore
    # exhausted, while each person's own 400 is still under the per-person 700.
    with pytest.raises(TokenQuotaExceeded) as excinfo:
        await service.preflight(
            organization_id=org_id, user_id=test_user["id"], model_id="shared", prompt="hi"
        )
    assert excinfo.value.policy_name == "Shared"
    assert excinfo.value.used == 800

    # Disable the shared policy: the per-person policy alone permits the call,
    # proving the two modes were evaluated differently.
    await repo.update_policy(shared["id"], organization_id=org_id, is_enabled=False)
    reset_token_limit_service()
    service = TokenLimitService(db_pool)
    snapshot = await service.preflight(
        organization_id=org_id, user_id=test_user["id"], model_id="shared", prompt="hi"
    )
    assert not snapshot.is_open
    assert [p.name for p in snapshot.policies] == ["Each"]


@pytest.mark.asyncio
async def test_tightest_applicable_policy_wins(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    loose = await repo.create_policy(
        org_id, model_ids=["*"], name="Loose", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=10_000,
    )
    tight = await repo.create_policy(
        org_id, model_ids=["m"], name="Tight", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=100,
    )
    for policy in (loose, tight):
        await repo.assign(policy["id"], organization_id=org_id, principal_type="org")
    await _record(
        db_pool, org_id=org_id, user_id=test_user["id"], model="m", output_tokens=150
    )

    service = TokenLimitService(db_pool)
    with pytest.raises(TokenQuotaExceeded) as excinfo:
        await service.preflight(
            organization_id=org_id, user_id=test_user["id"], model_id="m", prompt="hi"
        )
    assert excinfo.value.policy_name == "Tight"


@pytest.mark.asyncio
async def test_user_specific_policy_does_not_leak_to_other_users(
    db_pool, test_user, test_organization
):
    from lucent.db import UserRepository

    org_id = test_organization["id"]
    other = await UserRepository(db_pool).create(
        external_id=f"{uuid.uuid4().hex[:10]}-nospec",
        provider="local",
        organization_id=org_id,
        email=f"{uuid.uuid4().hex[:10]}@test.com",
        display_name="Unaffected",
    )
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="OnlyMe", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=0,
    )
    await repo.assign(
        policy["id"], organization_id=org_id,
        principal_type="user", principal_id=test_user["id"],
    )

    service = TokenLimitService(db_pool)
    # A zero limit blocks the assigned user outright.
    with pytest.raises(TokenQuotaExceeded):
        await service.preflight(
            organization_id=org_id, user_id=test_user["id"], model_id="m", prompt="hi"
        )
    # An unassigned colleague is untouched.
    snapshot = await service.preflight(
        organization_id=org_id, user_id=other["id"], model_id="m", prompt="hi"
    )
    assert snapshot.is_open


@pytest.mark.asyncio
async def test_master_switch_disables_enforcement(
    db_pool, test_user, test_organization, monkeypatch
):
    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Off1", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=0,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    monkeypatch.setattr("lucent.llm.token_quota.TokenLimitService.is_enabled", staticmethod(lambda: False))
    service = TokenLimitService(db_pool)
    snapshot = await service.preflight(
        organization_id=org_id, user_id=test_user["id"], model_id="m", prompt="hi"
    )
    assert snapshot.is_open


# ── engine wrapper ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_wraps_engines_without_changing_identity():
    from lucent.llm import engine_registry
    from lucent.llm.quota_engine import QuotaEnforcingEngine

    engine = engine_registry.get_engine("langchain")
    assert isinstance(engine, QuotaEnforcingEngine)
    # Transparent: name and attribute forwarding must be unchanged.
    assert engine.name == "langchain"
    assert engine.wrapped.name == "langchain"
    # Wrapping twice must not nest.
    assert engine_registry.get_engine("langchain") is engine


@pytest.mark.asyncio
async def test_engine_wrapper_blocks_before_delegating(
    db_pool, test_user, test_organization
):
    """A refused request never reaches the wrapped engine."""
    from lucent.llm.engine import LLMEngine
    from lucent.llm.quota_engine import QuotaEnforcingEngine

    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Block", window_seconds=DAY,
        scope_mode="individual", raw_input_tokens_limit=1,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    calls = []

    class FakeEngine(LLMEngine):
        @property
        def name(self) -> str:
            return "langchain"

        async def run_session(self, **kwargs):
            calls.append(kwargs)
            return "should not happen"

        async def run_session_streaming(self, **kwargs):
            calls.append(kwargs)
            return "should not happen"

        async def cleanup(self) -> None:
            return None

    wrapped = QuotaEnforcingEngine(FakeEngine())
    with pytest.raises(TokenQuotaExceeded):
        await wrapped.run_session(
            model="m",
            system_message="",
            prompt="x" * 400,
            audit_context={"organization_id": org_id, "user_id": test_user["id"]},
        )
    assert calls == []


@pytest.mark.asyncio
async def test_engine_wrapper_passes_through_without_policies(db_pool, test_user, test_organization):
    from lucent.llm.engine import LLMEngine
    from lucent.llm.quota_engine import QuotaEnforcingEngine

    calls = []

    class FakeEngine(LLMEngine):
        @property
        def name(self) -> str:
            return "langchain"

        async def run_session(self, **kwargs):
            calls.append("run_session")
            return "ok"

        async def run_session_streaming(self, **kwargs):
            calls.append("stream")
            return "ok"

        async def cleanup(self) -> None:
            return None

    wrapped = QuotaEnforcingEngine(FakeEngine())
    result = await wrapped.run_session(
        model="m",
        system_message="",
        prompt="hi",
        audit_context={
            "organization_id": test_organization["id"],
            "user_id": test_user["id"],
        },
    )
    assert result == "ok"
    assert calls == ["run_session"]


@pytest.mark.asyncio
async def test_streaming_session_is_stopped_when_budget_runs_out(
    db_pool, test_user, test_organization
):
    """A session that overspends mid-stream is cancelled, not completed."""
    import asyncio

    from lucent.llm.engine import LLMEngine, SessionEvent, SessionEventType
    from lucent.llm.quota_engine import QuotaEnforcingEngine

    org_id = test_organization["id"]
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Cap", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=100,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    class SpendingEngine(LLMEngine):
        @property
        def name(self) -> str:
            return "langchain"

        async def run_session(self, **kwargs):
            return "ok"

        async def cleanup(self) -> None:
            return None

        async def run_session_streaming(self, on_event=None, **kwargs):
            for _ in range(50):
                if on_event:
                    on_event(
                        SessionEvent(
                            type=SessionEventType.USAGE,
                            usage={"input_tokens": 10, "output_tokens": 50},
                        )
                    )
                await asyncio.sleep(0)
            return "finished anyway"

    wrapped = QuotaEnforcingEngine(SpendingEngine())
    with pytest.raises(TokenQuotaExceeded) as excinfo:
        await wrapped.run_session_streaming(
            model="m",
            system_message="",
            prompt="hi",
            audit_context={"organization_id": org_id, "user_id": test_user["id"]},
        )
    assert excinfo.value.dimension == "output"
    assert excinfo.value.limit == 100


@pytest.mark.asyncio
async def test_post_check_reports_overshoot(db_pool, test_user, test_organization):
    org_id = test_organization["id"]
    await _record(
        db_pool, org_id=org_id, user_id=test_user["id"], model="m", output_tokens=900
    )
    repo = TokenQuotaRepository(db_pool)
    policy = await repo.create_policy(
        org_id, model_ids=["m"], name="Late", window_seconds=DAY,
        scope_mode="individual", output_tokens_limit=500,
    )
    await repo.assign(policy["id"], organization_id=org_id, principal_type="org")

    service = TokenLimitService(db_pool)
    violation = await service.post_check(
        organization_id=org_id,
        user_id=test_user["id"],
        model_id="m",
        usage={"output_tokens": 0},
    )
    assert violation is not None
    assert violation.used == 900
