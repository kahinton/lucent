"""Tests for pluggable encryption backends."""

from __future__ import annotations

from base64 import b64encode

import httpx
import pytest
from cryptography.fernet import Fernet

from lucent.integrations.encryption import (
    BackendCredentialEncryptor,
    EncryptionError,
    FernetBackend,
    FernetEncryptor,
    VaultTransitBackend,
    get_encryption_backend,
    reset_default_encryption_backend,
)


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


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict) -> _FakeResponse:
        self.calls.append((url, json))
        return self._responses.pop(0)


class _FailingVaultBackend:
    def encrypt(self, plaintext: str) -> str:
        raise EncryptionError("nope")

    def decrypt(self, ciphertext: str) -> str:
        raise EncryptionError("nope")


def test_fernet_backend_round_trip() -> None:
    backend = FernetBackend(key=Fernet.generate_key())
    ciphertext = backend.encrypt("hello")
    assert backend.decrypt(ciphertext) == "hello"


def test_vault_transit_backend_encrypt_decrypt_round_trip() -> None:
    responses = [
        _FakeResponse(200, {"data": {"ciphertext": "vault:v1:abc123"}}),
        _FakeResponse(200, {"data": {"plaintext": b64encode(b"hello").decode("ascii")}}),
    ]
    client = _FakeClient(responses)
    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="token",
        key_name="lucent-credentials",
        client=client,
    )

    ciphertext = backend.encrypt("hello")
    assert ciphertext == "vault:v1:abc123"
    assert client.calls[0][0] == "/v1/transit/encrypt/lucent-credentials"
    assert client.calls[0][1]["plaintext"] == b64encode(b"hello").decode("ascii")

    plaintext = backend.decrypt(ciphertext)
    assert plaintext == "hello"
    assert client.calls[1][0] == "/v1/transit/decrypt/lucent-credentials"
    assert client.calls[1][1]["ciphertext"] == "vault:v1:abc123"


def test_vault_transit_backend_missing_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(EncryptionError, match="VAULT_ADDR"):
        VaultTransitBackend()


def test_vault_transit_backend_unreachable_raises() -> None:
    class _UnreachableClient:
        def post(self, url: str, json: dict) -> _FakeResponse:
            raise httpx.ConnectError("no route", request=httpx.Request("POST", url))

    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="token",
        client=_UnreachableClient(),
    )
    with pytest.raises(EncryptionError, match="encrypt request failed"):
        backend.encrypt("hello")


def test_backend_factory_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_default_encryption_backend()
    monkeypatch.setenv("ENCRYPTION_BACKEND", "fernet")
    monkeypatch.setenv("LUCENT_CREDENTIAL_KEY", Fernet.generate_key().decode())
    backend = get_encryption_backend()
    assert isinstance(backend, FernetBackend)


def test_backend_factory_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_default_encryption_backend()
    monkeypatch.setenv("ENCRYPTION_BACKEND", "unknown")
    with pytest.raises(EncryptionError, match="Unsupported ENCRYPTION_BACKEND"):
        get_encryption_backend()


def test_backend_credential_encryptor_contract() -> None:
    encryptor = BackendCredentialEncryptor(backend=FernetBackend(key=Fernet.generate_key()))
    payload = {"access_token": "token", "refresh_token": "refresh"}
    ciphertext = encryptor.encrypt(payload)
    assert isinstance(ciphertext, bytes)
    assert encryptor.decrypt(ciphertext) == payload


def test_backend_credential_encryptor_fallback_to_legacy_fernet() -> None:
    legacy = FernetEncryptor(key=Fernet.generate_key())
    legacy_ciphertext = legacy.encrypt({"token": "legacy"})

    encryptor = BackendCredentialEncryptor(
        backend=_FailingVaultBackend(),
        legacy_fernet=legacy,
    )
    assert encryptor.decrypt(legacy_ciphertext) == {"token": "legacy"}
