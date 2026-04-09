"""Tests for lucent.integrations.encryption — FernetEncryptor and protocol."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from lucent.integrations.encryption import (
    CredentialEncryptor,
    EncryptionError,
    FernetEncryptor,
)


class TestFernetEncryptorInit:
    """Construction-time validation."""

    def test_init_with_explicit_key(self) -> None:
        key = Fernet.generate_key()
        enc = FernetEncryptor(key=key)
        assert enc is not None

    def test_init_with_str_key(self) -> None:
        key = Fernet.generate_key().decode()
        enc = FernetEncryptor(key=key)
        assert enc is not None

    def test_init_from_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("LUCENT_CREDENTIAL_KEY", key)
        enc = FernetEncryptor()
        assert enc is not None

    def test_init_from_legacy_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = Fernet.generate_key().decode()
        monkeypatch.delenv("LUCENT_CREDENTIAL_KEY", raising=False)
        monkeypatch.setenv("LUCENT_ENCRYPTION_KEY", key)
        enc = FernetEncryptor()
        assert enc is not None

    def test_init_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LUCENT_CREDENTIAL_KEY", raising=False)
        monkeypatch.delenv("LUCENT_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionError, match="not provided"):
            FernetEncryptor()

    def test_init_bad_key_raises(self) -> None:
        with pytest.raises(EncryptionError, match="Invalid Fernet key"):
            FernetEncryptor(key="not-a-valid-key")

    def test_init_empty_string_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LUCENT_CREDENTIAL_KEY", raising=False)
        monkeypatch.delenv("LUCENT_ENCRYPTION_KEY", raising=False)
        with pytest.raises(EncryptionError, match="not provided"):
            FernetEncryptor(key="")


class TestFernetEncryptorRoundTrip:
    """Encrypt then decrypt should yield the original config."""

    @pytest.fixture()
    def encryptor(self) -> FernetEncryptor:
        return FernetEncryptor(key=Fernet.generate_key())

    def test_round_trip_simple(self, encryptor: FernetEncryptor) -> None:
        config = {"bot_token": "xoxb-123", "signing_secret": "abc"}
        ciphertext = encryptor.encrypt(config)
        assert isinstance(ciphertext, bytes)
        assert ciphertext != b""
        result = encryptor.decrypt(ciphertext)
        assert result == config

    def test_round_trip_nested(self, encryptor: FernetEncryptor) -> None:
        config = {"a": {"b": [1, 2, 3]}, "c": True, "d": None}
        assert encryptor.decrypt(encryptor.encrypt(config)) == config

    def test_round_trip_empty_dict(self, encryptor: FernetEncryptor) -> None:
        assert encryptor.decrypt(encryptor.encrypt({})) == {}

    def test_deterministic_serialization(self, encryptor: FernetEncryptor) -> None:
        """sort_keys ensures same config always serializes identically."""
        config = {"z": 1, "a": 2, "m": 3}
        ct1 = encryptor.encrypt(config)
        ct2 = encryptor.encrypt(config)
        # Fernet adds random IV, so ciphertext differs — but plaintext is same
        assert encryptor.decrypt(ct1) == encryptor.decrypt(ct2)


class TestFernetEncryptorErrors:
    """Error paths for encrypt/decrypt."""

    @pytest.fixture()
    def encryptor(self) -> FernetEncryptor:
        return FernetEncryptor(key=Fernet.generate_key())

    def test_decrypt_wrong_key(self) -> None:
        enc1 = FernetEncryptor(key=Fernet.generate_key())
        enc2 = FernetEncryptor(key=Fernet.generate_key())
        ciphertext = enc1.encrypt({"secret": "value"})
        with pytest.raises(EncryptionError, match="wrong key or corrupted"):
            enc2.decrypt(ciphertext)

    def test_decrypt_garbage_data(self, encryptor: FernetEncryptor) -> None:
        with pytest.raises(EncryptionError):
            encryptor.decrypt(b"definitely not valid ciphertext")

    def test_encrypt_unserializable_value(self, encryptor: FernetEncryptor) -> None:
        with pytest.raises(EncryptionError, match="serialize"):
            encryptor.encrypt({"fn": lambda x: x})  # type: ignore[dict-item]


class TestCredentialEncryptorProtocol:
    """FernetEncryptor satisfies the CredentialEncryptor protocol."""

    def test_isinstance_check(self) -> None:
        enc = FernetEncryptor(key=Fernet.generate_key())
        assert isinstance(enc, CredentialEncryptor)

    def test_protocol_has_encrypt_decrypt(self) -> None:
        enc = FernetEncryptor(key=Fernet.generate_key())
        assert hasattr(enc, "encrypt")
        assert hasattr(enc, "decrypt")
        assert callable(enc.encrypt)
        assert callable(enc.decrypt)
