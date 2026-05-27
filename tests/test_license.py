"""Tests for cryptographic license validation."""

import json
import time

import pytest
from nacl.signing import SigningKey

from lucent.license import (
    LICENSE_PREFIX,
    LicenseError,
    LicenseInfo,
    _b64url_encode,
    create_license,
    generate_keypair,
    validate_license,
)

# Test keypair — matches the production public key embedded in license.py.
# This private key is ONLY for tests; production key is stored offline.
_TEST_PRIVATE_KEY_HEX = "eeddca8cb93457f6e6064745285738aef75c9a281d876677c2fb4690fca5b095"


def _make_license(
    org: str = "test-org",
    max_users: int = 10,
    features: list[str] | None = None,
    duration_days: int = 365,
    signing_key_hex: str = _TEST_PRIVATE_KEY_HEX,
) -> str:
    """Helper to create a test license."""
    return create_license(
        signing_key_hex,
        org,
        max_users=max_users,
        features=features or ["rbac", "audit"],
        duration_days=duration_days,
    )


def _make_license_raw(
    payload: dict,
    signing_key_hex: str = _TEST_PRIVATE_KEY_HEX,
    prefix: str = LICENSE_PREFIX,
) -> str:
    """Helper to create a license from raw payload dict (for edge case tests)."""
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signing_key = SigningKey(bytes.fromhex(signing_key_hex))
    signed = signing_key.sign(payload_bytes)
    return f"{prefix}.{_b64url_encode(payload_bytes)}.{_b64url_encode(signed.signature)}"


class TestValidLicense:
    """Tests for successfully validating licenses."""

    def test_valid_license(self):
        key = _make_license()
        info = validate_license(key)
        assert isinstance(info, LicenseInfo)
        assert info.org == "test-org"
        assert info.tier == "team"
        assert info.max_users == 10
        assert info.features == ["rbac", "audit"]
        assert info.expires_at > time.time()

    def test_valid_license_minimal_payload(self):
        now = int(time.time())
        payload = {"org": "minimal", "tier": "team", "exp": now + 86400}
        key = _make_license_raw(payload)
        info = validate_license(key)
        assert info.org == "minimal"
        assert info.max_users == 0
        assert info.features == []

    def test_roundtrip_with_generate_keypair(self):
        priv, pub = generate_keypair()
        assert len(priv) == 64  # 32 bytes hex
        assert len(pub) == 64

    def test_create_and_validate(self):
        key = _make_license(org="acme-corp", max_users=50, features=["sharing"])
        info = validate_license(key)
        assert info.org == "acme-corp"
        assert info.max_users == 50
        assert info.features == ["sharing"]

    def test_whitespace_stripped(self):
        key = "  " + _make_license() + "  "
        info = validate_license(key)
        assert info.org == "test-org"


class TestInvalidFormat:
    """Tests for malformed license keys."""

    def test_empty_string(self):
        with pytest.raises(LicenseError, match="expected LCT-V1"):
            validate_license("")

    def test_random_string(self):
        with pytest.raises(LicenseError, match="expected LCT-V1"):
            validate_license("not-a-license-key")

    def test_wrong_prefix(self):
        with pytest.raises(LicenseError, match="Invalid license prefix"):
            validate_license("LCT-V2.abc.def")

    def test_missing_signature(self):
        with pytest.raises(LicenseError, match="expected LCT-V1"):
            validate_license("LCT-V1.payload-only")

    def test_extra_parts(self):
        with pytest.raises(LicenseError, match="expected LCT-V1"):
            validate_license("LCT-V1.a.b.c")

    def test_invalid_base64_payload(self):
        with pytest.raises(LicenseError, match="signature must be 64 bytes"):
            validate_license("LCT-V1.!!!invalid!!!.abc")

    def test_invalid_base64_signature(self):
        key = _make_license()
        parts = key.split(".")
        with pytest.raises(LicenseError, match="signature must be 64 bytes"):
            validate_license(f"{parts[0]}.{parts[1]}.!!!invalid!!!")


class TestSignatureVerification:
    """Tests for Ed25519 signature verification."""

    def test_tampered_payload(self):
        key = _make_license()
        parts = key.split(".")
        # Tamper by replacing one char in the payload
        tampered = parts[1][:-1] + ("A" if parts[1][-1] != "A" else "B")
        with pytest.raises(LicenseError):
            validate_license(f"{parts[0]}.{tampered}.{parts[2]}")

    def test_wrong_signing_key(self):
        """License signed with a different key should fail."""
        other_key = SigningKey.generate()
        now = int(time.time())
        payload = {"org": "evil", "tier": "team", "exp": now + 86400}
        key = _make_license_raw(payload, signing_key_hex=other_key.encode().hex())
        with pytest.raises(LicenseError, match="signature verification failed"):
            validate_license(key)

    def test_swapped_signature(self):
        """Signature from one license doesn't validate another."""
        key1 = _make_license(org="org-one")
        key2 = _make_license(org="org-two")
        parts1 = key1.split(".")
        parts2 = key2.split(".")
        # Use payload from key1 with signature from key2
        with pytest.raises(LicenseError, match="signature verification failed"):
            validate_license(f"{parts1[0]}.{parts1[1]}.{parts2[2]}")


class TestExpiration:
    """Tests for license expiration."""

    def test_expired_license(self):
        now = int(time.time())
        payload = {"org": "expired-org", "tier": "team", "exp": now - 3600, "iat": now - 86400}
        key = _make_license_raw(payload)
        with pytest.raises(LicenseError, match="expired"):
            validate_license(key)

    def test_just_expired(self):
        now = int(time.time())
        payload = {"org": "just-expired", "tier": "team", "exp": now - 1}
        key = _make_license_raw(payload)
        with pytest.raises(LicenseError, match="expired"):
            validate_license(key)

    def test_not_yet_expired(self):
        now = int(time.time())
        payload = {"org": "still-valid", "tier": "team", "exp": now + 3600}
        key = _make_license_raw(payload)
        info = validate_license(key)
        assert info.org == "still-valid"

    def test_now_override_for_testing(self):
        """The _now parameter allows deterministic time testing."""
        now = int(time.time())
        payload = {"org": "time-test", "tier": "team", "exp": now + 100}
        key = _make_license_raw(payload)
        # Valid at now
        info = validate_license(key, _now=now)
        assert info.org == "time-test"
        # Expired in the future
        with pytest.raises(LicenseError, match="expired"):
            validate_license(key, _now=now + 200)


class TestPayloadValidation:
    """Tests for payload field validation."""

    def test_missing_org(self):
        now = int(time.time())
        payload = {"tier": "team", "exp": now + 86400}
        key = _make_license_raw(payload)
        with pytest.raises(LicenseError, match="missing required field 'org'"):
            validate_license(key)

    def test_missing_tier(self):
        now = int(time.time())
        payload = {"org": "test", "exp": now + 86400}
        key = _make_license_raw(payload)
        with pytest.raises(LicenseError, match="missing required field 'tier'"):
            validate_license(key)

    def test_missing_exp(self):
        payload = {"org": "test", "tier": "team"}
        key = _make_license_raw(payload)
        with pytest.raises(LicenseError, match="missing required field 'exp'"):
            validate_license(key)

    def test_wrong_tier(self):
        now = int(time.time())
        payload = {"org": "test", "tier": "enterprise", "exp": now + 86400}
        key = _make_license_raw(payload)
        with pytest.raises(LicenseError, match="Invalid license tier"):
            validate_license(key)


class TestLicenseCreation:
    """Tests for the create_license utility."""

    def test_create_license_format(self):
        key = _make_license()
        parts = key.split(".")
        assert len(parts) == 3
        assert parts[0] == LICENSE_PREFIX

    def test_create_license_payload_sorted_keys(self):
        """Payload JSON uses sorted keys for deterministic signatures."""
        key = _make_license(org="sorted-test", features=["z", "a"])
        parts = key.split(".")
        payload = json.loads(_b64url_decode_helper(parts[1]))
        keys = list(payload.keys())
        assert keys == sorted(keys)

    def test_create_license_custom_duration(self):
        now = time.time()
        key = _make_license(duration_days=30)
        info = validate_license(key)
        # Should expire in roughly 30 days
        assert info.expires_at > now + (29 * 86400)
        assert info.expires_at < now + (31 * 86400)


def _b64url_decode_helper(s: str) -> bytes:
    """Decode base64url for test assertions."""
    import base64

    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
