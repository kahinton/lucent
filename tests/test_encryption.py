"""Tests for credential encryption — VaultTransitBackend string API and helpers."""

from __future__ import annotations

from base64 import b64encode

import httpx
import pytest

from lucent.integrations.encryption import (
    BackendCredentialEncryptor,
    CredentialEncryptor,
    EncryptionError,
    VaultTransitBackend,
    decrypt_credential,
    encrypt_credential,
    reset_default_encryptor,
    reset_default_encryption_backend,
)


# ---------------------------------------------------------------------------
# Test helpers — fake Vault HTTP client
# ---------------------------------------------------------------------------


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
    """Deterministic fake Vault HTTP client for encrypt/decrypt."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._store: dict[str, str] = {}  # ciphertext -> b64-encoded plaintext
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


def _make_backend() -> tuple[VaultTransitBackend, _FakeClient]:
    client = _FakeClient()
    backend = VaultTransitBackend(
        vault_addr="http://vault.test",
        vault_token="test-token",
        key_name="lucent-credentials",
        client=client,
    )
    return backend, client


# ---------------------------------------------------------------------------
# String-based encrypt/decrypt
# ---------------------------------------------------------------------------


class TestEncryptStr:
    """encrypt_str / decrypt_str round-trip on individual strings via BackendCredentialEncryptor."""

    @pytest.fixture()
    def encryptor(self) -> BackendCredentialEncryptor:
        backend, _ = _make_backend()
        return BackendCredentialEncryptor(backend=backend)

    def test_round_trip(self, encryptor: BackendCredentialEncryptor) -> None:
        secret = "xoxb-slack-bot-token-12345"
        ciphertext = encryptor.encrypt_str(secret)
        assert isinstance(ciphertext, str)
        assert ciphertext != secret
        assert encryptor.decrypt_str(ciphertext) == secret

    def test_empty_string(self, encryptor: BackendCredentialEncryptor) -> None:
        assert encryptor.decrypt_str(encryptor.encrypt_str("")) == ""

    def test_unicode(self, encryptor: BackendCredentialEncryptor) -> None:
        value = "日本語テスト 🔐"
        assert encryptor.decrypt_str(encryptor.encrypt_str(value)) == value

    def test_decrypt_wrong_ciphertext_raises(self, encryptor: BackendCredentialEncryptor) -> None:
        with pytest.raises(EncryptionError):
            encryptor.decrypt_str("not-valid-ciphertext")


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """encrypt_credential / decrypt_credential module helpers."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_default_encryptor()
        reset_default_encryption_backend()
        monkeypatch.setenv("VAULT_ADDR", "http://vault.test")
        monkeypatch.setenv("VAULT_TOKEN", "test-token")
        yield
        reset_default_encryptor()
        reset_default_encryption_backend()

    def test_missing_vault_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_default_encryptor()
        reset_default_encryption_backend()
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        with pytest.raises(EncryptionError, match="VAULT_ADDR"):
            encrypt_credential("value")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """BackendCredentialEncryptor satisfies the CredentialEncryptor protocol."""

    def test_isinstance(self) -> None:
        backend, _ = _make_backend()
        enc = BackendCredentialEncryptor(backend=backend)
        assert isinstance(enc, CredentialEncryptor)

    def test_has_all_methods(self) -> None:
        backend, _ = _make_backend()
        enc = BackendCredentialEncryptor(backend=backend)
        for method in ("encrypt", "decrypt", "encrypt_str", "decrypt_str", "rotate_key"):
            assert hasattr(enc, method), f"Missing method: {method}"
            assert callable(getattr(enc, method))
