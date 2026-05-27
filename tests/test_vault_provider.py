"""Unit tests for VaultSecretProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from lucent.secrets.base import SecretScope
from lucent.secrets.vault import VaultSecretProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

USER_SCOPE = SecretScope(
    organization_id="org-123",
    owner_user_id="user-456",
)

GROUP_SCOPE = SecretScope(
    organization_id="org-123",
    owner_group_id="group-789",
)

VAULT_ENV = {
    "VAULT_ADDR": "http://localhost:8200",
    "VAULT_TOKEN": "test-token",
}


@pytest.fixture()
def env(monkeypatch):
    """Set required Vault env vars."""
    for k, v in VAULT_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def provider(env) -> VaultSecretProvider:
    return VaultSecretProvider()


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_missing_addr_raises(self, monkeypatch):
        monkeypatch.setenv("VAULT_TOKEN", "tok")
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        with pytest.raises(ValueError, match="VAULT_ADDR"):
            VaultSecretProvider()

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "http://v:8200")
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="VAULT_TOKEN"):
            VaultSecretProvider()

    def test_default_mount(self, env):
        p = VaultSecretProvider()
        assert p._mount == "secret"

    def test_custom_mount(self, monkeypatch, env):
        monkeypatch.setenv("VAULT_KV_MOUNT", "kv")
        p = VaultSecretProvider()
        assert p._mount == "kv"

    def test_client_headers(self, env):
        p = VaultSecretProvider()
        assert p._client.headers["x-vault-token"] == "test-token"


# ---------------------------------------------------------------------------
# Path mapping tests
# ---------------------------------------------------------------------------


class TestPathMapping:
    def test_user_scope_path(self, provider):
        path = provider._build_path(USER_SCOPE)
        assert path == "lucent/org-123/user/user-456"

    def test_group_scope_path(self, provider):
        path = provider._build_path(GROUP_SCOPE)
        assert path == "lucent/org-123/group/group-789"

    def test_no_owner_raises(self, provider):
        scope = SecretScope(organization_id="org-1")
        with pytest.raises(ValueError, match="owner_user_id or owner_group_id"):
            provider._build_path(scope)


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_get_success(self, provider):
        mock_resp = httpx.Response(
            200,
            json={"data": {"data": {"value": "s3cret"}, "metadata": {}}},
            request=httpx.Request("GET", "http://test"),
        )
        provider._client.get = AsyncMock(return_value=mock_resp)

        result = await provider.get("api-key", USER_SCOPE)
        assert result == "s3cret"
        provider._client.get.assert_called_once_with(
            "/v1/secret/data/lucent/org-123/user/user-456/api-key"
        )

    @pytest.mark.asyncio
    async def test_get_not_found(self, provider):
        mock_resp = httpx.Response(
            404,
            json={"errors": []},
            request=httpx.Request("GET", "http://test"),
        )
        provider._client.get = AsyncMock(return_value=mock_resp)

        result = await provider.get("missing", USER_SCOPE)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_403_raises(self, provider):
        mock_resp = httpx.Response(
            403,
            json={"errors": ["permission denied"]},
            request=httpx.Request("GET", "http://test"),
        )
        provider._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="403"):
            await provider.get("key", USER_SCOPE)

    @pytest.mark.asyncio
    async def test_get_500_raises(self, provider):
        mock_resp = httpx.Response(
            500,
            json={"errors": ["internal"]},
            request=httpx.Request("GET", "http://test"),
        )
        provider._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="500"):
            await provider.get("key", USER_SCOPE)

    @pytest.mark.asyncio
    async def test_get_connection_error(self, provider):
        provider._client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(RuntimeError, match="connection error"):
            await provider.get("key", USER_SCOPE)


# ---------------------------------------------------------------------------
# SET tests
# ---------------------------------------------------------------------------


class TestSet:
    @pytest.mark.asyncio
    async def test_set_success(self, provider):
        mock_resp = httpx.Response(
            200,
            json={"data": {"version": 1}},
            request=httpx.Request("POST", "http://test"),
        )
        provider._client.post = AsyncMock(return_value=mock_resp)

        await provider.set("api-key", "val123", GROUP_SCOPE)
        provider._client.post.assert_called_once_with(
            "/v1/secret/data/lucent/org-123/group/group-789/api-key",
            json={"data": {"value": "val123"}},
        )

    @pytest.mark.asyncio
    async def test_set_500_raises(self, provider):
        mock_resp = httpx.Response(
            500,
            json={"errors": ["internal"]},
            request=httpx.Request("POST", "http://test"),
        )
        provider._client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="500"):
            await provider.set("key", "val", USER_SCOPE)

    @pytest.mark.asyncio
    async def test_set_connection_error(self, provider):
        provider._client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(RuntimeError, match="connection error"):
            await provider.set("key", "val", USER_SCOPE)


# ---------------------------------------------------------------------------
# DELETE tests
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self, provider):
        mock_resp = httpx.Response(
            204,
            request=httpx.Request("DELETE", "http://test"),
        )
        provider._client.delete = AsyncMock(return_value=mock_resp)

        result = await provider.delete("api-key", USER_SCOPE)
        assert result is True
        provider._client.delete.assert_called_once_with(
            "/v1/secret/metadata/lucent/org-123/user/user-456/api-key"
        )

    @pytest.mark.asyncio
    async def test_delete_not_found(self, provider):
        mock_resp = httpx.Response(
            404,
            json={"errors": []},
            request=httpx.Request("DELETE", "http://test"),
        )
        provider._client.delete = AsyncMock(return_value=mock_resp)

        result = await provider.delete("missing", USER_SCOPE)
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_500_raises(self, provider):
        mock_resp = httpx.Response(
            500,
            json={"errors": ["internal"]},
            request=httpx.Request("DELETE", "http://test"),
        )
        provider._client.delete = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="500"):
            await provider.delete("key", USER_SCOPE)

    @pytest.mark.asyncio
    async def test_delete_connection_error(self, provider):
        provider._client.delete = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(RuntimeError, match="connection error"):
            await provider.delete("key", USER_SCOPE)


# ---------------------------------------------------------------------------
# LIST tests
# ---------------------------------------------------------------------------


class TestListKeys:
    @pytest.mark.asyncio
    async def test_list_success(self, provider):
        mock_resp = httpx.Response(
            200,
            json={"data": {"keys": ["api-key", "db-pass"]}},
            request=httpx.Request("LIST", "http://test"),
        )
        provider._client.request = AsyncMock(return_value=mock_resp)

        result = await provider.list_keys(USER_SCOPE)
        assert result == ["api-key", "db-pass"]
        provider._client.request.assert_called_once_with(
            "LIST",
            "/v1/secret/metadata/lucent/org-123/user/user-456/",
        )

    @pytest.mark.asyncio
    async def test_list_empty(self, provider):
        mock_resp = httpx.Response(
            404,
            json={"errors": []},
            request=httpx.Request("LIST", "http://test"),
        )
        provider._client.request = AsyncMock(return_value=mock_resp)

        result = await provider.list_keys(GROUP_SCOPE)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_500_raises(self, provider):
        mock_resp = httpx.Response(
            500,
            json={"errors": ["internal"]},
            request=httpx.Request("LIST", "http://test"),
        )
        provider._client.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="500"):
            await provider.list_keys(USER_SCOPE)

    @pytest.mark.asyncio
    async def test_list_connection_error(self, provider):
        provider._client.request = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(RuntimeError, match="connection error"):
            await provider.list_keys(USER_SCOPE)


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, provider):
        mock_resp = httpx.Response(
            200,
            json={"initialized": True, "sealed": False},
            request=httpx.Request("GET", "http://test"),
        )
        provider._client.get = AsyncMock(return_value=mock_resp)

        assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_sealed(self, provider):
        mock_resp = httpx.Response(
            503,
            json={"initialized": True, "sealed": True},
            request=httpx.Request("GET", "http://test"),
        )
        provider._client.get = AsyncMock(return_value=mock_resp)

        assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_unreachable(self, provider):
        provider._client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        assert await provider.health_check() is False


# ---------------------------------------------------------------------------
# Error message safety — values must never appear in errors
# ---------------------------------------------------------------------------


class TestErrorSafety:
    @pytest.mark.asyncio
    async def test_set_error_does_not_leak_value(self, provider):
        """Ensure secret values don't appear in error messages."""
        secret_value = "super-secret-password-12345"
        provider._client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(RuntimeError) as exc_info:
            await provider.set("key", secret_value, USER_SCOPE)
        assert secret_value not in str(exc_info.value)
