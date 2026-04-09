"""Identity resolution and pairing-code challenge service.

PairingChallengeService — generate, hash, and verify 128-bit pairing codes.
IdentityResolver — map external platform identities to Lucent users.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

from lucent.integrations.models import (
    PairingChallengeStatus,
    UserLinkStatus,
    VerificationMethod,
)
from lucent.integrations.repositories import (
    PairingChallengeRepo,
    UserLinkRepo,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CODE_BYTES = 16  # 128-bit pairing code
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
# Rate limit: max codes a single user can generate per window
MAX_CODES_PER_WINDOW = 10
RATE_LIMIT_WINDOW = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityResult:
    """Outcome of an identity resolution attempt."""

    resolved: bool
    user_id: str | None = None
    organization_id: str | None = None
    link: dict[str, Any] | None = None


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a pairing-code verification attempt."""

    valid: bool
    challenge_id: str | None = None
    user_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# PairingChallengeService
# ---------------------------------------------------------------------------


class PairingChallengeService:
    """Generate, hash, and verify 128-bit pairing codes.

    Codes are bcrypt-hashed at rest. Each challenge allows up to
    ``MAX_ATTEMPTS`` verification attempts and expires after ``CODE_TTL``.
    """

    def __init__(self, challenge_repo: PairingChallengeRepo) -> None:
        self._repo = challenge_repo

    # -- Generate -------------------------------------------------------------

    async def generate(
        self,
        *,
        integration_id: str,
        user_id: str,
    ) -> tuple[dict[str, Any], str]:
        """Create a new pairing challenge and return ``(challenge, plaintext_code)``.

        Raises ``ValueError`` if the user has exceeded the rate limit.
        """
        now = datetime.now(timezone.utc)

        # Rate-limit: cap codes per user per window
        recent = await self._repo.count_recent_by_user(
            user_id, since=now - RATE_LIMIT_WINDOW,
        )
        if recent >= MAX_CODES_PER_WINDOW:
            raise ValueError(
                f"Rate limit exceeded: {MAX_CODES_PER_WINDOW} codes "
                f"per {RATE_LIMIT_WINDOW}",
            )

        # Expire any stale pending challenges for this user+integration
        pending = await self._repo.get_pending_for_user(user_id, integration_id)
        for ch in pending:
            await self._repo.increment_attempts(str(ch["id"]))
            # Incrementing past max_attempts auto-exhausts; but we really
            # just want them expired.  The expire_stale job handles TTL-based
            # cleanup — here we just ensure the user gets a fresh code.

        plaintext = secrets.token_urlsafe(CODE_BYTES)  # 22-char URL-safe string
        code_hash = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt(),
        ).decode("utf-8")

        challenge = await self._repo.create(
            integration_id=integration_id,
            user_id=user_id,
            code_hash=code_hash,
            expires_at=now + CODE_TTL,
            max_attempts=MAX_ATTEMPTS,
        )

        logger.info(
            "Pairing code issued for user=%s integration=%s (expires %s)",
            user_id, integration_id, challenge["expires_at"],
        )
        return challenge, plaintext

    # -- Verify ---------------------------------------------------------------

    async def verify(
        self,
        *,
        code: str,
        integration_id: str,
    ) -> VerifyResult:
        """Check a plaintext code against all pending challenges for an integration.

        Increments ``attempt_count`` on every candidate checked. Returns a
        ``VerifyResult`` indicating success or the reason for failure.
        """
        # Fetch all pending, non-expired challenges for this integration
        # We need to scan because the code is bcrypt-hashed (no direct lookup).
        challenges = await self._pending_for_integration(integration_id)

        if not challenges:
            return VerifyResult(valid=False, error="no_pending_challenges")

        for ch in challenges:
            challenge_id = str(ch["id"])

            # Increment attempt counter (returns None if already exhausted/expired)
            updated = await self._repo.increment_attempts(challenge_id)
            if updated is None:
                continue

            if updated["status"] == PairingChallengeStatus.EXHAUSTED.value:
                logger.warning(
                    "Pairing challenge %s exhausted (integration=%s)",
                    challenge_id, integration_id,
                )
                continue

            # Constant-time comparison via bcrypt
            if bcrypt.checkpw(
                code.encode("utf-8"),
                ch["code_hash"].encode("utf-8"),
            ):
                logger.info(
                    "Pairing code verified for challenge=%s user=%s",
                    challenge_id, ch["user_id"],
                )
                return VerifyResult(
                    valid=True,
                    challenge_id=challenge_id,
                    user_id=str(ch["user_id"]),
                )

        return VerifyResult(valid=False, error="invalid_code")

    # -- Internal helpers -----------------------------------------------------

    async def _pending_for_integration(
        self, integration_id: str,
    ) -> list[dict[str, Any]]:
        """Return all pending, non-expired challenges for an integration.

        Uses a direct pool query — PairingChallengeRepo doesn't expose
        an integration-scoped pending query, so we go to the DB directly.
        """
        from uuid import UUID

        async with self._repo.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM pairing_challenges
                WHERE integration_id = $1
                  AND status = 'pending'
                  AND expires_at > NOW()
                  AND attempt_count < max_attempts
                ORDER BY created_at DESC
                """,
                UUID(integration_id),
            )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# IdentityResolver
# ---------------------------------------------------------------------------


class IdentityResolver:
    """Map external platform identities to Lucent users.

    The primary lookup is by ``(provider, external_user_id, organization_id)``
    against the ``user_links`` table. Unlinked users receive a graceful
    ``IdentityResult(resolved=False)`` — callers decide what UX to show.
    """

    def __init__(
        self,
        user_link_repo: UserLinkRepo,
        challenge_service: PairingChallengeService,
    ) -> None:
        self._links = user_link_repo
        self._challenges = challenge_service

    async def resolve(
        self,
        *,
        provider: str,
        external_user_id: str,
        external_workspace_id: str | None = None,
    ) -> IdentityResult:
        """Look up the Lucent user linked to an external identity.

        Returns ``IdentityResult(resolved=True, ...)`` when an active link
        exists, or ``IdentityResult(resolved=False)`` when the external user
        is unknown or their link is inactive.
        """
        link = await self._links.resolve_identity(
            provider=provider,
            external_user_id=external_user_id,
            external_workspace_id=external_workspace_id,
        )

        if link and link.get("status") == UserLinkStatus.ACTIVE.value:
            return IdentityResult(
                resolved=True,
                user_id=str(link["user_id"]),
                organization_id=str(link["organization_id"]),
                link=link,
            )

        logger.debug(
            "Unlinked external user: provider=%s external_id=%s workspace=%s",
            provider, external_user_id, external_workspace_id,
        )
        return IdentityResult(resolved=False)

    async def resolve_or_prompt(
        self,
        *,
        provider: str,
        external_user_id: str,
        external_workspace_id: str | None = None,
    ) -> IdentityResult | str:
        """Resolve identity, returning a user-facing prompt message if unlinked.

        Convenience wrapper: callers can check ``isinstance(result, str)``
        to detect the unlinked case and forward the prompt to the platform.
        """
        result = await self.resolve(
            provider=provider,
            external_user_id=external_user_id,
            external_workspace_id=external_workspace_id,
        )
        if result.resolved:
            return result

        return (
            "Your account isn't linked to Lucent yet. "
            "Visit the Lucent web UI to generate a pairing code, "
            "then send it here as a DM to link your account."
        )

    async def redeem_code(
        self,
        *,
        code: str,
        integration_id: str,
        organization_id: str,
        provider: str,
        external_user_id: str,
        external_workspace_id: str | None = None,
    ) -> IdentityResult:
        """Verify a pairing code and activate the user link.

        On success, redeems the challenge, creates (or reactivates) a user link,
        and returns the resolved identity.
        """
        vr = await self._challenges.verify(
            code=code, integration_id=integration_id,
        )
        if not vr.valid or vr.user_id is None or vr.challenge_id is None:
            logger.warning(
                "Pairing code redemption failed: integration=%s external=%s error=%s",
                integration_id, external_user_id, vr.error,
            )
            return IdentityResult(resolved=False)

        # Redeem the challenge (marks it as used + records external claimant)
        redeemed = await self._challenges._repo.redeem(
            vr.challenge_id, claimed_by_external_id=external_user_id,
        )
        if redeemed is None:
            return IdentityResult(resolved=False)

        # Check for an existing link to supersede
        existing = await self._links.resolve_identity(
            provider=provider,
            external_user_id=external_user_id,
            external_workspace_id=external_workspace_id,
        )

        # Create the new user link
        link = await self._links.create(
            organization_id=organization_id,
            integration_id=integration_id,
            user_id=vr.user_id,
            provider=provider,
            external_user_id=external_user_id,
            external_workspace_id=external_workspace_id,
            verification_method=VerificationMethod.PAIRING_CODE.value,
        )

        new_link_id = str(link["id"])

        # Supersede the old link if one existed
        if existing:
            await self._links.supersede(
                str(existing["id"]),
                str(existing["organization_id"]),
                superseded_by=new_link_id,
            )

        # Activate the new link
        activated = await self._links.activate(new_link_id, organization_id)
        if activated is None:
            activated = link

        logger.info(
            "Identity linked: user=%s external=%s provider=%s",
            vr.user_id, external_user_id, provider,
        )

        return IdentityResult(
            resolved=True,
            user_id=vr.user_id,
            organization_id=organization_id,
            link=activated,
        )
