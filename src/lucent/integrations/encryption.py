"""Credential encryption backends and adapters.

Supports pluggable encryption backends selected by ``ENCRYPTION_BACKEND``:

- ``fernet`` (default): local symmetric encryption using ``LUCENT_CREDENTIAL_KEY``
- ``vault_transit``: Vault/OpenBao transit API based encryption
"""

from __future__ import annotations

import json
import os
from base64 import b64decode, b64encode
from typing import Any, Protocol, runtime_checkable

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


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


def _make_fernet(key: str | bytes) -> Fernet:
    """Create a Fernet instance from a key, raising EncryptionError on failure."""
    if isinstance(key, str):
        key = key.encode()
    try:
        return Fernet(key)
    except (ValueError, Exception) as exc:
        raise EncryptionError(f"Invalid Fernet key: {exc}") from exc


class FernetBackend:
    """Fernet backend implementing :class:`EncryptionBackend`.

    Reads the key from LUCENT_CREDENTIAL_KEY (preferred) or
    LUCENT_ENCRYPTION_KEY (legacy fallback).

    The key must be a 32-byte URL-safe base64-encoded string, which is the
    format produced by ``Fernet.generate_key()``.

    Validates the key eagerly at construction time so misconfigurations
    surface on startup rather than on first encrypt/decrypt call.
    """

    ENV_VAR = "LUCENT_CREDENTIAL_KEY"
    ENV_VAR_LEGACY = "LUCENT_ENCRYPTION_KEY"

    def __init__(self, key: str | bytes | None = None) -> None:
        raw = key or os.environ.get(self.ENV_VAR) or os.environ.get(self.ENV_VAR_LEGACY)
        if not raw:
            raise EncryptionError(
                f"Encryption key not provided and {self.ENV_VAR} is not set. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        self._fernet = _make_fernet(raw)
        self._multi: MultiFernet | None = None

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext string with Fernet."""
        try:
            token = self._active_fernet.encrypt(plaintext.encode("utf-8"))
            return token.decode("ascii")
        except Exception as exc:
            raise EncryptionError(f"Encryption failed: {exc}") from exc

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt Fernet ciphertext string back to plaintext."""
        try:
            plaintext = self._active_fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except InvalidToken as exc:
            raise EncryptionError(
                "Decryption failed — wrong key or corrupted data"
            ) from exc

    def rotate_key(self, old_key: str | bytes, new_key: str | bytes) -> None:
        """Rotate to a new key while retaining ability to decrypt with old key."""
        new_fernet = _make_fernet(new_key)
        old_fernet = _make_fernet(old_key)
        self._fernet = new_fernet
        self._multi = MultiFernet([new_fernet, old_fernet])

    @property
    def _active_fernet(self) -> Fernet | MultiFernet:
        """Return MultiFernet if key rotation is active, else single Fernet."""
        return self._multi if self._multi is not None else self._fernet


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
    """Get configured encryption backend from ``ENCRYPTION_BACKEND``."""
    global _default_backend  # noqa: PLW0603
    selected = (backend_name or os.environ.get("ENCRYPTION_BACKEND", "fernet")).strip().lower()
    if _default_backend is not None and backend_name is None:
        return _default_backend

    if selected in {"", "fernet"}:
        backend: EncryptionBackend = FernetBackend()
    elif selected in {"vault", "transit", "vault_transit", "vault-transit"}:
        backend = VaultTransitBackend()
    else:
        raise EncryptionError(
            f"Unsupported ENCRYPTION_BACKEND='{selected}'. Use 'fernet' or 'vault_transit'."
        )
    if backend_name is None:
        _default_backend = backend
    return backend


def reset_default_encryption_backend() -> None:
    """Reset cached encryption backend (for tests)."""
    global _default_backend  # noqa: PLW0603
    _default_backend = None


class BackendCredentialEncryptor:
    """CredentialEncryptor adapter over a pluggable string backend."""

    def __init__(
        self,
        backend: EncryptionBackend | None = None,
        *,
        legacy_fernet: "FernetEncryptor | None" = None,
    ) -> None:
        self._backend = backend or get_encryption_backend()
        self._legacy_fernet = legacy_fernet
        if self._legacy_fernet is None and not isinstance(self._backend, FernetBackend):
            try:
                self._legacy_fernet = FernetEncryptor()
            except EncryptionError:
                self._legacy_fernet = None

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
        except (EncryptionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if self._legacy_fernet is not None and not isinstance(self._backend, FernetBackend):
                return self._legacy_fernet.decrypt(data)
            if isinstance(exc, EncryptionError):
                raise exc
            raise EncryptionError("Decryption failed with configured encryption backend") from exc

    def encrypt_str(self, plaintext: str) -> str:
        return self._backend.encrypt(plaintext)

    def decrypt_str(self, ciphertext: str) -> str:
        try:
            return self._backend.decrypt(ciphertext)
        except EncryptionError as exc:
            if self._legacy_fernet is not None and not isinstance(self._backend, FernetBackend):
                return self._legacy_fernet.decrypt_str(ciphertext)
            raise exc

    def rotate_key(self, old_key: str | bytes, new_key: str | bytes) -> None:
        if isinstance(self._backend, FernetBackend):
            self._backend.rotate_key(old_key=old_key, new_key=new_key)
            return
        raise EncryptionError("Key rotation is not supported for this backend")


class FernetEncryptor(BackendCredentialEncryptor):
    """Backwards-compatible credential encryptor using Fernet backend."""

    def __init__(self, key: str | bytes | None = None) -> None:
        super().__init__(backend=FernetBackend(key=key))

    @property
    def _fernet_backend(self) -> FernetBackend:
        backend = self._backend
        assert isinstance(backend, FernetBackend)
        return backend

    def rotate_key(self, old_key: str | bytes, new_key: str | bytes) -> None:
        self._fernet_backend.rotate_key(old_key=old_key, new_key=new_key)


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
    Uses LUCENT_CREDENTIAL_KEY (or LUCENT_ENCRYPTION_KEY) from the environment.
    """
    return _get_default_encryptor().encrypt_str(value)


def decrypt_credential(value: str) -> str:
    """Decrypt a credential string using the default encryptor.

    Expects a URL-safe base64-encoded ciphertext produced by encrypt_credential.
    Uses LUCENT_CREDENTIAL_KEY (or LUCENT_ENCRYPTION_KEY) from the environment.
    """
    return _get_default_encryptor().decrypt_str(value)


def reset_default_encryptor() -> None:
    """Reset the cached default encryptor (for testing)."""
    global _default_encryptor  # noqa: PLW0603
    _default_encryptor = None
    reset_default_encryption_backend()
