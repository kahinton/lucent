"""Tests for the Transit secret provider."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from lucent.secrets.base import SecretScope
from lucent.secrets.transit import TransitSecretProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VAULT_ADDR = "http://openbao:8200"
VAULT_TOKEN = "test-token"
ORG_ID = str(uuid4())
USER_ID = str(uuid4())
GROUP_ID = str(uuid4())


def _user_scope() -> SecretScope:
    return SecretScope(organization_id=ORG_ID, owner_user_id=USER_ID)


def _group_scope() -> SecretScope:
    return SecretScope(organization_id=ORG_ID, owner_group_id=GROUP_ID)


def _make_pool() -> MagicMock:
    """Return a mock asyncpg pool."""
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    # transaction() is a regular method returning an async context manager
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return pool, conn


def _make_provider(pool=None) -> tuple[TransitSecretProvider, MagicMock]:
    if pool is None:
        pool, conn = _make_pool()
    else:
        conn = None
    provider = TransitSecretProvider(
        pool=pool,
        vault_addr=VAULT_ADDR,
        vault_token=VAULT_TOKEN,
    )
    return provider, conn


def _encrypt_response(plaintext: str) -> str:
    """Produce a fake transit ciphertext."""
    return f"vault:v1:{base64.b64encode(plaintext.encode()).decode()}"


def _decrypt_response_body(plaintext: str) -> dict:
    """Produce the JSON body for a decrypt response."""
    return {
        "data": {
            "plaintext": base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        }
    }


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_creates_client_with_headers(self):
        pool, _ = _make_pool()
        provider = TransitSecretProvider(
            pool=pool,
            vault_addr="http://localhost:8200/",
            vault_token="s.mytoken",
        )
        assert provider._client.headers["x-vault-token"] == "s.mytoken"
        assert str(provider._client.base_url) == "http://localhost:8200"

    def test_default_key_and_mount(self):
        pool, _ = _make_pool()
        provider = TransitSecretProvider(
            pool=pool, vault_addr=VAULT_ADDR, vault_token=VAULT_TOKEN
        )
        assert provider._transit_key == "lucent-secrets"
        assert provider._transit_mount == "transit"

    def test_custom_key_and_mount(self):
        pool, _ = _make_pool()
        provider = TransitSecretProvider(
            pool=pool,
            vault_addr=VAULT_ADDR,
            vault_token=VAULT_TOKEN,
            transit_key="my-key",
            transit_mount="my-transit",
        )
        assert provider._transit_key == "my-key"
        assert provider._transit_mount == "my-transit"


# ---------------------------------------------------------------------------
# Encrypt / Decrypt helpers
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    @pytest.mark.asyncio
    async def test_encrypt_sends_base64_plaintext(self):
        provider, _ = _make_provider()
        ciphertext = "vault:v1:abc123"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"ciphertext": ciphertext}}

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            result = await provider._encrypt("hello world")

        assert result == ciphertext
        call_args = mock_post.call_args
        assert call_args[0][0] == "/v1/transit/encrypt/lucent-secrets"
        body = call_args[1]["json"]
        assert base64.b64decode(body["plaintext"]).decode() == "hello world"

    @pytest.mark.asyncio
    async def test_encrypt_error_status(self):
        provider, _ = _make_provider()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            with pytest.raises(RuntimeError, match="Transit encrypt returned 500"):
                await provider._encrypt("secret")

    @pytest.mark.asyncio
    async def test_encrypt_connection_error(self):
        provider, _ = _make_provider()

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("connection refused")
            with pytest.raises(RuntimeError, match="Transit encrypt request failed"):
                await provider._encrypt("secret")

    @pytest.mark.asyncio
    async def test_decrypt_returns_plaintext(self):
        provider, _ = _make_provider()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _decrypt_response_body("hello world")

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            result = await provider._decrypt("vault:v1:abc123")

        assert result == "hello world"
        call_args = mock_post.call_args
        assert call_args[0][0] == "/v1/transit/decrypt/lucent-secrets"
        assert call_args[1]["json"] == {"ciphertext": "vault:v1:abc123"}

    @pytest.mark.asyncio
    async def test_decrypt_error_status(self):
        provider, _ = _make_provider()
        mock_resp = MagicMock()
        mock_resp.status_code = 400

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            with pytest.raises(RuntimeError, match="Transit decrypt returned 400"):
                await provider._decrypt("vault:v1:bad")

    @pytest.mark.asyncio
    async def test_decrypt_connection_error(self):
        provider, _ = _make_provider()

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("connection refused")
            with pytest.raises(RuntimeError, match="Transit decrypt request failed"):
                await provider._decrypt("vault:v1:abc")

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_roundtrip(self):
        """Verify the base64 encoding/decoding is symmetric."""
        provider, _ = _make_provider()
        plaintext = "my-secret-value-🔑"
        b64 = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
        ciphertext = "vault:v1:xyz"

        encrypt_resp = MagicMock()
        encrypt_resp.status_code = 200
        encrypt_resp.json.return_value = {"data": {"ciphertext": ciphertext}}

        decrypt_resp = MagicMock()
        decrypt_resp.status_code = 200
        decrypt_resp.json.return_value = {"data": {"plaintext": b64}}

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = encrypt_resp
            ct = await provider._encrypt(plaintext)
            assert ct == ciphertext

            mock_post.return_value = decrypt_resp
            pt = await provider._decrypt(ct)
            assert pt == plaintext


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_get_existing_secret(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        ciphertext = "vault:v1:encrypted"

        # DB returns a row with ciphertext stored as bytes
        row = {"encrypted_value": ciphertext.encode("utf-8")}
        conn.fetchrow = AsyncMock(return_value=row)

        decrypt_resp = MagicMock()
        decrypt_resp.status_code = 200
        decrypt_resp.json.return_value = _decrypt_response_body("secret-value")

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = decrypt_resp
            result = await provider.get("api-key", _user_scope())

        assert result == "secret-value"
        conn.fetchrow.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.fetchrow = AsyncMock(return_value=None)

        result = await provider.get("missing", _user_scope())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_with_group_scope(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        ciphertext = "vault:v1:groupenc"
        row = {"encrypted_value": ciphertext.encode("utf-8")}
        conn.fetchrow = AsyncMock(return_value=row)

        decrypt_resp = MagicMock()
        decrypt_resp.status_code = 200
        decrypt_resp.json.return_value = _decrypt_response_body("group-secret")

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = decrypt_resp
            result = await provider.get("shared-key", _group_scope())

        assert result == "group-secret"


# ---------------------------------------------------------------------------
# SET
# ---------------------------------------------------------------------------


class TestSet:
    @pytest.mark.asyncio
    async def test_set_inserts_new_secret(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.execute = AsyncMock(return_value="UPDATE 0")

        encrypt_resp = MagicMock()
        encrypt_resp.status_code = 200
        encrypt_resp.json.return_value = {"data": {"ciphertext": "vault:v1:new"}}

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = encrypt_resp
            await provider.set("api-key", "my-secret", _user_scope())

        # First call is UPDATE (returns UPDATE 0), second is INSERT
        assert conn.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_set_updates_existing_secret(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.execute = AsyncMock(return_value="UPDATE 1")

        encrypt_resp = MagicMock()
        encrypt_resp.status_code = 200
        encrypt_resp.json.return_value = {"data": {"ciphertext": "vault:v1:updated"}}

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = encrypt_resp
            await provider.set("api-key", "new-value", _user_scope())

        # Only UPDATE, no INSERT
        assert conn.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_set_stores_ciphertext_as_bytes(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.execute = AsyncMock(return_value="UPDATE 0")

        encrypt_resp = MagicMock()
        encrypt_resp.status_code = 200
        encrypt_resp.json.return_value = {"data": {"ciphertext": "vault:v1:ct"}}

        with patch.object(provider._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = encrypt_resp
            await provider.set("key", "value", _user_scope())

        # Check the INSERT call contains bytes
        insert_call = conn.execute.call_args_list[1]
        encrypted_arg = insert_call[0][2]  # second positional arg is encrypted_value
        assert isinstance(encrypted_arg, bytes)
        assert encrypted_arg == b"vault:v1:ct"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_existing(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.execute = AsyncMock(return_value="DELETE 1")

        result = await provider.delete("api-key", _user_scope())
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.execute = AsyncMock(return_value="DELETE 0")

        result = await provider.delete("missing", _user_scope())
        assert result is False


# ---------------------------------------------------------------------------
# LIST KEYS
# ---------------------------------------------------------------------------


class TestListKeys:
    @pytest.mark.asyncio
    async def test_list_keys_returns_sorted(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.fetch = AsyncMock(
            return_value=[{"key": "alpha"}, {"key": "beta"}, {"key": "gamma"}]
        )

        result = await provider.list_keys(_user_scope())
        assert result == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_list_keys_empty(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.fetch = AsyncMock(return_value=[])

        result = await provider.list_keys(_user_scope())
        assert result == []

    @pytest.mark.asyncio
    async def test_list_keys_group_scope(self):
        pool, conn = _make_pool()
        provider, _ = _make_provider(pool)
        conn.fetch = AsyncMock(return_value=[{"key": "shared"}])

        result = await provider.list_keys(_group_scope())
        assert result == ["shared"]


# ---------------------------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self):
        provider, _ = _make_provider()
        health_resp = MagicMock()
        health_resp.status_code = 200
        key_resp = MagicMock()
        key_resp.status_code = 200

        with patch.object(provider._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [health_resp, key_resp]
            assert await provider.health_check() is True

        calls = mock_get.call_args_list
        assert calls[0][0][0] == "/v1/sys/health"
        assert calls[1][0][0] == "/v1/transit/keys/lucent-secrets"

    @pytest.mark.asyncio
    async def test_unhealthy_system(self):
        provider, _ = _make_provider()
        resp = MagicMock()
        resp.status_code = 503

        with patch.object(provider._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = resp
            assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_key_not_found(self):
        provider, _ = _make_provider()
        health_resp = MagicMock()
        health_resp.status_code = 200
        key_resp = MagicMock()
        key_resp.status_code = 404

        with patch.object(provider._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [health_resp, key_resp]
            assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_unreachable(self):
        provider, _ = _make_provider()

        with patch.object(provider._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.ConnectError("unreachable")
            assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_custom_mount_and_key_in_health_check(self):
        pool, _ = _make_pool()
        provider = TransitSecretProvider(
            pool=pool,
            vault_addr=VAULT_ADDR,
            vault_token=VAULT_TOKEN,
            transit_key="custom-key",
            transit_mount="custom-transit",
        )
        health_resp = MagicMock()
        health_resp.status_code = 200
        key_resp = MagicMock()
        key_resp.status_code = 200

        with patch.object(provider._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [health_resp, key_resp]
            assert await provider.health_check() is True

        calls = mock_get.call_args_list
        assert calls[1][0][0] == "/v1/custom-transit/keys/custom-key"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    def test_transit_in_supported_providers(self):
        from lucent.secrets.registry import _SUPPORTED_PROVIDERS

        assert "transit" in _SUPPORTED_PROVIDERS

    def test_validate_provider_env_missing_vars(self):
        from lucent.secrets.registry import validate_provider_env

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="LUCENT_SECRET_PROVIDER=transit requires"):
                validate_provider_env("transit")

    def test_validate_provider_env_ok(self):
        from lucent.secrets.registry import validate_provider_env

        with patch.dict(
            "os.environ",
            {"VAULT_ADDR": "http://localhost:8200", "VAULT_TOKEN": "tok"},
        ):
            validate_provider_env("transit")  # Should not raise

    @pytest.mark.asyncio
    async def test_initialize_creates_transit_provider(self):
        from lucent.secrets.registry import SecretRegistry, initialize_secret_provider

        SecretRegistry.reset()
        pool = MagicMock()
        with patch.dict(
            "os.environ",
            {
                "LUCENT_SECRET_PROVIDER": "transit",
                "VAULT_ADDR": "http://openbao:8200",
                "VAULT_TOKEN": "root",
            },
        ):
            provider = await initialize_secret_provider(pool)

        assert isinstance(provider, TransitSecretProvider)
        assert SecretRegistry.get("transit") is provider
        SecretRegistry.reset()
