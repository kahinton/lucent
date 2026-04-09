"""Tests for credential encryption — string API, key rotation, and helpers."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from lucent.integrations.encryption import (
    CredentialEncryptor,
    EncryptionError,
    FernetEncryptor,
    decrypt_credential,
    encrypt_credential,
    reset_default_encryptor,
)

# ---------------------------------------------------------------------------
# String-based encrypt/decrypt
# ---------------------------------------------------------------------------


class TestEncryptStr:
    """encrypt_str / decrypt_str round-trip on individual strings."""

    @pytest.fixture()
    def encryptor(self) -> FernetEncryptor:
        return FernetEncryptor(key=Fernet.generate_key())

    def test_round_trip(self, encryptor: FernetEncryptor) -> None:
        secret = "xoxb-slack-bot-token-12345"
        ciphertext = encryptor.encrypt_str(secret)
        assert isinstance(ciphertext, str)
        assert ciphertext != secret
        assert encryptor.decrypt_str(ciphertext) == secret

    def test_returns_url_safe_base64(self, encryptor: FernetEncryptor) -> None:
        ciphertext = encryptor.encrypt_str("test-value")
        # Fernet tokens are URL-safe base64 — only [A-Za-z0-9_=-] characters
        assert all(c.isalnum() or c in "-_=" for c in ciphertext)

    def test_empty_string(self, encryptor: FernetEncryptor) -> None:
        assert encryptor.decrypt_str(encryptor.encrypt_str("")) == ""

    def test_unicode(self, encryptor: FernetEncryptor) -> None:
        value = "日本語テスト 🔐"
        assert encryptor.decrypt_str(encryptor.encrypt_str(value)) == value

    def test_decrypt_wrong_key(self) -> None:
        enc1 = FernetEncryptor(key=Fernet.generate_key())
        enc2 = FernetEncryptor(key=Fernet.generate_key())
        ct = enc1.encrypt_str("secret")
        with pytest.raises(EncryptionError, match="wrong key or corrupted"):
            enc2.decrypt_str(ct)

    def test_decrypt_garbage(self, encryptor: FernetEncryptor) -> None:
        with pytest.raises(EncryptionError):
            encryptor.decrypt_str("not-valid-ciphertext")


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


class TestKeyRotation:
    """rotate_key should allow decrypting data from both old and new keys."""

    def test_rotate_decrypts_old_data(self) -> None:
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        enc = FernetEncryptor(key=old_key)
        old_ct = enc.encrypt_str("secret-before-rotation")

        enc.rotate_key(old_key=old_key, new_key=new_key)
        assert enc.decrypt_str(old_ct) == "secret-before-rotation"

    def test_rotate_encrypts_with_new_key(self) -> None:
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        enc = FernetEncryptor(key=old_key)
        enc.rotate_key(old_key=old_key, new_key=new_key)

        new_ct = enc.encrypt_str("secret-after-rotation")
        # A fresh encryptor with only the new key should decrypt it
        new_only = FernetEncryptor(key=new_key)
        assert new_only.decrypt_str(new_ct) == "secret-after-rotation"

    def test_rotate_decrypts_new_data(self) -> None:
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        enc = FernetEncryptor(key=old_key)
        enc.rotate_key(old_key=old_key, new_key=new_key)

        ct = enc.encrypt_str("after-rotation")
        assert enc.decrypt_str(ct) == "after-rotation"

    def test_rotate_dict_api(self) -> None:
        """Key rotation also works with the dict-based encrypt/decrypt."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        enc = FernetEncryptor(key=old_key)
        old_ct = enc.encrypt({"token": "xoxb-123"})

        enc.rotate_key(old_key=old_key, new_key=new_key)
        assert enc.decrypt(old_ct) == {"token": "xoxb-123"}

    def test_rotate_bad_key_raises(self) -> None:
        enc = FernetEncryptor(key=Fernet.generate_key())
        with pytest.raises(EncryptionError, match="Invalid Fernet key"):
            enc.rotate_key(old_key="bad", new_key=Fernet.generate_key())


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """encrypt_credential / decrypt_credential module helpers."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_default_encryptor()
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("LUCENT_CREDENTIAL_KEY", key)
        yield
        reset_default_encryptor()

    def test_round_trip(self) -> None:
        ct = encrypt_credential("my-secret-token")
        assert isinstance(ct, str)
        assert decrypt_credential(ct) == "my-secret-token"

    def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_default_encryptor()
        monkeypatch.delenv("LUCENT_CREDENTIAL_KEY", raising=False)
        monkeypatch.delenv("LUCENT_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionError, match="not set"):
            encrypt_credential("value")

    def test_uses_credential_key_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LUCENT_CREDENTIAL_KEY takes precedence over LUCENT_ENCRYPTION_KEY."""
        reset_default_encryptor()
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("LUCENT_CREDENTIAL_KEY", key)
        monkeypatch.delenv("LUCENT_ENCRYPTION_KEY", raising=False)
        ct = encrypt_credential("test")
        assert decrypt_credential(ct) == "test"

    def test_falls_back_to_legacy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_default_encryptor()
        key = Fernet.generate_key().decode()
        monkeypatch.delenv("LUCENT_CREDENTIAL_KEY", raising=False)
        monkeypatch.setenv("LUCENT_ENCRYPTION_KEY", key)
        ct = encrypt_credential("test")
        assert decrypt_credential(ct) == "test"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """FernetEncryptor satisfies the CredentialEncryptor protocol."""

    def test_isinstance(self) -> None:
        enc = FernetEncryptor(key=Fernet.generate_key())
        assert isinstance(enc, CredentialEncryptor)

    def test_has_all_methods(self) -> None:
        enc = FernetEncryptor(key=Fernet.generate_key())
        for method in ("encrypt", "decrypt", "encrypt_str", "decrypt_str", "rotate_key"):
            assert hasattr(enc, method), f"Missing method: {method}"
            assert callable(getattr(enc, method))
