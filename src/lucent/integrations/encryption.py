"""Credential encryption backends and adapters.

Lucent requires a secret store (OpenBao/Vault) for credential encryption.
The transit backend encrypts/decrypts via the Vault API — the encryption
key never leaves the secret store.
"""

from __future__ import annotations

import json
import os
from base64 import b64decode, b64encode
from typing import Any, Protocol, runtime_checkable

import httpx


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


@runtime_checkable
class EncryptionBackend(Protocol):
    """String encryption backend abstraction."""

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext and return ciphertext."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext and return plaintext."""
        ...


@runtime_checkable
class CredentialEncryptor(Protocol):
    """Interface for encrypting/decrypting integration credential configs."""

    def encrypt(self, config: dict[str, Any]) -> bytes:
        """Serialize and encrypt a config dict.

        Returns opaque ciphertext bytes suitable for storing in
        the ``encrypted_config`` BYTEA column.
        """
        ...

    def decrypt(self, data: bytes) -> dict[str, Any]:
        """Decrypt ciphertext bytes back into a config dict.

        Raises EncryptionError if the data is corrupt or the key is wrong.
        """
        ...

    def encrypt_str(self, plaintext: str) -> str:
        """Encrypt a plaintext string.

        Returns a URL-safe base64-encoded ciphertext string.
        """
        ...

    def decrypt_str(self, ciphertext: str) -> str:
        """Decrypt a URL-safe base64-encoded ciphertext string.

        Raises EncryptionError if the data is corrupt or the key is wrong.
        """
        ...

    def rotate_key(self, old_key: str | bytes, new_key: str | bytes) -> None:
        """Rotate the encryption key.

        After rotation, new encryptions use *new_key*. Decryption tries
        *new_key* first, falling back to *old_key* for data encrypted
        before rotation.
        """
        ...


class VaultTransitBackend:
    """Vault/OpenBao transit backend implementing :class:`EncryptionBackend`."""

    def __init__(
        self,
        *,
        vault_addr: str | None = None,
        vault_token: str | None = None,
        key_name: str | None = None,
        mount: str | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._vault_addr = (vault_addr or os.environ.get("VAULT_ADDR", "")).rstrip("/")
        self._vault_token = vault_token or os.environ.get("VAULT_TOKEN", "")
        self._key_name = key_name or os.environ.get("VAULT_TRANSIT_KEY_NAME", "lucent-credentials")
        self._mount = mount or os.environ.get("VAULT_TRANSIT_MOUNT", "transit")

        if not self._vault_addr:
            raise EncryptionError("Vault transit backend requires VAULT_ADDR")
        if not self._vault_token:
            raise EncryptionError("Vault transit backend requires VAULT_TOKEN")

        headers = {"X-Vault-Token": self._vault_token}
        namespace = os.environ.get("VAULT_NAMESPACE")
        if namespace:
            headers["X-Vault-Namespace"] = namespace

        self._client = client or httpx.Client(
            base_url=self._vault_addr,
            headers=headers,
            timeout=timeout,
        )

    def encrypt(self, plaintext: str) -> str:
        encoded = b64encode(plaintext.encode("utf-8")).decode("ascii")
        endpoint = f"/v1/{self._mount}/encrypt/{self._key_name}"
        try:
            resp = self._client.post(endpoint, json={"plaintext": encoded})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EncryptionError(
                f"Vault transit encrypt request failed: {type(exc).__name__}"
            ) from exc
        try:
            return resp.json()["data"]["ciphertext"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EncryptionError("Vault transit encrypt response missing ciphertext") from exc

    def decrypt(self, ciphertext: str) -> str:
        endpoint = f"/v1/{self._mount}/decrypt/{self._key_name}"
        try:
            resp = self._client.post(endpoint, json={"ciphertext": ciphertext})
            resp.raise_for_status()
            encoded = resp.json()["data"]["plaintext"]
            return b64decode(encoded).decode("utf-8")
        except httpx.HTTPError as exc:
            raise EncryptionError(
                f"Vault transit decrypt request failed: {type(exc).__name__}"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise EncryptionError("Vault transit decrypt response missing plaintext") from exc
        except Exception as exc:
            raise EncryptionError(f"Vault transit decrypt decode failed: {exc}") from exc


_default_backend: EncryptionBackend | None = None


def get_encryption_backend(backend_name: str | None = None) -> EncryptionBackend:
    """Get the Vault transit encryption backend.

    Lucent requires a secret store (OpenBao/Vault) for credential encryption.
    The transit backend encrypts/decrypts via the Vault API — the encryption
    key never leaves the secret store.
    """
    global _default_backend  # noqa: PLW0603
    if _default_backend is not None and backend_name is None:
        return _default_backend

    backend: EncryptionBackend = VaultTransitBackend()
    if backend_name is None:
        _default_backend = backend
    return backend


def reset_default_encryption_backend() -> None:
    """Reset cached encryption backend (for tests)."""
    global _default_backend  # noqa: PLW0603
    _default_backend = None


class BackendCredentialEncryptor:
    """CredentialEncryptor adapter over a pluggable string backend."""

    def __init__(self, backend: EncryptionBackend | None = None) -> None:
        self._backend = backend or get_encryption_backend()

    def encrypt(self, config: dict[str, Any]) -> bytes:
        """Serialize config to JSON and encrypt through selected backend."""
        try:
            plaintext = json.dumps(config, separators=(",", ":"), sort_keys=True)
            return self._backend.encrypt(plaintext).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise EncryptionError(f"Failed to serialize config for encryption: {exc}") from exc

    def decrypt(self, data: bytes) -> dict[str, Any]:
        """Decrypt ciphertext bytes back into a config dict."""
        try:
            plaintext = self._backend.decrypt(data.decode("utf-8"))
            return json.loads(plaintext)
        except EncryptionError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EncryptionError("Decryption failed with configured encryption backend") from exc

    def encrypt_str(self, plaintext: str) -> str:
        return self._backend.encrypt(plaintext)

    def decrypt_str(self, ciphertext: str) -> str:
        return self._backend.decrypt(ciphertext)

    def rotate_key(self, old_key: str | bytes, new_key: str | bytes) -> None:
        raise EncryptionError("Key rotation is managed by the secret store")


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------

_default_encryptor: CredentialEncryptor | None = None


def _get_default_encryptor() -> CredentialEncryptor:
    """Lazily create a module-level credential encryptor from environment."""
    global _default_encryptor  # noqa: PLW0603
    if _default_encryptor is None:
        _default_encryptor = BackendCredentialEncryptor()
    return _default_encryptor


def encrypt_credential(value: str) -> str:
    """Encrypt a credential string using the default encryptor.

    Returns a URL-safe base64-encoded ciphertext string.
    Uses Vault transit backend for encryption.
    """
    return _get_default_encryptor().encrypt_str(value)


def decrypt_credential(value: str) -> str:
    """Decrypt a credential string using the default encryptor.

    Expects a URL-safe base64-encoded ciphertext produced by encrypt_credential.
    Uses Vault transit backend for decryption.
    """
    return _get_default_encryptor().decrypt_str(value)


def reset_default_encryptor() -> None:
    """Reset the cached default encryptor (for testing)."""
    global _default_encryptor  # noqa: PLW0603
    _default_encryptor = None
    reset_default_encryption_backend()
