"""Azure Blob Storage provider for user file content.

Authentication goes through ``DefaultAzureCredential``'s credential chain:
managed identity (system- or user-assigned via ``AZURE_CLIENT_ID``), then the
environment/CLI/developer credentials that credential class also consults. It
**never** uses a storage account key, connection string, or SAS token — those
credential paths are not implemented and must not be added. The Azure SDKs are
imported lazily inside :func:`_build_container_client` so a local install that
never selects the Azure provider never loads them.

The account URL is validated at construction to be an
``https://<account>.blob.core.windows.net`` endpoint, and the configured
container is probed once per provider. A missing or inaccessible container is a
configuration/identity failure and raises
:class:`AzureContainerUnavailableError` loudly — it is never translated into
"file not found", which would otherwise make a typo in either environment
variable look like every user's files had vanished.
"""

from __future__ import annotations

import inspect
import os
import re
import threading
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from lucent.logging import get_logger
from lucent.storage.providers import FileStorageProvider

logger = get_logger(__name__)

ENV_ACCOUNT_URL = "LUCENT_AZURE_STORAGE_ACCOUNT_URL"
ENV_CONTAINER = "LUCENT_AZURE_STORAGE_CONTAINER"

# An Azure Blob account endpoint is exactly one label under
# blob.core.windows.net. This deliberately rejects custom domains, sovereign
# cloud suffixes, userinfo, explicit ports, paths, and query strings: the
# account the bytes land in must be unambiguous.
_ACCOUNT_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.blob\.core\.windows\.net$"
)


class AzureContainerUnavailableError(RuntimeError):
    """The configured storage container is missing or not accessible.

    This is intentionally *not* ``FileNotFoundError``: it signals a broken
    deployment (wrong account, container, or identity) rather than a missing
    object, and the service/API therefore surface it as a server error.
    """


class _BuiltClient(NamedTuple):
    """Resources built for one provider, retained so shutdown can close them."""

    container: Any
    service: Any
    credential: Any


def _validate_account_url(account_url: str) -> str:
    """Validate and normalise an Azure Blob account endpoint.

    Accepted form (case-insensitive, optional single trailing slash)::

        https://<account>.blob.core.windows.net

    Everything else is rejected: non-HTTPS schemes, other hosts (including
    lookalikes such as ``blob.core.windows.net.example.com``), embedded
    credentials, explicit ports, non-root paths, query strings, and fragments.
    Returns the canonical ``https://<account>.blob.core.windows.net`` form.
    """
    raw = str(account_url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError(
            f"{ENV_ACCOUNT_URL} must use https, got {raw!r}. "
            "Expected https://<account>.blob.core.windows.net"
        )
    if parsed.username or parsed.password:
        raise ValueError(
            f"{ENV_ACCOUNT_URL} must not embed credentials: {raw!r}"
        )
    if parsed.port is not None:
        raise ValueError(
            f"{ENV_ACCOUNT_URL} must not specify a port: {raw!r}. "
            "Expected https://<account>.blob.core.windows.net"
        )
    host = (parsed.hostname or "").lower()
    if not _ACCOUNT_HOST_RE.fullmatch(host):
        raise ValueError(
            f"{ENV_ACCOUNT_URL} host must be <account>.blob.core.windows.net, "
            f"got {host or raw!r}"
        )
    if parsed.path not in ("", "/"):
        raise ValueError(
            f"{ENV_ACCOUNT_URL} must not include a path: {raw!r}"
        )
    if parsed.query or parsed.fragment:
        raise ValueError(
            f"{ENV_ACCOUNT_URL} must not include a query or fragment: {raw!r}"
        )
    return f"https://{host}"


def _blob_name(storage_key: str) -> str:
    """Validate a server-generated storage key and return it as a blob name.

    SECURITY: ``storage_key`` is *always* generated server-side by
    :class:`~lucent.storage.service.UserFileService` (``org/user/file_id`` plus
    ``/revisions/<uuid>``). It must **never** be taken from a request path or
    body. Azure treats ``../`` literally as part of a blob name, so it offers no
    protection of its own — the only traversal risk is our own join logic, and
    this function is that guard. It rejects empty keys, absolute keys
    (leading ``/``), any backslash, and any ``.`` / ``..`` / empty path segment.
    """
    raw = str(storage_key or "")
    if not raw or raw.startswith("/") or "\\" in raw:
        raise ValueError("Invalid storage key")
    for segment in raw.split("/"):
        if segment in {"", ".", ".."}:
            raise ValueError("Invalid storage key")
    return raw


def _is_not_found_error(exc: BaseException) -> bool:
    """True when an Azure SDK error means "the requested resource is gone".

    Checked structurally (class name / HTTP status) so the module never has to
    import ``azure.core`` just to name the exception type — which keeps the
    provider usable in tests and on local installs without the Azure SDKs.

    The provider probes its container before any blob operation, so by the time
    a 404 reaches a blob call it can only mean the blob itself is missing.
    """
    for cls in type(exc).__mro__:
        if cls.__name__ in {"ResourceNotFoundError", "FileNotFoundError"}:
            return True
    return getattr(exc, "status_code", None) == 404


def _build_container_client(account_url: str, container: str) -> _BuiltClient:
    """Build an async container client backed by ``DefaultAzureCredential``.

    Imported lazily: a deployment that does not use Azure storage should not pay
    to import (or install) these packages. ``DefaultAzureCredential`` reads
    ``AZURE_CLIENT_ID`` so a user-assigned identity is picked up when set; its
    remaining chain (environment/CLI/developer credentials) is also active.

    Returns the container, service client, and credential together so the
    provider can close all three at shutdown.
    """
    try:
        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient
    except ImportError as exc:  # pragma: no cover - only without the Azure SDKs
        raise RuntimeError(
            "The Azure file storage provider requires the 'azure-storage-blob' and "
            "'azure-identity' packages. Install them (e.g. pip install azure-storage-blob)."
        ) from exc

    canonical_url = _validate_account_url(account_url)
    credential = DefaultAzureCredential()
    service = BlobServiceClient(account_url=canonical_url, credential=credential)
    return _BuiltClient(service.get_container_client(container), service, credential)


class AzureBlobStorageProvider(FileStorageProvider):
    """Azure Blob Storage backend keyed by server-generated opaque storage keys."""

    name = "azure"

    def __init__(
        self,
        *,
        account_url: str | None = None,
        container: str | None = None,
        client: Any | None = None,
    ):
        raw_url = (
            account_url if account_url is not None else os.environ.get(ENV_ACCOUNT_URL, "")
        ).strip()
        self.container = (
            container if container is not None else os.environ.get(ENV_CONTAINER, "")
        ).strip()
        if not raw_url or not self.container:
            # Fail closed and loud rather than silently writing nowhere.
            raise ValueError(
                "Azure blob storage is not configured: set "
                f"{ENV_ACCOUNT_URL} and {ENV_CONTAINER} (or pass account_url and "
                "container) before selecting the 'azure' file storage provider."
            )
        self.account_url = _validate_account_url(raw_url)
        self._client = client
        self._service_client: Any | None = None
        self._credential: Any | None = None
        self._client_lock = threading.Lock()
        # Set once the container has been confirmed to exist and be reachable.
        # Concurrent first calls may probe twice; the probe is idempotent.
        self._container_ready = False

    async def _container_client(self) -> Any:
        client = self._client
        if client is None:
            with self._client_lock:
                if self._client is None:
                    built = _build_container_client(self.account_url, self.container)
                    self._client = built.container
                    self._service_client = built.service
                    self._credential = built.credential
                client = self._client
        if not self._container_ready:
            await self._probe_container(client)
        return client

    async def _probe_container(self, client: Any) -> None:
        """Confirm the configured container exists and is accessible.

        A failure here is a deployment problem, not a missing user file, so it
        is raised as :class:`AzureContainerUnavailableError` rather than being
        mapped to ``FileNotFoundError``. Once it succeeds, any later 404 from a
        blob operation unambiguously refers to that blob.
        """
        try:
            await client.get_container_properties()
        except Exception as exc:
            raise AzureContainerUnavailableError(
                f"Azure Blob container {self.container!r} at {self.account_url!r} "
                "does not exist or is not accessible to the configured identity. "
                "Check LUCENT_AZURE_STORAGE_ACCOUNT_URL, "
                "LUCENT_AZURE_STORAGE_CONTAINER, and the identity's role "
                "assignment; refusing to report this as missing user files."
            ) from exc
        self._container_ready = True

    async def _blob(self, storage_key: str) -> Any:
        # Validate the key before acquiring (and probing) the client so a bad
        # key never touches Azure.
        name = _blob_name(storage_key)
        return (await self._container_client()).get_blob_client(name)

    async def put(self, storage_key: str, content: bytes) -> None:
        # overwrite=True keeps put idempotent — a retried upload of the same key
        # (the service reuses the key only within one create/append attempt)
        # must not fail on an already-present blob.
        await (await self._blob(storage_key)).upload_blob(content, overwrite=True)

    async def get(self, storage_key: str) -> bytes:
        blob = await self._blob(storage_key)
        try:
            downloader = await blob.download_blob()
            return await downloader.readall()
        except Exception as exc:
            # The container was already probed, so a 404 here is a genuine
            # missing blob. Log it: the service turns FileNotFoundError into a
            # user-facing 404, and an operator needs a server-side signal that a
            # stored row points at bytes that are not there.
            if _is_not_found_error(exc):
                logger.error(
                    "Azure blob missing for storage key %r in container %r; "
                    "returning a 404 to the caller",
                    storage_key,
                    self.container,
                )
                raise FileNotFoundError(storage_key) from exc
            raise

    async def delete(self, storage_key: str) -> None:
        blob = await self._blob(storage_key)
        try:
            await blob.delete_blob()
        except Exception as exc:
            # Deletion is best-effort and idempotent, mirroring the local
            # provider's unlink(missing_ok=True): absent blob is a no-op.
            if _is_not_found_error(exc):
                return
            raise

    async def close(self) -> None:
        """Close the credential and both SDK clients.

        Safe to call more than once; after closing, the next operation rebuilds
        the client. Called from the API lifespan shutdown via
        :func:`lucent.storage.providers.close_file_storage_registry`.
        """
        container, service, credential = (
            self._client,
            self._service_client,
            self._credential,
        )
        self._client = None
        self._service_client = None
        self._credential = None
        self._container_ready = False
        for label, resource in (
            ("container", container),
            ("service", service),
            ("credential", credential),
        ):
            close = getattr(resource, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pragma: no cover - shutdown best effort
                logger.warning("Failed to close Azure blob %s client", label, exc_info=True)
