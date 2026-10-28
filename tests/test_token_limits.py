"""Unit tests for token limit decisions (no database).

The enforcement rules — which policy wins, how shared and per-person pools
differ, when a request is refused — are pure logic over a snapshot, so they are
tested here without a database. Ledger-backed behavior is covered by
``test_token_limits_integration.py``.
"""

from lucent.db.token_quota import (
    ALL_MODELS,
    DIMENSION_CACHED_INPUT,
    DIMENSION_OUTPUT,
    DIMENSION_RAW_INPUT,
    SCOPE_GLOBAL,
    SCOPE_INDIVIDUAL,
    QuotaPolicy,
)
from lucent.llm.token_quota import (
    QuotaSnapshot,
    QuotaViolation,
    TokenQuotaExceeded,
    _humanize_window,
    enrich_usage,
    estimate_tokens,
)


def _policy(**overrides) -> QuotaPolicy:
    defaults = dict(
        id="p1",
        organization_id="org",
        model_ids=("gpt-5.1",),
        name="Daily",
        description="",
        window_seconds=86400,
        scope_mode=SCOPE_INDIVIDUAL,
        raw_input_tokens_limit=None,
        cached_input_tokens_limit=None,
        output_tokens_limit=None,
        is_enabled=True,
    )
    defaults.update(overrides)
    return QuotaPolicy(**defaults)


def test_estimate_tokens_is_monotonic_and_ignores_blanks():
    assert estimate_tokens(None, "") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)
    assert estimate_tokens("a" * 400, "b" * 400) > estimate_tokens("a" * 400)


def test_enrich_usage_derives_uncached_input():
    """Providers report input inclusive of cache reads; raw input must subtract."""
    normalized = enrich_usage(
        {"input_tokens": 1000, "cache_read_tokens": 400, "output_tokens": 250}
    )
    assert normalized[DIMENSION_RAW_INPUT] == 600
    assert normalized[DIMENSION_CACHED_INPUT] == 400
    assert normalized[DIMENSION_OUTPUT] == 250


def test_enrich_usage_clamps_negative_raw_input():
    """A cache read larger than the input total must not go negative."""
    normalized = enrich_usage({"input_tokens": 100, "cache_read_tokens": 500})
    assert normalized[DIMENSION_RAW_INPUT] == 0
    assert normalized[DIMENSION_CACHED_INPUT] == 500


def test_enrich_usage_handles_missing_and_none_values():
    normalized = enrich_usage({})
    assert set(normalized.values()) == {0}


def test_humanize_window_reads_naturally():
    assert _humanize_window(3600) == "hour"
    assert _humanize_window(7200) == "2 hours"
    assert _humanize_window(86400) == "day"
    assert _humanize_window(172800) == "2 days"
    assert _humanize_window(604800) == "7 days"
    assert _humanize_window(1800) == "30 minutes"
    assert _humanize_window(90) == "90 seconds"


def test_empty_snapshot_is_open():
    snapshot = QuotaSnapshot()
    assert snapshot.is_open
    assert snapshot.dimension_capacity() == {}
    assert snapshot.violation_for({DIMENSION_RAW_INPUT: 10_000}) is None


def test_snapshot_reports_no_violation_within_budget():
    policy = _policy(output_tokens_limit=1000)
    snapshot = QuotaSnapshot(
        policies=(policy,),
        remaining={policy.id: {DIMENSION_OUTPUT: 1000}},
    )
    assert snapshot.violation_for({DIMENSION_OUTPUT: 999}) is None


def test_snapshot_reports_violation_when_budget_is_spent():
    policy = _policy(output_tokens_limit=1000, scope_mode=SCOPE_GLOBAL)
    snapshot = QuotaSnapshot(
        policies=(policy,),
        remaining={policy.id: {DIMENSION_OUTPUT: 1000}},
        used={(policy.id, DIMENSION_OUTPUT): 500},
    )
    violation = snapshot.violation_for({DIMENSION_OUTPUT: 1001})
    assert violation is not None
    assert violation.dimension == DIMENSION_OUTPUT
    assert violation.limit == 1000
    # used = already spent + this session's overshoot
    assert violation.used == 1501


def test_snapshot_ignores_unbounded_dimensions():
    policy = _policy(raw_input_tokens_limit=None, output_tokens_limit=10)
    snapshot = QuotaSnapshot(
        policies=(policy,),
        remaining={policy.id: {DIMENSION_RAW_INPUT: None, DIMENSION_OUTPUT: 10}},
    )
    assert snapshot.violation_for({DIMENSION_RAW_INPUT: 10**9}) is None


def test_dimension_capacity_takes_the_tightest_policy():
    """Several policies apply at once; the smallest remaining budget wins."""
    tight = _policy(id="tight", output_tokens_limit=100)
    loose = _policy(id="loose", output_tokens_limit=9000, model_ids=(ALL_MODELS,))
    snapshot = QuotaSnapshot(
        policies=(tight, loose),
        remaining={
            tight.id: {DIMENSION_OUTPUT: 100},
            loose.id: {DIMENSION_OUTPUT: 9000},
        },
    )
    assert snapshot.dimension_capacity() == {DIMENSION_OUTPUT: 100}


def test_violation_reports_the_first_offending_policy():
    first = _policy(id="first", name="First", output_tokens_limit=10)
    second = _policy(id="second", name="Second", output_tokens_limit=1000)
    snapshot = QuotaSnapshot(
        policies=(first, second),
        remaining={
            first.id: {DIMENSION_OUTPUT: 10},
            second.id: {DIMENSION_OUTPUT: 1000},
        },
    )
    violation = snapshot.violation_for({DIMENSION_OUTPUT: 50})
    assert violation is not None
    assert violation.policy_name == "First"


def test_policy_limit_lookup_and_limits_map():
    policy = _policy(raw_input_tokens_limit=100, cached_input_tokens_limit=None)
    assert policy.limit_for(DIMENSION_RAW_INPUT) == 100
    assert policy.limit_for(DIMENSION_CACHED_INPUT) is None
    # Only bounded dimensions appear, so the UI never renders a fake zero.
    assert policy.limits() == {DIMENSION_RAW_INPUT: 100}


def test_policy_all_models_sentinel():
    assert _policy(model_ids=(ALL_MODELS,)).applies_to_all_models
    assert not _policy().applies_to_all_models


def test_policy_covers_each_listed_model():
    """A multi-model policy applies to its models and nothing else."""
    policy = _policy(model_ids=("alpha", "beta"))
    assert policy.covers("alpha")
    assert policy.covers("beta")
    assert not policy.covers("gamma")
    assert not policy.applies_to_all_models


def test_policy_all_models_covers_everything():
    policy = _policy(model_ids=(ALL_MODELS,))
    assert policy.covers("anything")
    assert policy.label() == "all models"


def test_policy_label_describes_the_scope():
    assert _policy(model_ids=("only",)).label() == "model 'only'"
    assert _policy(model_ids=("a", "b", "c")).label() == "3 models"


def test_exception_message_is_actionable():
    error = TokenQuotaExceeded(
        policy_name="Daily allowance",
        dimension=DIMENSION_OUTPUT,
        limit=1_000_000,
        used=1_000_000,
        window_seconds=86400,
        scope_mode=SCOPE_INDIVIDUAL,
        model_ids=(ALL_MODELS,),
        requested=25_000,
    )
    message = str(error)
    # Names the policy, the model scope, the size, the pool and the window.
    assert "Daily allowance" in message
    assert "all models" in message
    assert "1,000,000" in message
    assert "per-person" in message
    assert "per day" in message
    # Tells the person how much more this request needs.
    assert "25,000" in message


def test_multi_model_exception_message_counts_rather_than_lists():
    """Six ids in a chat error would bury the useful number."""
    error = TokenQuotaExceeded(
        policy_name="Trio",
        dimension=DIMENSION_OUTPUT,
        limit=10,
        used=10,
        window_seconds=3600,
        scope_mode=SCOPE_GLOBAL,
        model_ids=("a", "b", "c", "d", "e", "f"),
    )
    message = str(error)
    assert "6 models" in message
    assert "'a'" not in message


def test_exception_model_id_stays_single_valued():
    """`model_id` remains a single string for existing API consumers."""
    single = TokenQuotaExceeded(
        policy_name="P", dimension=DIMENSION_OUTPUT, limit=1, used=1,
        window_seconds=3600, scope_mode=SCOPE_GLOBAL, model_ids=("one",),
    )
    assert single.model_id == "one"

    wide = TokenQuotaExceeded(
        policy_name="P", dimension=DIMENSION_OUTPUT, limit=1, used=1,
        window_seconds=3600, scope_mode=SCOPE_GLOBAL, model_ids=("a", "*"),
    )
    assert wide.model_id == ALL_MODELS


def test_violation_converts_to_exception_preserving_fields():
    violation = QuotaViolation(
        policy_id="p1",
        policy_name="Name",
        dimension=DIMENSION_CACHED_INPUT,
        limit=10,
        used=12,
        requested=2,
        window_seconds=3600,
        scope_mode=SCOPE_GLOBAL,
        model_ids=("model-x",),
    )
    error = violation.as_exception()
    assert error.limit == 10
    assert error.used == 12
    assert error.dimension == DIMENSION_CACHED_INPUT
    assert error.scope_mode == SCOPE_GLOBAL
    assert "shared" in str(error)
    assert "model-x" in str(error)


def test_wrapper_forwards_private_helpers_from_the_concrete_engine():
    """Callers reach engine-specific helpers through the wrapper unchanged."""
    from lucent.llm.engine import LLMEngine
    from lucent.llm.quota_engine import QuotaEnforcingEngine, enforce

    class Engine(LLMEngine):
        @property
        def name(self) -> str:
            return "langchain"

        async def run_session(self, **kwargs):  # pragma: no cover - unused
            return None

        async def run_session_streaming(self, **kwargs):  # pragma: no cover - unused
            return None

        async def cleanup(self) -> None:
            return None

        def _helper(self) -> str:
            return "concrete"

        public_flag = True

    wrapped = QuotaEnforcingEngine(Engine())
    assert wrapped._helper() == "concrete"
    assert wrapped.public_flag is True
    assert wrapped.name == "langchain"
    # enforce() is idempotent, so repeated registry lookups never nest wrappers.
    assert enforce(wrapped) is wrapped


def test_wrapper_missing_attribute_raises_attributeerror():
    """Attribute errors must stay normal rather than recursing."""
    import pytest as _pytest

    from lucent.llm.engine import LLMEngine
    from lucent.llm.quota_engine import QuotaEnforcingEngine

    class Engine(LLMEngine):
        @property
        def name(self) -> str:
            return "langchain"

        async def run_session(self, **kwargs):  # pragma: no cover - unused
            return None

        async def run_session_streaming(self, **kwargs):  # pragma: no cover - unused
            return None

        async def cleanup(self) -> None:
            return None

    wrapped = QuotaEnforcingEngine(Engine())
    with _pytest.raises(AttributeError):
        wrapped.does_not_exist
