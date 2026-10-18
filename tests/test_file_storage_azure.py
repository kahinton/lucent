"""Unit tests for the Azure Blob file storage provider and registry resolution.

No database and no real Azure: the blob client is faked and injected (directly,
or by monkeypatching the lazy builder), so these run as plain unit tests.
"""

from __future__ import annotations

import pytest

from lucent.storage import azure_blob as ab
from lucent.storage.azure_blob import (
    AzureBlobStorageProvider,
    AzureContainerUnavailableError,
)
from lucent.storage.providers import (
    FileStorageProvider,
    FileStorageRegistry,
    LocalFileStorageProvider,
    build_file_storage_registry,
    close_file_storage_registry,
    reset_file_storage_registry,
)


class ResourceNotFoundError(Exception):
    """Mimics azure.core.exceptions.ResourceNotFoundError by name + status."""

    status_code = 404


class _FakeDownloader:
    def __init__(self, data: bytes):
        self._data = data

    async def readall(self) -> bytes:
        return self._data


class _FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str):
        self._store = store
        self._name = name

    async def upload_blob(self, data: bytes, overwrite: bool = False) -> None:
        self._store[self._name] = bytes(data)

    async def download_blob(self) -> _FakeDownloader:
        if self._name not in self._store:
            raise ResourceNotFoundError(self._name)
        return _FakeDownloader(self._store[self._name])

    async def delete_blob(self) -> None:
        if self._name not in self._store:
            raise ResourceNotFoundError(self._name)
        del self._store[self._name]


class _FakeContainer:
    def __init__(self, *, container_missing: bool = False):
        self.store: dict[str, bytes] = {}
        self.blob_names: list[str] = []
        self.container_missing = container_missing
        self.probe_count = 0
        self.closed = False

    async def get_container_properties(self) -> dict:
        self.probe_count += 1
        if self.container_missing:
            # azure.core raises the same ResourceNotFoundError for a missing
            # container as for a missing blob.
            raise ResourceNotFoundError("container")
        return {}

    async def close(self) -> None:
        self.closed = True

    def get_blob_client(self, name: str) -> _FakeBlob:
        self.blob_names.append(name)
        return _FakeBlob(self.store, name)


class _FakeAzureProvider(FileStorageProvider):
    """Minimal duck-typed provider named azure, for registry tests."""

    name = "azure"

    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def put(self, storage_key: str, content: bytes) -> None:
        self.store[storage_key] = content

    async def get(self, storage_key: str) -> bytes:
        if storage_key not in self.store:
            raise FileNotFoundError(storage_key)
        return self.store[storage_key]

    async def delete(self, storage_key: str) -> None:
        self.store.pop(storage_key, None)


# --------------------------------------------------------------------------- #
# Configuration / fail-closed
# --------------------------------------------------------------------------- #


def test_provider_requires_account_url_and_container(monkeypatch):
    monkeypatch.delenv(ab.ENV_ACCOUNT_URL, raising=False)
    monkeypatch.delenv(ab.ENV_CONTAINER, raising=False)

    with pytest.raises(ValueError, match="not configured"):
        AzureBlobStorageProvider()

    with pytest.raises(ValueError, match="not configured"):
        AzureBlobStorageProvider(account_url="https://x.blob.core.windows.net")


def test_provider_reads_account_and_container_from_env(monkeypatch):
    monkeypatch.setenv(ab.ENV_ACCOUNT_URL, "https://acct.blob.core.windows.net")
    monkeypatch.setenv(ab.ENV_CONTAINER, "lucent-files")

    provider = AzureBlobStorageProvider(client=_FakeContainer())

    assert provider.account_url == "https://acct.blob.core.windows.net"
    assert provider.container == "lucent-files"


@pytest.mark.parametrize(
    "account_url",
    [
        "http://acct.blob.core.windows.net",
        "ftp://acct.blob.core.windows.net",
        "https://evil.example.com",
        # Lookalike / suffix trick.
        "https://acct.blob.core.windows.net.evil.com",
        # Explicit port, embedded credentials, path, query, fragment.
        "https://acct.blob.core.windows.net:8443",
        "https://user:pass@acct.blob.core.windows.net",
        "https://acct.blob.core.windows.net/container",
        "https://acct.blob.core.windows.net?x=1",
        "https://acct.blob.core.windows.net#frag",
        # Sovereign-cloud / invalid-account-label hosts are outside the
        # supported endpoint.
        "https://acct.blob.core.chinacloudapi.cn",
        "https://acct_blob.blob.core.windows.net",
    ],
)
def test_account_url_rejects_unexpected_endpoints(account_url):
    with pytest.raises(ValueError, match=ab.ENV_ACCOUNT_URL):
        AzureBlobStorageProvider(account_url=account_url, container="c")


@pytest.mark.parametrize(
    "account_url",
    [
        "https://acct.blob.core.windows.net",
        "https://acct.blob.core.windows.net/",
        "HTTPS://ACCT.blob.core.windows.net",
    ],
)
def test_account_url_accepts_and_normalises_canonical_endpoint(account_url):
    provider = AzureBlobStorageProvider(
        account_url=account_url, container="c", client=_FakeContainer()
    )
    assert provider.account_url == "https://acct.blob.core.windows.net"


# --------------------------------------------------------------------------- #
# Key validation (the traversal guard)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "storage_key",
    [
        "",
        "/absolute/key",
        "../escape",
        "a/../../b",
        "a/./b",
        "a//b",
        "a/",
        ".",
        "..",
        "org\\user\\file",
        "a/b\\c",
    ],
)
def test_blob_name_rejects_bad_keys(storage_key):
    with pytest.raises(ValueError, match="Invalid storage key"):
        ab._blob_name(storage_key)


def test_blob_name_accepts_server_generated_keys():
    key = "11111111-1111-1111-1111-111111111111/2222/revisions/3333"
    assert ab._blob_name(key) == key


async def test_put_validates_key_before_touching_the_client(monkeypatch):
    container = _FakeContainer()
    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net",
        container="c",
        client=container,
    )

    with pytest.raises(ValueError, match="Invalid storage key"):
        await provider.put("../escape", b"nope")

    # The client was never asked for a blob for the rejected key.
    assert container.blob_names == []


# --------------------------------------------------------------------------- #
# Round-trip with a faked, monkeypatched client
# --------------------------------------------------------------------------- #


async def test_round_trip_get_put_delete_via_monkeypatched_builder(monkeypatch):
    container = _FakeContainer()
    monkeypatch.setenv(ab.ENV_ACCOUNT_URL, "https://acct.blob.core.windows.net")
    monkeypatch.setenv(ab.ENV_CONTAINER, "c")
    monkeypatch.setattr(
        ab,
        "_build_container_client",
        lambda account_url, name: ab._BuiltClient(container, None, None),
    )

    provider = AzureBlobStorageProvider()
    key = "org/user/file-1"

    await provider.put(key, b"hello")
    assert await provider.get(key) == b"hello"
    assert container.store == {key: b"hello"}

    # Idempotent overwrite.
    await provider.put(key, b"world")
    assert await provider.get(key) == b"world"

    await provider.delete(key)
    assert key not in container.store


async def test_get_missing_blob_raises_file_not_found():
    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net",
        container="c",
        client=_FakeContainer(),
    )

    with pytest.raises(FileNotFoundError):
        await provider.get("org/user/missing")


async def test_delete_absent_blob_is_a_no_op():
    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net",
        container="c",
        client=_FakeContainer(),
    )

    # Must not raise — mirrors unlink(missing_ok=True) on the local provider.
    await provider.delete("org/user/absent")


async def test_missing_container_fails_loudly_not_as_file_not_found():
    """A typo'd/missing container must never look like every file vanished."""
    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net",
        container="typo-container",
        client=_FakeContainer(container_missing=True),
    )

    with pytest.raises(
        AzureContainerUnavailableError,
        match=r"typo-container.*does not exist or is not accessible",
    ):
        await provider.get("org/user/file")

    # Writes fail the same way instead of 500ing from an unhandled put error.
    with pytest.raises(AzureContainerUnavailableError):
        await provider.put("org/user/file", b"content")

    # It is a server/config error, explicitly not the service's 404 sentinel.
    assert not issubclass(AzureContainerUnavailableError, FileNotFoundError)


async def test_missing_blob_still_raises_file_not_found_and_logs(caplog):
    import logging

    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net",
        container="c",
        client=_FakeContainer(),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(FileNotFoundError):
            await provider.get("org/user/missing")

    # The operator gets a signal before the service converts this to a 404.
    assert "Azure blob missing" in caplog.text
    assert "org/user/missing" in caplog.text


async def test_client_and_credential_are_built_once_and_reused(monkeypatch):
    container = _FakeContainer()
    builds: list[tuple[str, str]] = []

    def _builder(account_url: str, name: str):
        builds.append((account_url, name))
        return ab._BuiltClient(container, None, None)

    monkeypatch.setattr(ab, "_build_container_client", _builder)
    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net", container="c"
    )

    await provider.put("org/user/a", b"one")
    assert await provider.get("org/user/a") == b"one"
    await provider.put("org/user/b", b"two")
    await provider.get("org/user/b")

    assert builds == [("https://acct.blob.core.windows.net", "c")]
    # The container is probed once per provider, not once per operation.
    assert container.probe_count == 1


async def test_close_closes_container_service_and_credential(monkeypatch):
    class _Closable:
        def __init__(self):
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    container = _FakeContainer()
    service = _Closable()
    credential = _Closable()
    monkeypatch.setattr(
        ab,
        "_build_container_client",
        lambda account_url, name: ab._BuiltClient(container, service, credential),
    )
    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net", container="c"
    )

    await provider.put("org/user/a", b"one")
    await provider.close()

    assert container.closed is True
    assert service.closed is True
    assert credential.closed is True

    # Idempotent shutdown.
    await provider.close()


async def test_get_does_not_swallow_unrelated_errors():
    class _Boom:
        async def get_container_properties(self):
            return {}

        def get_blob_client(self, name):
            raise RuntimeError("transient failure")

    provider = AzureBlobStorageProvider(
        account_url="https://acct.blob.core.windows.net",
        container="c",
        client=_Boom(),
    )

    with pytest.raises(RuntimeError, match="transient failure"):
        await provider.get("org/user/file")


# --------------------------------------------------------------------------- #
# Registry: registration + active provider resolution
# --------------------------------------------------------------------------- #


def test_registry_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("LUCENT_FILE_STORAGE_PROVIDER", raising=False)
    registry = FileStorageRegistry([LocalFileStorageProvider(tmp_path)])

    assert registry.active_name == "local"
    assert registry.get("local") is registry.active()


def test_registry_can_activate_azure_while_still_resolving_local(tmp_path):
    local = LocalFileStorageProvider(tmp_path)
    azure = _FakeAzureProvider()
    registry = FileStorageRegistry([local, azure], active="azure")

    assert registry.active_name == "azure"
    assert registry.active() is azure
    # A row written as 'local' still resolves even though azure is active.
    assert registry.get("local") is local


def test_registry_fails_closed_when_active_provider_is_missing(tmp_path):
    with pytest.raises(ValueError, match="not available"):
        FileStorageRegistry([LocalFileStorageProvider(tmp_path)], active="azure")


def test_azure_blob_configured_reads_env(monkeypatch):
    monkeypatch.delenv(ab.ENV_ACCOUNT_URL, raising=False)
    monkeypatch.delenv(ab.ENV_CONTAINER, raising=False)
    from lucent.storage.providers import azure_blob_configured

    assert azure_blob_configured() is False

    monkeypatch.setenv(ab.ENV_ACCOUNT_URL, "https://acct.blob.core.windows.net")
    monkeypatch.setenv(ab.ENV_CONTAINER, "c")
    assert azure_blob_configured() is True


async def test_registry_close_closes_each_provider_once(tmp_path):
    class _ClosableProvider(_FakeAzureProvider):
        def __init__(self):
            super().__init__()
            self.close_count = 0

        async def close(self) -> None:
            self.close_count += 1

    local = LocalFileStorageProvider(tmp_path)
    azure = _ClosableProvider()
    registry = FileStorageRegistry([local, azure], active="local")

    await registry.close()

    assert azure.close_count == 1


async def test_process_registry_is_cached_and_dropped_on_shutdown():
    reset_file_storage_registry()
    try:
        first = build_file_storage_registry()
        assert build_file_storage_registry() is first

        await close_file_storage_registry()

        second = build_file_storage_registry()
        assert second is not first
    finally:
        reset_file_storage_registry()


async def test_shutdown_closes_the_cached_registrys_providers(monkeypatch):
    from lucent.storage import providers as providers_module

    closed: list[str] = []

    class _ClosableProvider(_FakeAzureProvider):
        async def close(self) -> None:
            closed.append(self.name)

    monkeypatch.setattr(
        providers_module, "_default_providers", lambda: [_ClosableProvider()]
    )
    monkeypatch.setenv("LUCENT_FILE_STORAGE_PROVIDER", "azure")
    reset_file_storage_registry()
    try:
        registry = build_file_storage_registry()
        await close_file_storage_registry()
        assert closed == ["azure"]
        assert build_file_storage_registry() is not registry
    finally:
        reset_file_storage_registry()
