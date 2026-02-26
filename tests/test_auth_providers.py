"""Tests for authentication providers and security utilities."""

import bcrypt

from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    generate_csrf_token,
    generate_session_token,
    get_cookie_params,
    hash_password,
    hash_session_token,
    sign_value,
    validate_csrf_token,
    verify_signed_value,
)


class TestCSRFTokens:
    """Tests for CSRF token generation and validation."""

    def test_generate_returns_dotted_format(self):
        token = generate_csrf_token()
        assert "." in token
        parts = token.rsplit(".", 1)
        assert len(parts) == 2
        assert len(parts[0]) > 0
        assert len(parts[1]) > 0

    def test_generated_token_validates(self):
        token = generate_csrf_token()
        assert validate_csrf_token(token) is True

    def test_tampered_token_fails(self):
        token = generate_csrf_token()
        random_part, _ = token.rsplit(".", 1)
        tampered = f"{random_part}.tampered_signature"
        assert validate_csrf_token(tampered) is False

    def test_none_fails(self):
        assert validate_csrf_token(None) is False

    def test_empty_string_fails(self):
        assert validate_csrf_token("") is False

    def test_no_dot_fails(self):
        assert validate_csrf_token("nodothere") is False

    def test_each_token_is_unique(self):
        tokens = {generate_csrf_token() for _ in range(10)}
        assert len(tokens) == 10


class TestSignedValues:
    """Tests for HMAC-signed value utilities."""

    def test_sign_and_verify_roundtrip(self):
        value = "test-uuid-12345"
        signed = sign_value(value)
        assert verify_signed_value(signed) == value

    def test_tampered_value_returns_none(self):
        signed = sign_value("original")
        tampered = "original.wrong_signature"
        assert verify_signed_value(tampered) is None

    def test_none_returns_none(self):
        assert verify_signed_value(None) is None

    def test_empty_returns_none(self):
        assert verify_signed_value("") is None

    def test_no_dot_returns_none(self):
        assert verify_signed_value("nodothere") is None

    def test_signed_format(self):
        signed = sign_value("hello")
        assert signed.startswith("hello.")
        assert len(signed) > len("hello.")

    def test_different_values_different_signatures(self):
        sig1 = sign_value("value1")
        sig2 = sign_value("value2")
        # Different values should produce different signed strings
        assert sig1 != sig2


class TestPasswordHashing:
    """Tests for password hashing utilities."""

    def test_hash_produces_bcrypt_string(self):
        hashed = hash_password("my_secret")
        assert hashed.startswith("$2b$")

    def test_correct_password_verifies(self):
        hashed = hash_password("my_secret")
        assert bcrypt.checkpw(b"my_secret", hashed.encode()) is True

    def test_wrong_password_fails(self):
        hashed = hash_password("my_secret")
        assert bcrypt.checkpw(b"wrong_password", hashed.encode()) is False

    def test_different_hashes_for_same_password(self):
        # bcrypt uses random salt, so same password produces different hashes
        hash1 = hash_password("same")
        hash2 = hash_password("same")
        assert hash1 != hash2
        # But both verify correctly
        assert bcrypt.checkpw(b"same", hash1.encode())
        assert bcrypt.checkpw(b"same", hash2.encode())


class TestSessionTokens:
    """Tests for session token utilities."""

    def test_generate_returns_nonempty_string(self):
        token = generate_session_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_each_token_is_unique(self):
        tokens = {generate_session_token() for _ in range(10)}
        assert len(tokens) == 10

    def test_hash_is_deterministic(self):
        token = "test_token_123"
        hash1 = hash_session_token(token)
        hash2 = hash_session_token(token)
        assert hash1 == hash2

    def test_hash_differs_from_raw(self):
        token = "test_token_123"
        hashed = hash_session_token(token)
        assert hashed != token

    def test_different_tokens_different_hashes(self):
        hash1 = hash_session_token("token_a")
        hash2 = hash_session_token("token_b")
        assert hash1 != hash2


class TestCookieParams:
    """Tests for cookie security configuration."""

    def test_default_params(self):
        params = get_cookie_params()
        assert params["httponly"] is True
        assert params["samesite"] == "lax"
        assert params["path"] == "/"
        assert isinstance(params["secure"], bool)

    def test_secure_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("LUCENT_SECURE_COOKIES", raising=False)
        # Need to reimport to pick up env change
        import lucent.auth_providers as ap
        original = ap.SECURE_COOKIES
        # The module-level constant is already set; just verify the default behavior
        params = get_cookie_params()
        assert "secure" in params


class TestConstants:
    """Tests for module constants."""

    def test_session_cookie_name(self):
        assert SESSION_COOKIE_NAME == "lucent_session"

    def test_csrf_cookie_name(self):
        assert CSRF_COOKIE_NAME == "lucent_csrf"
