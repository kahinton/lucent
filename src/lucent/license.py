"""Cryptographic license validation for Lucent team tier.

License keys use Ed25519 signatures to prevent forgery. Format:
    LCT-V1.<base64url-payload>.<base64url-signature>

The payload is JSON containing org info, expiration, and feature flags.
The signature is computed over the raw payload bytes using Ed25519.
Only the public verification key is embedded in the source code;
the private signing key is kept offline for license generation.
"""

import base64
import json
import time
from dataclasses import dataclass, field

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from lucent.logging import get_logger

logger = get_logger("license")

LICENSE_PREFIX = "LCT-V1"

# Ed25519 public verification key (hex-encoded).
# The corresponding private key is kept offline for license generation.
_PUBLIC_KEY_HEX = "a0d7405de9340a872432e2aa1848d81485dd15d2dcbe341981015b976cc9eece"


@dataclass(frozen=True)
class LicenseInfo:
    """Validated license information."""

    org: str
    tier: str
    max_users: int
    features: list[str] = field(default_factory=list)
    issued_at: int = 0
    expires_at: int = 0


class LicenseError(Exception):
    """Raised when license validation fails."""


def _b64url_decode(s: str) -> bytes:
    """Decode base64url without padding."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64url_encode(data: bytes) -> str:
    """Encode to base64url without padding."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def validate_license(license_key: str, *, _now: float | None = None) -> LicenseInfo:
    """Validate a license key and return its info.

    Args:
        license_key: The full license key string.
        _now: Override current time for testing (unix timestamp).

    Returns:
        LicenseInfo with the validated license details.

    Raises:
        LicenseError: If the license is invalid, expired, or tampered with.
    """
    now = _now if _now is not None else time.time()

    # Parse format: LCT-V1.<payload>.<signature>
    parts = license_key.strip().split(".")
    if len(parts) != 3:
        raise LicenseError("Invalid license format: expected LCT-V1.<payload>.<signature>")

    prefix, payload_b64, signature_b64 = parts

    if prefix != LICENSE_PREFIX:
        raise LicenseError(f"Invalid license prefix: expected '{LICENSE_PREFIX}'")

    try:
        payload_bytes = _b64url_decode(payload_b64)
    except Exception:
        raise LicenseError("Invalid license: payload decode failed")

    try:
        signature_bytes = _b64url_decode(signature_b64)
    except Exception:
        raise LicenseError("Invalid license: signature decode failed")

    if len(signature_bytes) != 64:
        raise LicenseError("Invalid license: signature must be 64 bytes")

    # Verify Ed25519 signature against embedded public key
    try:
        verify_key = VerifyKey(bytes.fromhex(_PUBLIC_KEY_HEX))
        verify_key.verify(payload_bytes, signature_bytes)
    except BadSignatureError:
        raise LicenseError("Invalid license: signature verification failed")
    except Exception as e:
        raise LicenseError(f"Invalid license: {e}")

    # Parse and validate payload
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise LicenseError("Invalid license: payload is not valid JSON")

    for required_field in ("org", "tier", "exp"):
        if required_field not in payload:
            raise LicenseError(f"Invalid license: missing required field '{required_field}'")

    if payload["tier"] != "team":
        raise LicenseError(f"Invalid license tier: '{payload['tier']}'")

    if payload["exp"] < now:
        raise LicenseError("License has expired")

    return LicenseInfo(
        org=payload["org"],
        tier=payload["tier"],
        max_users=payload.get("max_users", 0),
        features=payload.get("features", []),
        issued_at=payload.get("iat", 0),
        expires_at=payload["exp"],
    )


# --- Offline license generation utilities ---
# These functions are for administrative use only.
# The private signing key must NEVER be committed to version control.


def generate_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair for license signing.

    Returns:
        Tuple of (private_key_hex, public_key_hex).
    """
    signing_key = SigningKey.generate()
    return (
        signing_key.encode().hex(),
        signing_key.verify_key.encode().hex(),
    )


def create_license(
    signing_key_hex: str,
    org: str,
    *,
    max_users: int = 0,
    features: list[str] | None = None,
    duration_days: int = 365,
) -> str:
    """Create a signed license key.

    Args:
        signing_key_hex: Ed25519 private key in hex.
        org: Organization name.
        max_users: Maximum number of users (0 = unlimited).
        features: List of enabled feature flags.
        duration_days: License validity in days.

    Returns:
        A signed license key string.
    """
    now = int(time.time())
    payload = {
        "exp": now + (duration_days * 86400),
        "features": features or [],
        "iat": now,
        "max_users": max_users,
        "org": org,
        "tier": "team",
    }

    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

    signing_key = SigningKey(bytes.fromhex(signing_key_hex))
    signed = signing_key.sign(payload_bytes)

    return f"{LICENSE_PREFIX}.{_b64url_encode(payload_bytes)}.{_b64url_encode(signed.signature)}"
