"""Pre-request token limit enforcement.

Every LLM request in Lucent — chat, daemon tasks, sub-agent reports, the
general LLM layer, the coding engines — passes through this module before it is
allowed to run. The rules themselves live in ``token_quota_policies`` (152); this
module decides whether a request may proceed and, where it can, stops a session
mid-flight once a budget is exhausted.

Why a wrapper and not call-site checks
--------------------------------------
There are four engines and a dozen call sites. Enforcing at each one would
guarantee that the day someone adds a fifth engine or a new entry point, the
limit silently stops applying. Instead :mod:`lucent.llm.quota_engine` wraps
every engine returned by the registry, so a new engine inherits enforcement for
free and there is exactly one place to reason about.

Three enforcement points
------------------------
1. **Preflight** (before the request). The prompt and system message are
   estimated and compared against what is left of each policy's raw-input
   budget. If the estimate alone cannot fit, the request is refused and nothing
   is spent. Output and cached-input budgets are checked for *already* being
   exhausted, which is the case a caller can actually be blocked by up front
   (their future size is unknown).
2. **Mid-stream** (during the request). Usage events are accumulated in memory
   and compared against the remaining budget snapshotted at preflight. This is
   deliberately DB-free so it can run inside a synchronous event callback, and
   it is what stops a runaway tool loop from spending an entire pool in one
   session.
3. **Post-session** (afterwards). The real, provider-reported totals are
   re-checked against the ledger for the log and for the next request, which
   catches concurrent overshoot that the in-memory snapshot could not see.

Accounting
----------
Consumption is read from ``llm_token_usage`` — the same ledger the Usage report
renders. Quota figures can therefore never disagree with reported usage. Within
a policy, cached prompt tokens are counted separately from uncached ones so a
policy can cap either independently.

Caching
-------
Policy *definitions* are cached for a few seconds (they change rarely, and the
daemon runs in a separate process, so the TTL is what bounds propagation).
Consumption is never cached. An organization with no enabled policies short-
circuits before touching the database at all, so the feature costs nothing until
an administrator actually configures a limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from lucent.db.token_quota import (
    ALL_MODELS,
    DIMENSION_CACHED_INPUT,
    DIMENSION_OUTPUT,
    DIMENSION_RAW_INPUT,
    DIMENSIONS,
    PRINCIPAL_GROUP,
    PRINCIPAL_ORG,
    PRINCIPAL_USER,
    SCOPE_GLOBAL,
    SCOPE_INDIVIDUAL,
    PoolUsage,
    QuotaAssignment,
    QuotaPolicy,
    ResolvedPolicy,
    TokenQuotaRepository,
)

logger = logging.getLogger(__name__)

DIMENSION_LABELS = {
    DIMENSION_RAW_INPUT: "uncached input tokens",
    DIMENSION_CACHED_INPUT: "cached input tokens",
    DIMENSION_OUTPUT: "output tokens",
}

# Rough char-per-token divisor used only for the preflight estimate. Matches
# ``lucent.llm.model_auto.estimate_prompt_tokens`` so the two never disagree
# about how big a prompt is.
_CHARS_PER_TOKEN = 4


class TokenQuotaExceeded(Exception):
    """A token budget would be exceeded by (or during) a request.

    Carries enough structure for the caller to render a useful message and for
    an API layer to map it onto a status code, without the caller having to
    re-read the policy.
    """

    def __init__(
        self,
        *,
        policy_name: str,
        dimension: str,
        limit: int,
        used: int,
        window_seconds: int,
        scope_mode: str,
        model_ids: Iterable[str],
        requested: int = 0,
    ) -> None:
        self.policy_name = policy_name
        self.dimension = dimension
        self.limit = limit
        self.used = used
        self.window_seconds = window_seconds
        self.scope_mode = scope_mode
        self.model_ids = tuple(model_ids)
        self.requested = requested
        super().__init__(self.message())

    @property
    def model_id(self) -> str:
        """Single-model scope, or the all-models sentinel.

        Kept for API consumers (`detail.model` in the chat error payload) that
        want one string when the policy covers exactly one model or everything.
        """
        if len(self.model_ids) == 1:
            return self.model_ids[0]
        return ALL_MODELS if ALL_MODELS in self.model_ids else ""

    def message(self) -> str:
        window = _humanize_window(self.window_seconds)
        pool = "shared" if self.scope_mode == SCOPE_GLOBAL else "per-person"
        model = _describe_model_scope(self.model_ids)
        label = DIMENSION_LABELS.get(self.dimension, self.dimension)
        detail = (
            f"Token limit reached for {model}: policy '{self.policy_name}' allows "
            f"{self.limit:,} {label} per {window} ({pool} pool) and "
            f"{self.used:,} have been used."
        )
        if self.requested:
            detail += f" This request needs about {self.requested:,} more."
        return detail


def _humanize_window(seconds: int) -> str:
    seconds = max(int(seconds or 0), 0)
    if seconds % 86400 == 0 and seconds >= 86400:
        days = seconds // 86400
        return "day" if days == 1 else f"{days} days"
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return "hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return "minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def estimate_tokens(*texts: str | None) -> int:
    """Rough prompt size in tokens.

    Intentionally crude and monotonic: it exists to refuse a request that
    obviously cannot fit, not to bill anyone. Under-estimating only means the
    request runs and the mid-stream check catches the overrun; over-estimating
    would refuse legitimate work, so the divisor is the conservative one used
    elsewhere in the codebase.
    """
    return sum(len(text) for text in texts if text) // _CHARS_PER_TOKEN


def _describe_model_scope(model_ids: Iterable[str]) -> str:
    """Readable model scope for a message.

    "all models" for the sentinel, the quoted id for a single model, and a
    count for a multi-model policy — listing six ids in a chat error would bury
    the number the person actually needs.
    """
    models = tuple(str(model) for model in model_ids)
    if ALL_MODELS in models or not models:
        return "all models"
    if len(models) == 1:
        return f"model '{models[0]}'"
    return f"{len(models)} models"


@dataclass(frozen=True, slots=True)
class QuotaViolation:
    """One dimension of one policy that is exhausted or would be."""

    policy_id: str
    policy_name: str
    dimension: str
    limit: int
    used: int
    requested: int
    window_seconds: int
    scope_mode: str
    model_ids: tuple[str, ...]

    def as_exception(self) -> TokenQuotaExceeded:
        return TokenQuotaExceeded(
            policy_name=self.policy_name,
            dimension=self.dimension,
            limit=self.limit,
            used=self.used,
            window_seconds=self.window_seconds,
            scope_mode=self.scope_mode,
            model_ids=self.model_ids,
            requested=self.requested,
        )


@dataclass(slots=True)
class QuotaSnapshot:
    """Remaining budget per policy, captured at preflight.

    Held for the lifetime of one LLM session so the mid-stream check can run
    without touching the database.
    """

    policies: tuple[QuotaPolicy, ...] = ()
    # policy_id -> dimension -> remaining tokens (None = unbounded)
    remaining: dict[str, dict[str, int | None]] = field(default_factory=dict)
    # usage already counted toward the pool at snapshot time
    used: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        """True when no policy constrains this request."""
        return not self.policies

    def dimension_capacity(self) -> dict[str, int]:
        """Total remaining tokens per dimension across all policies.

        The minimum is what matters: a request is limited by whichever budget
        runs out first, so the tightest remaining value is the real ceiling.
        """
        capacity: dict[str, int] = {}
        for policy_remaining in self.remaining.values():
            for dimension, value in policy_remaining.items():
                if value is None:
                    continue
                current = capacity.get(dimension)
                capacity[dimension] = value if current is None else min(current, value)
        return capacity

    def violation_for(self, accumulated: dict[str, int]) -> QuotaViolation | None:
        """First violation caused by this session's own consumption."""
        for policy in self.policies:
            policy_remaining = self.remaining.get(policy.id) or {}
            for dimension in DIMENSIONS:
                remaining = policy_remaining.get(dimension)
                if remaining is None:
                    continue
                spent = int(accumulated.get(dimension, 0))
                if spent > remaining:
                    limit = policy.limit_for(dimension) or 0
                    used = self.used.get((policy.id, dimension), 0)
                    return QuotaViolation(
                        policy_id=policy.id,
                        policy_name=policy.name,
                        dimension=dimension,
                        limit=limit,
                        used=used + spent,
                        requested=spent,
                        window_seconds=policy.window_seconds,
                        scope_mode=policy.scope_mode,
                        model_ids=policy.model_ids,
                    )
        return None


class _ReservationLedger:
    """Best-effort in-process accounting for requests currently in flight.

    Two requests starting at the same instant both pass a ledger-only preflight
    and together overshoot. Recording the estimate as a reservation while a
    request runs closes most of that window. It is process-local by design: the
    daemon and the server are separate processes, and a cross-process ledger
    would need a database round trip on every check, which is the cost this
    module exists to avoid. Post-session reconciliation catches the rest.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], list[tuple[float, int]]] = {}
        self._lock = asyncio.Lock()

    async def add(
        self, organization_id: str, user_id: str, model_id: str, tokens: int, ttl: int
    ) -> None:
        if tokens <= 0:
            return
        key = (str(organization_id), str(user_id), str(model_id))
        async with self._lock:
            self._prune_locked()
            self._entries.setdefault(key, []).append((time.monotonic() + ttl, tokens))

    async def totals(
        self,
        organization_id: str,
        user_ids: Iterable[str] | set[str] | None,
        model_ids: set[str] | None,
    ) -> int:
        """Sum live reservations.

        ``model_ids`` of ``None`` means "every model", which is how an
        all-models policy must see in-flight work: a reservation placed against
        ``gpt-5.1`` still counts toward a budget that covers every model.
        """
        async with self._lock:
            self._prune_locked()
            wanted_models = None if model_ids is None else {str(m) for m in model_ids}
            now = time.monotonic()
            total = 0
            for (org, user, model), entries in self._entries.items():
                if org != str(organization_id):
                    continue
                if wanted_models is not None and model not in wanted_models:
                    continue
                if user_ids is not None and user not in user_ids:
                    continue
                total += sum(tokens for expires, tokens in entries if expires > now)
            return total

    async def release(self, keys: list[tuple[str, str, str]]) -> None:
        async with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for key in list(self._entries):
            live = [entry for entry in self._entries[key] if entry[0] > now]
            if live:
                self._entries[key] = live
            else:
                del self._entries[key]


@dataclass(slots=True)
class _OrgPolicyCache:
    """A short-lived cache of policy definitions for one organization."""

    policies: list[dict[str, Any]]
    expires_at: float


class TokenLimitService:
    """Decides whether an LLM request may run, and tracks its consumption."""

    def __init__(self, pool: Any = None) -> None:
        self._pool = pool
        self._repo: TokenQuotaRepository | None = None
        self._policy_cache: dict[str, _OrgPolicyCache] = {}
        self._reservations = _ReservationLedger()

    # ── plumbing ───────────────────────────────────────────────────────────

    async def _repository(self) -> TokenQuotaRepository:
        if self._repo is None:
            pool = self._pool
            if pool is None:
                from lucent.db import get_pool

                pool = await get_pool()
            self._repo = TokenQuotaRepository(pool)
        return self._repo

    @staticmethod
    def _cache_ttl() -> int:
        try:
            from lucent.settings import token_limits_cache_ttl_seconds

            return max(int(token_limits_cache_ttl_seconds()), 0)
        except Exception:  # noqa: BLE001 — a settings read must never block a request
            return 15

    @staticmethod
    def is_enabled() -> bool:
        try:
            from lucent.settings import token_limits_enabled

            return bool(token_limits_enabled())
        except Exception:  # noqa: BLE001
            return True

    def invalidate(self, organization_id: str | None = None) -> None:
        """Drop cached policy definitions.

        Called by every write path in the admin UI. The TTL covers the daemon
        process, which cannot see this call.
        """
        if organization_id is None:
            self._policy_cache.clear()
        else:
            self._policy_cache.pop(str(organization_id), None)

    async def _org_policies(self, organization_id: str) -> list[dict[str, Any]]:
        ttl = self._cache_ttl()
        key = str(organization_id)
        cached = self._policy_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached.expires_at > now:
            return cached.policies
        repo = await self._repository()
        policies = await repo.list_policies(organization_id, include_disabled=False)
        self._policy_cache[key] = _OrgPolicyCache(
            policies=policies, expires_at=now + ttl
        )
        return policies

    async def has_policies(self, organization_id: str) -> bool:
        """Cheap gate: does this organization configure any limit at all?"""
        try:
            return bool(await self._org_policies(organization_id))
        except Exception:  # noqa: BLE001 — never fail a request on a lookup error
            logger.warning(
                "Token limit policy lookup failed; allowing request", exc_info=True
            )
            return False

    # ── preflight ──────────────────────────────────────────────────────────

    async def preflight(
        self,
        *,
        organization_id: str | None,
        user_id: str | None,
        model_id: str,
        system_message: str | None = None,
        prompt: str | None = None,
        extra_text: Iterable[str | None] = (),
        enforce: bool = True,
        reserve: bool = True,
    ) -> QuotaSnapshot:
        """Check the request against every applicable policy.

        Returns a snapshot the caller keeps for mid-stream checks. Raises
        :class:`TokenQuotaExceeded` when the request must not run.

        ``reserve=False`` performs a read-only check with no in-flight
        accounting. API layers use it to fail fast with a proper HTTP error
        before streaming begins; the engine wrapper then runs the authoritative
        pass that reserves budget for the session's duration. Calling both with
        ``reserve=True`` would double-count the estimate.

        A missing organization or user means a system-level call with no
        attributable principal; those pass through unenforced (and are logged),
        because failing closed would break bootstrap and maintenance work that
        has no budget owner.
        """
        if not enforce or not self.is_enabled():
            return QuotaSnapshot()
        if not organization_id:
            return QuotaSnapshot()

        try:
            if not await self.has_policies(organization_id):
                return QuotaSnapshot()
            repo = await self._repository()
            resolved = (
                await repo.resolve_policies(organization_id, user_id, model_id)
                if user_id
                else self._org_wide_policies(await self._org_policies(organization_id), model_id)
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Token limit preflight lookup failed; allowing request", exc_info=True
            )
            return QuotaSnapshot()

        if not resolved:
            return QuotaSnapshot()

        estimated = estimate_tokens(system_message, prompt, *extra_text)
        usage_cache: dict[tuple[int, frozenset[str]], PoolUsage] = {}
        remaining: dict[str, dict[str, int | None]] = {}
        used_totals: dict[tuple[str, str], int] = {}
        policies: list[QuotaPolicy] = []

        for entry in resolved:
            policy = entry.policy
            usage = await self._usage_for(
                repository=repo,
                organization_id=organization_id,
                policy=policy,
                cache=usage_cache,
            )
            subject_users = await self._subject_users(
                repository=repo,
                organization_id=organization_id,
                policy=policy,
                assignments=entry.matched_assignments,
                current_user_id=user_id,
            )
            # An all-models policy sees every in-flight reservation; a
            # model-scoped one sees only the models it covers.
            reservation_models = (
                None if policy.applies_to_all_models else set(policy.model_ids)
            )

            policy_remaining: dict[str, int | None] = {}
            for dimension in DIMENSIONS:
                limit = policy.limit_for(dimension)
                if limit is None:
                    policy_remaining[dimension] = None
                    continue
                consumed = usage.for_dimension(dimension, subject_users)
                if dimension == DIMENSION_RAW_INPUT:
                    consumed += await self._reservations.totals(
                        str(organization_id), subject_users, reservation_models
                    )
                used_totals[(policy.id, dimension)] = consumed
                policy_remaining[dimension] = limit - consumed

                if policy_remaining[dimension] < 0:
                    raise QuotaViolation(
                        policy_id=policy.id,
                        policy_name=policy.name,
                        dimension=dimension,
                        limit=limit,
                        used=consumed,
                        requested=0,
                        window_seconds=policy.window_seconds,
                        scope_mode=policy.scope_mode,
                        model_ids=policy.model_ids,
                    ).as_exception()

                # Raw input is the one dimension whose size is known before the
                # call, so a request that cannot fit is refused now rather than
                # discovered halfway through.
                if dimension == DIMENSION_RAW_INPUT and estimated > policy_remaining[dimension]:
                    raise QuotaViolation(
                        policy_id=policy.id,
                        policy_name=policy.name,
                        dimension=dimension,
                        limit=limit,
                        used=consumed,
                        requested=estimated,
                        window_seconds=policy.window_seconds,
                        scope_mode=policy.scope_mode,
                        model_ids=policy.model_ids,
                    ).as_exception()

                # An exhausted *output* budget with nothing left must refuse the
                # request outright. Output is always spent (the model always
                # replies), so a remaining balance of exactly zero means any
                # answer would overshoot — unlike cached input, where a request
                # may legitimately produce no cache reads at all.
                if dimension == DIMENSION_OUTPUT and policy_remaining[dimension] == 0:
                    raise QuotaViolation(
                        policy_id=policy.id,
                        policy_name=policy.name,
                        dimension=dimension,
                        limit=limit,
                        used=consumed,
                        requested=0,
                        window_seconds=policy.window_seconds,
                        scope_mode=policy.scope_mode,
                        model_ids=policy.model_ids,
                    ).as_exception()

            remaining[policy.id] = policy_remaining
            policies.append(policy)

        snapshot = QuotaSnapshot(
            policies=tuple(policies), remaining=remaining, used=used_totals
        )
        if reserve and estimated > 0 and user_id:
            await self._reservations.add(
                str(organization_id),
                str(user_id),
                str(model_id),
                estimated,
                ttl=max((p.window_seconds for p in policies), default=3600),
            )
        return snapshot

    def _org_wide_policies(
        self, policies: list[dict[str, Any]], model_id: str
    ) -> list[ResolvedPolicy]:
        """Policies applicable to a context-less call: org assignments only.

        A daemon bootstrap or maintenance call has no individual principal, so
        only organization-wide assignments can be attributed to it. Per-user
        policies have no owner to charge and are skipped rather than guessed.

        ``individual`` policies are also skipped: they define an allowance *per
        person*, and a call with no person has no per-person pool to draw from.
        Treating it as a fresh pool would let unattributed work bypass the limit
        entirely, so it is simpler and safer not to check it at all.
        """
        resolved: list[ResolvedPolicy] = []
        for raw in policies:
            scope = tuple(str(m) for m in raw["model_ids"])
            if model_id not in scope and ALL_MODELS not in scope:
                continue
            if raw["scope_mode"] != SCOPE_GLOBAL:
                continue
            org_assignment = next(
                (
                    assignment
                    for assignment in raw["assignments"]
                    if assignment["principal_type"] == PRINCIPAL_ORG
                ),
                None,
            )
            if org_assignment is None:
                continue
            resolved.append(
                ResolvedPolicy(
                    policy=QuotaPolicy(
                        id=raw["id"],
                        organization_id=raw["organization_id"],
                        model_ids=scope,
                        name=raw["name"],
                        description=raw["description"],
                        window_seconds=int(raw["window_seconds"]),
                        scope_mode=raw["scope_mode"],
                        raw_input_tokens_limit=raw["raw_input_tokens_limit"],
                        cached_input_tokens_limit=raw["cached_input_tokens_limit"],
                        output_tokens_limit=raw["output_tokens_limit"],
                        is_enabled=bool(raw["is_enabled"]),
                    ),
                    matched_assignments=(
                        QuotaAssignment(
                            id=str(org_assignment["id"]),
                            policy_id=str(raw["id"]),
                            principal_type=PRINCIPAL_ORG,
                            principal_id=None,
                        ),
                    ),
                )
            )
        return resolved

    async def _usage_for(
        self,
        *,
        repository: TokenQuotaRepository,
        organization_id: str,
        policy: QuotaPolicy,
        cache: dict[tuple[int, frozenset[str]], PoolUsage],
    ) -> PoolUsage:
        # Keyed on the whole model set: two policies over the same models in the
        # same window share one query, which is the common case (a broad default
        # plus a narrower exception).
        key = (policy.window_seconds, frozenset(policy.model_ids))
        if key not in cache:
            cache[key] = await repository.usage_for_window(
                organization_id,
                window_seconds=policy.window_seconds,
                model_ids=policy.model_ids,
            )
        return cache[key]

    async def _subject_users(
        self,
        *,
        repository: TokenQuotaRepository,
        organization_id: str,
        policy: QuotaPolicy,
        assignments: tuple[Any, ...],
        current_user_id: str | None,
    ) -> set[str] | None:
        """Which people share this policy's pool.

        ``None`` means "the whole organization" — used by ``global`` policies
        assigned to the org, where every member draws from one pool.
        ``individual`` mode always narrows to the requesting user: each member
        independently gets the full allowance.
        """
        if policy.scope_mode == SCOPE_INDIVIDUAL:
            return {str(current_user_id)} if current_user_id else set()
        users: set[str] = set()
        for assignment in assignments:
            if assignment.principal_type == PRINCIPAL_ORG:
                return None
            if assignment.principal_type == PRINCIPAL_USER and assignment.principal_id:
                users.add(str(assignment.principal_id))
            elif assignment.principal_type == PRINCIPAL_GROUP and assignment.principal_id:
                members = await repository.group_members(
                    organization_id, [assignment.principal_id]
                )
                users |= members.get(str(assignment.principal_id), set())
        return users

    # ── session accounting ─────────────────────────────────────────────────

    async def post_check(
        self,
        *,
        organization_id: str | None,
        user_id: str | None,
        model_id: str,
        usage: dict[str, Any],
    ) -> QuotaViolation | None:
        """Reconcile after a session.

        Records the overshoot in the log so an operator can see it even when the
        request had already completed. Never raises: the tokens are already
        spent, so failing here could only lose the caller's result.
        """
        if not self.is_enabled() or not organization_id:
            return None
        try:
            if not await self.has_policies(organization_id):
                return None
            repo = await self._repository()
            resolved = (
                await repo.resolve_policies(organization_id, user_id, model_id)
                if user_id
                else self._org_wide_policies(await self._org_policies(organization_id), model_id)
            )
            for entry in resolved:
                policy = entry.policy
                pool_usage = await repo.usage_for_window(
                    organization_id,
                    window_seconds=policy.window_seconds,
                    model_ids=policy.model_ids,
                )
                subject_users = await self._subject_users(
                    repository=repo,
                    organization_id=organization_id,
                    policy=policy,
                    assignments=entry.matched_assignments,
                    current_user_id=user_id,
                )
                for dimension in DIMENSIONS:
                    limit = policy.limit_for(dimension)
                    if limit is None:
                        continue
                    consumed = pool_usage.for_dimension(dimension, subject_users)
                    if consumed > limit:
                        violation = QuotaViolation(
                            policy_id=policy.id,
                            policy_name=policy.name,
                            dimension=dimension,
                            limit=limit,
                            used=consumed,
                            requested=0,
                            window_seconds=policy.window_seconds,
                            scope_mode=policy.scope_mode,
                            model_ids=policy.model_ids,
                        )
                        logger.warning(
                            "Token limit exceeded after session: %s (org=%s user=%s)",
                            violation.as_exception().message(),
                            organization_id,
                            user_id,
                        )
                        return violation
        except Exception:  # noqa: BLE001
            logger.debug("Token limit post-check failed", exc_info=True)
        return None

    async def release_reservation(
        self, organization_id: str | None, user_id: str | None, model_id: str
    ) -> None:
        """Drop an in-flight estimate once the request has ended.

        The real usage is in the ledger by then, so keeping the estimate would
        double-count it for the rest of the window.
        """
        if not organization_id or not user_id:
            return
        await self._reservations.release(
            [(str(organization_id), str(user_id), str(model_id))]
        )


_service: TokenLimitService | None = None
_service_lock = asyncio.Lock()


async def get_token_limit_service(pool: Any = None) -> TokenLimitService:
    """Return the process-wide limit service (created on first use)."""
    global _service
    if _service is None:
        async with _service_lock:
            if _service is None:
                _service = TokenLimitService(pool)
    return _service


def reset_token_limit_service() -> None:
    """Drop the singleton (tests, and after a settings change)."""
    global _service
    _service = None


def enrich_usage(
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Normalize a provider usage payload into the three quota dimensions.

    Providers report ``input_tokens`` inclusive of cached tokens, so the raw
    (uncached) figure has to be derived. Clamped at zero because a few providers
    report a cache read larger than the input total on boundary calls.
    """
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    cached = max(0, int(usage.get("cache_read_tokens") or 0))
    output = max(0, int(usage.get("output_tokens") or 0))
    return {
        DIMENSION_RAW_INPUT: max(input_tokens - cached, 0),
        DIMENSION_CACHED_INPUT: cached,
        DIMENSION_OUTPUT: output,
    }


__all__ = [
    "DIMENSION_LABELS",
    "QuotaSnapshot",
    "QuotaViolation",
    "TokenLimitService",
    "TokenQuotaExceeded",
    "enrich_usage",
    "estimate_tokens",
    "get_token_limit_service",
    "reset_token_limit_service",
]
