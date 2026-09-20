"""Backward-compatible facade for integration repositories."""

from lucent.db.integrations_repositories import (
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)


__all__ = ["IntegrationRepo", "PairingChallengeRepo", "UserLinkRepo"]
