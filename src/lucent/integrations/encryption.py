"""Credential encryption for integration configs (tokens, signing secrets).

Phase 1 uses Fernet symmetric encryption with a key from LUCENT_CREDENTIAL_KEY
(falls back to LUCENT_ENCRYPTION_KEY for backwards compatibility).
The CredentialEncryptor protocol allows swapping to envelope encryption (KMS)
in Phase 2 without changing callers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


@runtime_checkable
class CredentialEncryptor(Protocol):
    """Interface for encrypting/decrypting integration credential configs.

    Implementations must be synchronous — encryption is CPU-bound and fast.
    """

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


class FernetEncryptor:
    """Fernet-based credential encryptor.

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

    # --- dict-based API (for encrypted_config BYTEA column) ---

    def encrypt(self, config: dict[str, Any]) -> bytes:
        """Serialize config to JSON and encrypt with Fernet."""
        try:
            plaintext = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
            return self._active_fernet.encrypt(plaintext)
        except (TypeError, ValueError) as exc:
            raise EncryptionError(f"Failed to serialize config for encryption: {exc}") from exc

    def decrypt(self, data: bytes) -> dict[str, Any]:
        """Decrypt Fernet ciphertext and deserialize back to a config dict."""
        try:
            plaintext = self._active_fernet.decrypt(data)
            return json.loads(plaintext)
        except InvalidToken as exc:
            raise EncryptionError(
                "Decryption failed — wrong key or corrupted data"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EncryptionError(
                f"Decrypted data is not valid JSON config: {exc}"
            ) from exc

    # --- string-based API (for individual credential values) ---

    def encrypt_str(self, plaintext: str) -> str:
        """Encrypt a plaintext string, returning URL-safe base64."""
        try:
            token = self._active_fernet.encrypt(plaintext.encode("utf-8"))
            return token.decode("ascii")
        except Exception as exc:
            raise EncryptionError(f"Encryption failed: {exc}") from exc

    def decrypt_str(self, ciphertext: str) -> str:
        """Decrypt a URL-safe base64 ciphertext string back to plaintext."""
        try:
            plaintext = self._active_fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except InvalidToken as exc:
            raise EncryptionError(
                "Decryption failed — wrong key or corrupted data"
            ) from exc

    # --- key rotation ---

    def rotate_key(self, old_key: str | bytes, new_key: str | bytes) -> None:
        """Rotate to a new key while retaining ability to decrypt with old key.

        New encryptions use *new_key*. Decryption tries *new_key* first,
        then falls back to *old_key*.
        """
        new_fernet = _make_fernet(new_key)
        old_fernet = _make_fernet(old_key)
        self._fernet = new_fernet
        self._multi = MultiFernet([new_fernet, old_fernet])

    @property
    def _active_fernet(self) -> Fernet | MultiFernet:
        """Return MultiFernet if key rotation is active, else single Fernet."""
        return self._multi if self._multi is not None else self._fernet


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------

_default_encryptor: FernetEncryptor | None = None


def _get_default_encryptor() -> FernetEncryptor:
    """Lazily create a module-level FernetEncryptor from environment."""
    global _default_encryptor  # noqa: PLW0603
    if _default_encryptor is None:
        _default_encryptor = FernetEncryptor()
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
