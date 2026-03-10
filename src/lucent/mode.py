"""Lucent deployment mode configuration.

Controls which features are available based on the deployment tier:
- personal: Single user, no org features. Open source default.
- team: Multi-user with organizations, RBAC, audit, sharing. Requires license.

Set via LUCENT_MODE environment variable. Defaults to 'personal'.
"""

import os
from enum import Enum
from functools import lru_cache

from lucent.logging import get_logger

logger = get_logger("mode")


class DeploymentMode(str, Enum):
    """Available deployment modes."""
    PERSONAL = "personal"
    TEAM = "team"


@lru_cache(maxsize=1)
def get_mode() -> DeploymentMode:
    """Get the current deployment mode.
    
    Returns:
        The configured DeploymentMode, defaulting to PERSONAL.
    """
    mode_str = os.environ.get("LUCENT_MODE", "personal").lower().strip()

    try:
        mode = DeploymentMode(mode_str)
    except ValueError:
        logger.warning(
            f"Unknown LUCENT_MODE '{mode_str}', defaulting to 'personal'. "
            f"Valid modes: {', '.join(m.value for m in DeploymentMode)}"
        )
        mode = DeploymentMode.PERSONAL

    # Team mode requires a license key
    if mode == DeploymentMode.TEAM:
        license_key = os.environ.get("LUCENT_LICENSE_KEY", "").strip()
        if not license_key:
            logger.error(
                "LUCENT_MODE=team requires LUCENT_LICENSE_KEY. "
                "Falling back to personal mode."
            )
            return DeploymentMode.PERSONAL

        if not _validate_license(license_key):
            logger.error(
                "Invalid LUCENT_LICENSE_KEY. "
                "Falling back to personal mode."
            )
            return DeploymentMode.PERSONAL

        logger.info("Team mode enabled with valid license")

    return mode


def is_personal_mode() -> bool:
    """Check if running in personal (single-user) mode."""
    return get_mode() == DeploymentMode.PERSONAL


def is_team_mode() -> bool:
    """Check if running in team (multi-user) mode."""
    return get_mode() == DeploymentMode.TEAM


def require_team_mode(feature_name: str) -> None:
    """Raise an error if a team-only feature is accessed in personal mode.
    
    Args:
        feature_name: Name of the feature for the error message.
        
    Raises:
        PermissionError: If not in team mode.
    """
    if not is_team_mode():
        raise PermissionError(
            f"'{feature_name}' requires team mode. "
            f"Set LUCENT_MODE=team with a valid LUCENT_LICENSE_KEY."
        )


def _validate_license(license_key: str) -> bool:
    """Validate a license key.
    
    NOTE: License validation is not yet implemented. Any non-empty key
    is accepted. This placeholder will be replaced with cryptographic
    validation before the team tier is officially available.
    
    Args:
        license_key: The license key to validate.
        
    Returns:
        True if the license is valid.
    """
    # Placeholder - will be replaced with actual validation
    return bool(license_key)
