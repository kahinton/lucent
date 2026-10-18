"""Provider interface and built-in local storage for user files."""

from __future__ import annotations

import asyncio
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath

from lucent.logging import get_logger

logger = get_logger(__name__)


class FileStorageProvider(ABC):
    """Byte storage boundary for local and external file providers."""

    name: str

    @abstractmethod
    async def put(self, storage_key: str, content: bytes) -> None:
        """Persist content at an opaque provider key."""

    @abstractmethod
    async def get(self, storage_key: str) -> bytes:
        """Load content from an opaque provider key."""

    @abstractmethod
    async def delete(self, storage_key: str) -> None:
        """Delete content if it exists."""

    async def close(self) -> None:
        """Release any long-lived client/credential held by the provider.

        Providers that own network clients override this. It is called once on
        process shutdown and must be safe to call more than once.
        """
        return None


class LocalFileStorageProvider(FileStorageProvider):
    """Filesystem-backed provider with atomic writes and contained paths."""

    name = "local"

    def __init__(self, root: str | Path | None = None):
        configured_root = root or os.environ.get("LUCENT_FILE_STORAGE_PATH", "./data/files")
        self.root = Path(configured_root).expanduser().resolve()

    def _path(self, storage_key: str) -> Path:
        key = PurePosixPath(str(storage_key or ""))
        if not storage_key or key.is_absolute() or ".." in key.parts:
            raise ValueError("Invalid storage key")
        candidate = (self.root / Path(*key.parts)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Invalid storage key")
        return candidate

    async def put(self, storage_key: str, content: bytes) -> None:
        path = self._path(storage_key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, path)

        await asyncio.to_thread(write)

    async def get(self, storage_key: str) -> bytes:
        return await asyncio.to_thread(self._path(storage_key).read_bytes)

    async def delete(self, storage_key: str) -> None:
        path = self._path(storage_key)

        def remove() -> None:
            path.unlink(missing_ok=True)

        await asyncio.to_thread(remove)


ENV_ACTIVE_PROVIDER = "LUCENT_FILE_STORAGE_PROVIDER"


def azure_blob_configured() -> bool:
    """True when the Azure provider has an account URL and container configured."""
    return bool(
        os.environ.get("LUCENT_AZURE_STORAGE_ACCOUNT_URL", "").strip()
        and os.environ.get("LUCENT_AZURE_STORAGE_CONTAINER", "").strip()
    )


def _default_providers() -> list[FileStorageProvider]:
    """Legacy ``local`` provider plus ``azure`` when it is configured.

    ``local`` is always registered so a row written before a provider switch
    (``provider='local'``) keeps resolving and reading. ``azure`` is added only
    when configured, so a deployment that does not use it never touches the
    Azure SDK import path.
    """
    providers: list[FileStorageProvider] = [LocalFileStorageProvider()]
    if azure_blob_configured():
        from lucent.storage.azure_blob import AzureBlobStorageProvider

        providers.append(AzureBlobStorageProvider())
    return providers


class FileStorageRegistry:
    """Resolves configured providers without coupling metadata to a backend.

    A single registry serves both jobs that must agree: resolving the provider
    named on a stored row (reads, deletes, and revisions) and naming the active
    provider for new files. Rows always carry their own ``provider``, so
    switching the active provider never breaks reads of rows written under the
    previous one.

    ``active()`` governs **new file creation only**. Revisions of an existing
    file intentionally stay on the provider recorded on that file's row
    (``UserFileService.update`` resolves ``current["provider"]``), including
    after the active provider setting has changed.
    """

    def __init__(
        self,
        providers: list[FileStorageProvider] | None = None,
        *,
        active: str | None = None,
    ):
        provider_list = providers if providers is not None else _default_providers()
        self._providers = {provider.name: provider for provider in provider_list}
        requested = (
            active if active is not None else os.environ.get(ENV_ACTIVE_PROVIDER, "local")
        )
        requested = str(requested or "local").strip().lower()
        if requested not in self._providers:
            # Fail closed: a config that names an unavailable provider must be
            # loud, not silently fall back and split writes across backends.
            raise ValueError(
                f"{ENV_ACTIVE_PROVIDER}={requested!r} is not available. "
                f"Registered providers: {', '.join(sorted(self._providers)) or 'none'}. "
                "Configure LUCENT_AZURE_STORAGE_ACCOUNT_URL and "
                "LUCENT_AZURE_STORAGE_CONTAINER to enable the 'azure' provider."
            )
        self._active = requested

    def get(self, name: str) -> FileStorageProvider:
        provider = self._providers.get(name)
        if not provider:
            raise ValueError(f"Unknown file storage provider: {name}")
        return provider

    async def close(self) -> None:
        """Close every registered provider (best effort, one failure isolated)."""
        seen: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await provider.close()
            except Exception:  # pragma: no cover - shutdown best effort
                logger.warning(
                    "Failed to close file storage provider %r",
                    provider.name,
                    exc_info=True,
                )

    @property
    def active_name(self) -> str:
        """Name of the provider new files are created on."""
        return self._active

    def active(self) -> FileStorageProvider:
        """Provider new files are created on (``LUCENT_FILE_STORAGE_PROVIDER``).

        Revisions of an existing file stay on that file's recorded provider.
        """
        return self._providers[self._active]


# One registry (and therefore one Azure provider, credential, and client) per
# process rather than one per UserFileService construction. Building these per
# request re-probed the credential chain, re-fetched tokens, and leaked aiohttp
# sessions because nothing closed them.
_process_registry: FileStorageRegistry | None = None
_process_registry_lock = threading.Lock()


def build_file_storage_registry() -> FileStorageRegistry:
    """Canonical factory for the process-wide registry.

    Registration (which providers can be resolved) and the active provider for
    new files both come from here, so callers never build a partial registry
    that disagrees with the rest of the process.

    The result is cached for the lifetime of the process: configuration is read
    once, on first use, and construction failures are not cached (so a
    misconfiguration keeps failing closed on every call rather than silently
    falling back). The registry and its providers are closed by
    :func:`close_file_storage_registry` on application shutdown.
    """
    global _process_registry
    with _process_registry_lock:
        if _process_registry is None:
            _process_registry = FileStorageRegistry()
        return _process_registry


async def close_file_storage_registry() -> None:
    """Close and drop the process-wide registry.

    Called from the API lifespan shutdown. Safe when no registry was ever
    built and safe to call more than once.
    """
    global _process_registry
    with _process_registry_lock:
        registry = _process_registry
        _process_registry = None
    if registry is not None:
        await registry.close()


def reset_file_storage_registry() -> None:
    """Drop the cached registry without closing it.

    Intended for tests that change storage configuration between cases. Because
    this cannot await provider ``close()``, callers replacing a registry that
    may hold an open Azure client should await
    :func:`close_file_storage_registry` first.
    """
    global _process_registry
    with _process_registry_lock:
        _process_registry = None
