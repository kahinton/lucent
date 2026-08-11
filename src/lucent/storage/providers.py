"""Provider interface and built-in local storage for user files."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath


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


class FileStorageRegistry:
    """Resolves configured providers without coupling metadata to a backend."""

    def __init__(self, providers: list[FileStorageProvider] | None = None):
        provider_list = providers or [LocalFileStorageProvider()]
        self._providers = {provider.name: provider for provider in provider_list}

    def get(self, name: str) -> FileStorageProvider:
        provider = self._providers.get(name)
        if not provider:
            raise ValueError(f"Unknown file storage provider: {name}")
        return provider
