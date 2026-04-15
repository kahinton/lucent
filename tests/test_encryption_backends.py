"""Tests for pluggable encryption backends."""

from __future__ import annotations

from base64 import b64encode

import httpx
import pytest

from lucent.integrations.encryption import (
    BackendCredentialEncryptor,
    EncryptionError,
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
    """Deterministic fake Vault HTTP client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._store: dict[str, str] = {}
        self._counter = 0

    def post(self, url: str, json: dict) -> _FakeResponse:
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


class _FailingVaultBackend:
    def encrypt(self, plaintext: str) -> str:
        raise EncryptionError("nope")

    def decrypt(self, ciphertext: str) -> str:
        raise EncryptionError("nope")


def test_vault_transit_backend_encrypt_decrypt_round_trip() -> None:
    client = _FakeClient()
    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="token",
        key_name="lucent-credentials",
        client=client,
    )

    ciphertext = backend.encrypt("hello")
    assert ciphertext == "vault:v1:ct1"
    assert client.calls[0][0] == "/v1/transit/encrypt/lucent-credentials"
    assert client.calls[0][1]["plaintext"] == b64encode(b"hello").decode("ascii")

    plaintext = backend.decrypt(ciphertext)
    assert plaintext == "hello"
    assert client.calls[1][0] == "/v1/transit/decrypt/lucent-credentials"
    assert client.calls[1][1]["ciphertext"] == "vault:v1:ct1"


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


def test_backend_factory_returns_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_default_encryption_backend()
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test")
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    backend = get_encryption_backend()
    assert isinstance(backend, VaultTransitBackend)
    reset_default_encryption_backend()


def test_backend_factory_missing_vault_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_default_encryption_backend()
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(EncryptionError, match="VAULT_ADDR"):
        get_encryption_backend()
    reset_default_encryption_backend()


def test_backend_credential_encryptor_contract() -> None:
    client = _FakeClient()
    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="token",
        client=client,
    )
    encryptor = BackendCredentialEncryptor(backend=backend)
    payload = {"access_token": "token", "refresh_token": "refresh"}
    ciphertext = encryptor.encrypt(payload)
    assert isinstance(ciphertext, bytes)
    assert encryptor.decrypt(ciphertext) == payload


def test_backend_credential_encryptor_decrypt_failure_raises() -> None:
    encryptor = BackendCredentialEncryptor(backend=_FailingVaultBackend())
    with pytest.raises(EncryptionError, match="nope"):
        encryptor.decrypt(b"some-ciphertext")


def test_backend_credential_encryptor_rotate_key_raises() -> None:
    client = _FakeClient()
    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="token",
        client=client,
    )
    encryptor = BackendCredentialEncryptor(backend=backend)
    with pytest.raises(EncryptionError, match="managed by the secret store"):
        encryptor.rotate_key("old", "new")
