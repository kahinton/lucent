"""Lucent deployment mode reporting.

**The mode no longer gates behaviour.** Capability is decided by the caller's
role and permissions (``lucent.rbac``), not by an environment variable. An
instance that has organizations should be manageable whether or not
``LUCENT_MODE`` says "team"; a single-user instance simply has one organization
and one member. Gating routers on a mode meant a licensed feature could be
unreachable for a configuration reason unrelated to authorization — and that is
exactly what happened: the entire organization API was unmounted in "personal"
mode, returning 404 instead of 403.

What remains here is *reporting*: the mode is read for log lines and for a label
in the UI (``Team`` vs ``Personal``). It has no authority. Nothing should be
mounted, unmounted, enabled, or refused based on this module.

``LUCENT_MODE`` defaults to ``personal`` and accepts ``personal`` or ``team``.
A ``team`` instance additionally reports the health of ``LUCENT_LICENSE_KEY``
when one is present, purely as a log line.
"""

import os
from enum import Enum
from functools import lru_cache

from lucent.license import LicenseError, validate_license
from lucent.logging import get_logger

logger = get_logger("mode")


class DeploymentMode(str, Enum):
    """Deployment shape, for reporting only."""

    PERSONAL = "personal"
    TEAM = "team"


@lru_cache(maxsize=1)
def get_mode() -> DeploymentMode:
    """Return the reported deployment mode.

    Returns:
        The configured DeploymentMode, defaulting to PERSONAL.

    Note:
        This value carries no authority. Do not branch behaviour on it — use
        :mod:`lucent.rbac` permissions instead. See the module docstring.
    """
    mode_str = os.environ.get("LUCENT_MODE", "personal").lower().strip()

    try:
        mode = DeploymentMode(mode_str)
    except ValueError:
        logger.warning(
            "Unknown LUCENT_MODE %r, reporting 'personal'. Valid modes: %s",
            mode_str,
            ", ".join(m.value for m in DeploymentMode),
        )
        mode = DeploymentMode.PERSONAL

    if mode == DeploymentMode.TEAM:
        # Report licence health, but never let it change what works.
        license_key = os.environ.get("LUCENT_LICENSE_KEY", "").strip()
        if not license_key:
            logger.info(
                "LUCENT_MODE=team without LUCENT_LICENSE_KEY. Continuing: the "
                "mode is reporting only and does not gate any feature."
            )
        else:
            _log_license_status(license_key)

    return mode


def is_personal_mode() -> bool:
    """True when the instance reports itself as single-user.

    Reporting only — see the module docstring. Never use this to allow or deny
    an operation.
    """
    return get_mode() == DeploymentMode.PERSONAL


def is_team_mode() -> bool:
    """True when the instance reports itself as multi-user.

    Reporting only — see the module docstring. Never use this to allow or deny
    an operation.
    """
    return get_mode() == DeploymentMode.TEAM


def deployment_label() -> str:
    """Human-readable mode label for the UI."""
    return "Team" if is_team_mode() else "Personal"


def _log_license_status(license_key: str) -> None:
    """Log whether the configured licence is valid. Never raises."""
    try:
        info = validate_license(license_key)
    except LicenseError as exc:
        logger.warning(
            "LUCENT_LICENSE_KEY is not valid (%s). Continuing: the licence does "
            "not gate any feature.",
            exc,
        )
        return
    logger.info("License validated for org '%s', expires %s", info.org, info.expires_at)
