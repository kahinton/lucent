"""Backward-compatible facade for integration repositories."""

from lucent.db.integrations_repositories import (
    _INTEGRATION_TRANSITIONS,
    _USER_LINK_TRANSITIONS,
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)

__all__ = [
    "_INTEGRATION_TRANSITIONS",
    "_USER_LINK_TRANSITIONS",
    "IntegrationRepo",
    "PairingChallengeRepo",
    "UserLinkRepo",
]
