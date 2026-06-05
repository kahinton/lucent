"""Tests for identity resolution and pairing-code challenge service.

Covers: PairingChallengeService (generate, rate limiting, verify, bcrypt,
max attempts, TTL) and IdentityResolver (active link, no link, superseded
link, redeem_code).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import bcrypt
import pytest

from lucent.integrations.identity import (
    CODE_TTL,
    MAX_ATTEMPTS,
    MAX_CODES_PER_WINDOW,
    RATE_LIMIT_WINDOW,
    IdentityResolver,
    IdentityResult,
    PairingChallengeService,
    VerifyResult,
)
from lucent.integrations.models import (
    PairingChallengeStatus,
    VerificationMethod,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_challenge(
    *,
    challenge_id: str | None = None,
    integration_id: str | None = None,
    user_id: str | None = None,
    code_hash: str | None = None,
    status: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = MAX_ATTEMPTS,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": challenge_id or str(uuid4()),
        "integration_id": integration_id or str(uuid4()),
        "user_id": user_id or str(uuid4()),
        "code_hash": code_hash or "",
        "status": status,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "expires_at": expires_at or (datetime.now(timezone.utc) + CODE_TTL),
        "created_at": datetime.now(timezone.utc),
    }


def _make_link(
    *,
    link_id: str | None = None,
    user_id: str | None = None,
    organization_id: str | None = None,
    provider: str = "slack",
    external_user_id: str = "U_EXT_123",
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": link_id or str(uuid4()),
        "user_id": user_id or str(uuid4()),
        "organization_id": organization_id or str(uuid4()),
        "integration_id": str(uuid4()),
        "provider": provider,
        "external_user_id": external_user_id,
        "external_workspace_id": None,
        "status": status,
        "verification_method": VerificationMethod.PAIRING_CODE.value,
        "linked_at": datetime.now(timezone.utc),
    }


def _mock_challenge_repo() -> MagicMock:
    repo = MagicMock()
    repo.count_recent_by_user = AsyncMock(return_value=0)
    repo.get_pending_for_user = AsyncMock(return_value=[])
    repo.create = AsyncMock()
    repo.increment_attempts = AsyncMock()
    repo.redeem = AsyncMock()
    # For _pending_for_integration — needs pool.acquire context manager
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    repo.pool = pool
    repo._conn = conn  # stash for tests to configure
    return repo


def _mock_link_repo() -> MagicMock:
    repo = MagicMock()
    repo.resolve_identity = AsyncMock(return_value=None)
    repo.create = AsyncMock()
    repo.activate = AsyncMock()
    repo.supersede = AsyncMock()
    return repo


# ===========================================================================
# PairingChallengeService — generate
# ===========================================================================


class TestPairingGenerate:
    """PairingChallengeService.generate() tests."""

    @pytest.mark.asyncio
    async def test_generate_returns_challenge_and_plaintext(self):
        repo = _mock_challenge_repo()
        challenge = _make_challenge()
        repo.create = AsyncMock(return_value=challenge)

        svc = PairingChallengeService(repo)
        result, plaintext = await svc.generate(
            integration_id=str(uuid4()), user_id=str(uuid4()),
        )

        assert result == challenge
        assert isinstance(plaintext, str)
        assert len(plaintext) > 10  # url-safe token, at least 22 chars

    @pytest.mark.asyncio
    async def test_generate_calls_create_with_bcrypt_hash(self):
        repo = _mock_challenge_repo()
        repo.create = AsyncMock(return_value=_make_challenge())

        svc = PairingChallengeService(repo)
        _, plaintext = await svc.generate(
            integration_id="int-1", user_id="user-1",
        )

        repo.create.assert_awaited_once()
        call_kwargs = repo.create.call_args[1]
        # The stored hash should verify against the plaintext
        assert bcrypt.checkpw(
            plaintext.encode("utf-8"),
            call_kwargs["code_hash"].encode("utf-8"),
        )

    @pytest.mark.asyncio
    async def test_generate_expires_stale_pending(self):
        repo = _mock_challenge_repo()
        old_challenge = _make_challenge(challenge_id="old-1")
        repo.get_pending_for_user = AsyncMock(return_value=[old_challenge])
        repo.create = AsyncMock(return_value=_make_challenge())

        svc = PairingChallengeService(repo)
        await svc.generate(integration_id="int-1", user_id="user-1")

        repo.increment_attempts.assert_awaited_once_with("old-1")


class TestPairingRateLimit:
    """Rate limiting for pairing code generation."""

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_raises(self):
        repo = _mock_challenge_repo()
        repo.count_recent_by_user = AsyncMock(return_value=MAX_CODES_PER_WINDOW)

        svc = PairingChallengeService(repo)
        with pytest.raises(ValueError, match="Rate limit exceeded"):
            await svc.generate(integration_id="int-1", user_id="user-1")

    @pytest.mark.asyncio
    async def test_rate_limit_just_under_succeeds(self):
        repo = _mock_challenge_repo()
        repo.count_recent_by_user = AsyncMock(return_value=MAX_CODES_PER_WINDOW - 1)
        repo.create = AsyncMock(return_value=_make_challenge())

        svc = PairingChallengeService(repo)
        result, _ = await svc.generate(
            integration_id="int-1", user_id="user-1",
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_rate_limit_checks_correct_window(self):
        repo = _mock_challenge_repo()
        repo.count_recent_by_user = AsyncMock(return_value=0)
        repo.create = AsyncMock(return_value=_make_challenge())

        svc = PairingChallengeService(repo)
        await svc.generate(integration_id="int-1", user_id="user-1")

        call_args = repo.count_recent_by_user.call_args
        since_arg = call_args[1].get("since") or call_args[0][1]
        expected = datetime.now(timezone.utc) - RATE_LIMIT_WINDOW
        assert abs((since_arg - expected).total_seconds()) < 5


# ===========================================================================
# PairingChallengeService — verify
# ===========================================================================


class TestPairingVerify:
    """PairingChallengeService.verify() tests."""

    @pytest.mark.asyncio
    async def test_verify_no_pending_challenges(self):
        repo = _mock_challenge_repo()
        # pool.acquire -> conn.fetch returns empty list (default)

        svc = PairingChallengeService(repo)
        result = await svc.verify(code="abc", integration_id=str(uuid4()))

        assert not result.valid
        assert result.error == "no_pending_challenges"

    @pytest.mark.asyncio
    async def test_verify_correct_code(self):
        repo = _mock_challenge_repo()
        plaintext = "test-secret-code"
        hashed = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")
        user_id = str(uuid4())
        ch_id = str(uuid4())
        challenge = _make_challenge(
            challenge_id=ch_id, user_id=user_id, code_hash=hashed,
        )

        # Configure the pool query to return our challenge
        repo._conn.fetch = AsyncMock(return_value=[challenge])
        # increment_attempts returns the challenge (not exhausted)
        repo.increment_attempts = AsyncMock(return_value=challenge)

        svc = PairingChallengeService(repo)
        result = await svc.verify(code=plaintext, integration_id=str(uuid4()))

        assert result.valid
        assert result.user_id == user_id
        assert result.challenge_id == ch_id

    @pytest.mark.asyncio
    async def test_verify_wrong_code(self):
        repo = _mock_challenge_repo()
        hashed = bcrypt.hashpw(b"correct-code", bcrypt.gensalt()).decode("utf-8")
        challenge = _make_challenge(code_hash=hashed)

        repo._conn.fetch = AsyncMock(return_value=[challenge])
        repo.increment_attempts = AsyncMock(return_value=challenge)

        svc = PairingChallengeService(repo)
        result = await svc.verify(code="wrong-code", integration_id=str(uuid4()))

        assert not result.valid
        assert result.error == "invalid_code"

    @pytest.mark.asyncio
    async def test_verify_exhausted_challenge_skipped(self):
        repo = _mock_challenge_repo()
        hashed = bcrypt.hashpw(b"code", bcrypt.gensalt()).decode("utf-8")
        challenge = _make_challenge(code_hash=hashed)

        repo._conn.fetch = AsyncMock(return_value=[challenge])
        # increment_attempts returns exhausted status
        exhausted = {**challenge, "status": PairingChallengeStatus.EXHAUSTED.value}
        repo.increment_attempts = AsyncMock(return_value=exhausted)

        svc = PairingChallengeService(repo)
        result = await svc.verify(code="code", integration_id=str(uuid4()))

        assert not result.valid

    @pytest.mark.asyncio
    async def test_verify_increment_returns_none_skips(self):
        repo = _mock_challenge_repo()
        challenge = _make_challenge(code_hash="irrelevant")

        repo._conn.fetch = AsyncMock(return_value=[challenge])
        repo.increment_attempts = AsyncMock(return_value=None)

        svc = PairingChallengeService(repo)
        result = await svc.verify(code="code", integration_id=str(uuid4()))

        assert not result.valid
        assert result.error == "invalid_code"

    @pytest.mark.asyncio
    async def test_verify_max_attempts_constant(self):
        assert MAX_ATTEMPTS == 5


# ===========================================================================
# IdentityResolver — resolve
# ===========================================================================


class TestIdentityResolve:
    """IdentityResolver.resolve() tests."""

    @pytest.mark.asyncio
    async def test_resolve_active_link(self):
        link_repo = _mock_link_repo()
        user_id = str(uuid4())
        org_id = str(uuid4())
        link = _make_link(
            user_id=user_id, organization_id=org_id, status="active",
        )
        link_repo.resolve_identity = AsyncMock(return_value=link)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.resolve(
            provider="slack",
            external_user_id="U_EXT_123",
        )

        assert result.resolved
        assert result.user_id == user_id
        assert result.organization_id == org_id
        assert result.link == link

    @pytest.mark.asyncio
    async def test_resolve_no_link(self):
        link_repo = _mock_link_repo()
        link_repo.resolve_identity = AsyncMock(return_value=None)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.resolve(
            provider="slack",
            external_user_id="U_UNKNOWN",
        )

        assert not result.resolved
        assert result.user_id is None

    @pytest.mark.asyncio
    async def test_resolve_superseded_link_not_resolved(self):
        link_repo = _mock_link_repo()
        link = _make_link(status="superseded")
        link_repo.resolve_identity = AsyncMock(return_value=link)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.resolve(
            provider="slack",
            external_user_id="U_EXT_123",
        )

        assert not result.resolved

    @pytest.mark.asyncio
    async def test_resolve_revoked_link_not_resolved(self):
        link_repo = _mock_link_repo()
        link = _make_link(status="revoked")
        link_repo.resolve_identity = AsyncMock(return_value=link)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.resolve(
            provider="slack",
            external_user_id="U_EXT_123",
        )

        assert not result.resolved

    @pytest.mark.asyncio
    async def test_resolve_passes_workspace_id(self):
        link_repo = _mock_link_repo()
        link_repo.resolve_identity = AsyncMock(return_value=None)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        await resolver.resolve(
            provider="slack",
            external_user_id="U_EXT_123",
            external_workspace_id="W_123",
        )

        link_repo.resolve_identity.assert_awaited_once_with(
            provider="slack",
            external_user_id="U_EXT_123",
            external_workspace_id="W_123",
        )


# ===========================================================================
# IdentityResolver — resolve_or_prompt
# ===========================================================================


class TestResolveOrPrompt:
    """IdentityResolver.resolve_or_prompt() tests."""

    @pytest.mark.asyncio
    async def test_returns_identity_when_linked(self):
        link_repo = _mock_link_repo()
        link = _make_link(status="active")
        link_repo.resolve_identity = AsyncMock(return_value=link)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.resolve_or_prompt(
            provider="slack", external_user_id="U_EXT_123",
        )

        assert isinstance(result, IdentityResult)
        assert result.resolved

    @pytest.mark.asyncio
    async def test_returns_prompt_string_when_unlinked(self):
        link_repo = _mock_link_repo()
        link_repo.resolve_identity = AsyncMock(return_value=None)

        challenge_repo = _mock_challenge_repo()
        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.resolve_or_prompt(
            provider="slack", external_user_id="U_UNKNOWN",
        )

        assert isinstance(result, str)
        assert "pairing code" in result.lower()


# ===========================================================================
# IdentityResolver — redeem_code
# ===========================================================================


class TestRedeemCode:
    """IdentityResolver.redeem_code() tests."""

    @pytest.mark.asyncio
    async def test_successful_redemption(self):
        link_repo = _mock_link_repo()
        challenge_repo = _mock_challenge_repo()

        user_id = str(uuid4())
        ch_id = str(uuid4())
        org_id = str(uuid4())
        integration_id = str(uuid4())
        new_link_id = str(uuid4())

        # Mock verify to succeed
        plaintext = "valid-code"
        hashed = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")
        challenge = _make_challenge(
            challenge_id=ch_id, user_id=user_id, code_hash=hashed,
        )
        challenge_repo._conn.fetch = AsyncMock(return_value=[challenge])
        challenge_repo.increment_attempts = AsyncMock(return_value=challenge)

        # Mock redeem
        challenge_repo.redeem = AsyncMock(return_value=challenge)

        # No existing link
        link_repo.resolve_identity = AsyncMock(return_value=None)

        # Mock create + activate
        new_link = _make_link(
            link_id=new_link_id, user_id=user_id, organization_id=org_id,
        )
        link_repo.create = AsyncMock(return_value=new_link)
        link_repo.activate = AsyncMock(return_value={**new_link, "status": "active"})

        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.redeem_code(
            code=plaintext,
            integration_id=integration_id,
            organization_id=org_id,
            provider="slack",
            external_user_id="U_EXT_456",
        )

        assert result.resolved
        assert result.user_id == user_id
        assert result.organization_id == org_id

    @pytest.mark.asyncio
    async def test_redemption_with_existing_link_supersedes(self):
        link_repo = _mock_link_repo()
        challenge_repo = _mock_challenge_repo()

        user_id = str(uuid4())
        ch_id = str(uuid4())
        org_id = str(uuid4())
        old_link_id = str(uuid4())
        new_link_id = str(uuid4())

        # Mock verify to succeed
        plaintext = "valid-code"
        hashed = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")
        challenge = _make_challenge(
            challenge_id=ch_id, user_id=user_id, code_hash=hashed,
        )
        challenge_repo._conn.fetch = AsyncMock(return_value=[challenge])
        challenge_repo.increment_attempts = AsyncMock(return_value=challenge)
        challenge_repo.redeem = AsyncMock(return_value=challenge)

        # Existing link to supersede
        old_link = _make_link(
            link_id=old_link_id, user_id=user_id, organization_id=org_id,
        )
        link_repo.resolve_identity = AsyncMock(return_value=old_link)

        new_link = _make_link(
            link_id=new_link_id, user_id=user_id, organization_id=org_id,
        )
        link_repo.create = AsyncMock(return_value=new_link)
        link_repo.activate = AsyncMock(return_value={**new_link, "status": "active"})

        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.redeem_code(
            code=plaintext,
            integration_id=str(uuid4()),
            organization_id=org_id,
            provider="slack",
            external_user_id="U_EXT_456",
        )

        assert result.resolved
        link_repo.supersede.assert_awaited_once_with(
            old_link_id, org_id, superseded_by=new_link_id,
        )

    @pytest.mark.asyncio
    async def test_failed_verification_returns_unresolved(self):
        link_repo = _mock_link_repo()
        challenge_repo = _mock_challenge_repo()
        # No challenges => verify fails

        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.redeem_code(
            code="bad-code",
            integration_id=str(uuid4()),
            organization_id=str(uuid4()),
            provider="slack",
            external_user_id="U_EXT_789",
        )

        assert not result.resolved

    @pytest.mark.asyncio
    async def test_redeem_returns_none_returns_unresolved(self):
        link_repo = _mock_link_repo()
        challenge_repo = _mock_challenge_repo()

        # Verify succeeds
        plaintext = "valid-code"
        hashed = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")
        user_id = str(uuid4())
        challenge = _make_challenge(
            challenge_id=str(uuid4()), user_id=user_id, code_hash=hashed,
        )
        challenge_repo._conn.fetch = AsyncMock(return_value=[challenge])
        challenge_repo.increment_attempts = AsyncMock(return_value=challenge)
        # But redeem fails (e.g., concurrent redemption)
        challenge_repo.redeem = AsyncMock(return_value=None)

        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.redeem_code(
            code=plaintext,
            integration_id=str(uuid4()),
            organization_id=str(uuid4()),
            provider="slack",
            external_user_id="U_EXT_789",
        )

        assert not result.resolved

    @pytest.mark.asyncio
    async def test_activate_returns_none_falls_back_to_link(self):
        """When activate returns None, redeem_code uses the original link."""
        link_repo = _mock_link_repo()
        challenge_repo = _mock_challenge_repo()

        user_id = str(uuid4())
        org_id = str(uuid4())

        plaintext = "valid-code"
        hashed = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")
        challenge = _make_challenge(
            challenge_id=str(uuid4()), user_id=user_id, code_hash=hashed,
        )
        challenge_repo._conn.fetch = AsyncMock(return_value=[challenge])
        challenge_repo.increment_attempts = AsyncMock(return_value=challenge)
        challenge_repo.redeem = AsyncMock(return_value=challenge)

        link_repo.resolve_identity = AsyncMock(return_value=None)
        new_link = _make_link(user_id=user_id, organization_id=org_id)
        link_repo.create = AsyncMock(return_value=new_link)
        link_repo.activate = AsyncMock(return_value=None)  # fails

        challenge_svc = PairingChallengeService(challenge_repo)
        resolver = IdentityResolver(link_repo, challenge_svc)

        result = await resolver.redeem_code(
            code=plaintext,
            integration_id=str(uuid4()),
            organization_id=org_id,
            provider="slack",
            external_user_id="U_EXT_456",
        )

        # Still resolves — uses original link as fallback
        assert result.resolved
        assert result.link == new_link


# ===========================================================================
# Dataclass contracts
# ===========================================================================


class TestDataclasses:
    """IdentityResult and VerifyResult are frozen dataclasses."""

    def test_identity_result_defaults(self):
        r = IdentityResult(resolved=False)
        assert r.user_id is None
        assert r.organization_id is None
        assert r.link is None

    def test_identity_result_frozen(self):
        r = IdentityResult(resolved=True, user_id="u1")
        with pytest.raises(AttributeError):
            r.resolved = False  # type: ignore[misc]

    def test_verify_result_defaults(self):
        r = VerifyResult(valid=False)
        assert r.challenge_id is None
        assert r.user_id is None
        assert r.error is None

    def test_verify_result_frozen(self):
        r = VerifyResult(valid=True, challenge_id="c1")
        with pytest.raises(AttributeError):
            r.valid = False  # type: ignore[misc]


# ===========================================================================
# Constants
# ===========================================================================


class TestConstants:
    """Verify identity module constants match expected values."""

    def test_code_ttl_is_10_minutes(self):
        assert CODE_TTL == timedelta(minutes=10)

    def test_max_attempts_is_5(self):
        assert MAX_ATTEMPTS == 5

    def test_max_codes_per_window_is_10(self):
        assert MAX_CODES_PER_WINDOW == 10

    def test_rate_limit_window_is_1_hour(self):
        assert RATE_LIMIT_WINDOW == timedelta(hours=1)
