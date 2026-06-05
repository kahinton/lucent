"""Tests for lucent.integrations.encryption — BackendCredentialEncryptor and protocol."""

from __future__ import annotations

from base64 import b64encode

import httpx
import pytest

from lucent.integrations.encryption import (
    BackendCredentialEncryptor,
    CredentialEncryptor,
    EncryptionError,
    VaultTransitBackend,
)


class _FakeClient:
    """Deterministic fake Vault HTTP client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._store: dict[str, str] = {}
        self._counter = 0

    def post(self, url: str, json: dict) -> "_FakeResponse":
        self.calls.append((url, json))
        if "/encrypt/" in url:
            self._counter += 1
            ct = f"vault:v1:ct{self._counter}"
            self._store[ct] = json["plaintext"]
            return _FakeResponse(200, {"data": {"ciphertext": ct}})
        elif "/decrypt/" in url:
            ct = json["ciphertext"]
            if ct not in self._store:
                return _FakeResponse(400)
            return _FakeResponse(200, {"data": {"plaintext": self._store[ct]}})
        return _FakeResponse(404)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://vault.test/v1/transit")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Vault request failed", request=request, response=response)

    def json(self) -> dict:
        return self._payload


def _make_encryptor() -> BackendCredentialEncryptor:
    client = _FakeClient()
    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="test-token",
        key_name="lucent-credentials",
        client=client,
    )
    return BackendCredentialEncryptor(backend=backend)


class TestBackendCredentialEncryptorRoundTrip:
    """Encrypt then decrypt should yield the original config."""

    @pytest.fixture()
    def encryptor(self) -> BackendCredentialEncryptor:
        return _make_encryptor()

    def test_round_trip_simple(self, encryptor: BackendCredentialEncryptor) -> None:
        config = {"bot_token": "xoxb-123", "signing_secret": "abc"}
        ciphertext = encryptor.encrypt(config)
        assert isinstance(ciphertext, bytes)
        assert ciphertext != b""
        result = encryptor.decrypt(ciphertext)
        assert result == config

    def test_round_trip_nested(self, encryptor: BackendCredentialEncryptor) -> None:
        config = {"a": {"b": [1, 2, 3]}, "c": True, "d": None}
        assert encryptor.decrypt(encryptor.encrypt(config)) == config

    def test_round_trip_empty_dict(self, encryptor: BackendCredentialEncryptor) -> None:
        assert encryptor.decrypt(encryptor.encrypt({})) == {}

    def test_deterministic_serialization(self, encryptor: BackendCredentialEncryptor) -> None:
        """sort_keys ensures same config always serializes identically."""
        config = {"z": 1, "a": 2, "m": 3}
        ct1 = encryptor.encrypt(config)
        ct2 = encryptor.encrypt(config)
        assert encryptor.decrypt(ct1) == encryptor.decrypt(ct2)


class TestBackendCredentialEncryptorErrors:
    """Error paths for encrypt/decrypt."""

    @pytest.fixture()
    def encryptor(self) -> BackendCredentialEncryptor:
        return _make_encryptor()

    def test_encrypt_unserializable_value(self, encryptor: BackendCredentialEncryptor) -> None:
        with pytest.raises(EncryptionError, match="serialize"):
            encryptor.encrypt({"fn": lambda x: x})  # type: ignore[dict-item]


class TestCredentialEncryptorProtocol:
    """BackendCredentialEncryptor satisfies the CredentialEncryptor protocol."""

    def test_isinstance_check(self) -> None:
        enc = _make_encryptor()
        assert isinstance(enc, CredentialEncryptor)

    def test_protocol_has_encrypt_decrypt(self) -> None:
        enc = _make_encryptor()
        assert hasattr(enc, "encrypt")
        assert hasattr(enc, "decrypt")
        assert callable(enc.encrypt)
        assert callable(enc.decrypt)
