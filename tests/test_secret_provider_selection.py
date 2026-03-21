"""Tests for secret provider selection and secret:// env var resolution."""

from __future__ import annotations

import pytest

from lucent.auth import set_current_user
from lucent.secrets.base import SecretProvider, SecretScope
from lucent.secrets.registry import (
    SecretRegistry,
    get_selected_provider_name,
    initialize_secret_provider,
    validate_provider_env,
)
from lucent.secrets.utils import SECRET_REF_PREFIX, resolve_env_vars


class _FakeProvider(SecretProvider):
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get(self, key: str, scope: SecretScope) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, scope: SecretScope) -> None:
        self.values[key] = value

    async def delete(self, key: str, scope: SecretScope) -> bool:
        return self.values.pop(key, None) is not None

    async def list_keys(self, scope: SecretScope) -> list[str]:
        return sorted(self.values.keys())


class TestProviderSelection:
    def teardown_method(self):
        SecretRegistry.reset()

    def test_selected_provider_defaults_to_auto(self, monkeypatch):
        monkeypatch.delenv("LUCENT_SECRET_PROVIDER", raising=False)
        assert get_selected_provider_name() == "auto"

    def test_selected_provider_explicit_auto(self, monkeypatch):
        monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "auto")
        assert get_selected_provider_name() == "auto"

    def test_selected_provider_explicit_builtin(self, monkeypatch):
        monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "builtin")
        assert get_selected_provider_name() == "builtin"

    def test_selected_provider_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "bogus")
        with pytest.raises(ValueError, match="Invalid LUCENT_SECRET_PROVIDER"):
            get_selected_provider_name()

    @pytest.mark.asyncio
    async def test_initialize_vault_selected(self, monkeypatch):
        monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "vault")
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("VAULT_TOKEN", "test-token")
        provider = await initialize_secret_provider(object())
        assert SecretRegistry.get() is provider

    @pytest.mark.asyncio
    async def test_initialize_aws_selected(self, monkeypatch):
        monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "aws")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
        provider = await initialize_secret_provider(object())
        assert SecretRegistry.get() is provider

    @pytest.mark.asyncio
    async def test_initialize_azure_selected(self, monkeypatch):
        monkeypatch.setenv("LUCENT_SECRET_PROVIDER", "azure")
        monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://example.vault.azure.net")
        monkeypatch.setenv("AZURE_TENANT_ID", "tid")
        monkeypatch.setenv("AZURE_CLIENT_ID", "cid")
        monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
        provider = await initialize_secret_provider(object())
        assert SecretRegistry.get() is provider

    @pytest.mark.asyncio
    async def test_auto_detect_falls_back_to_builtin(self, monkeypatch):
        """When VAULT_ADDR is not set, auto-detect returns builtin."""
        monkeypatch.delenv("LUCENT_SECRET_PROVIDER", raising=False)
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.setenv("LUCENT_SECRET_KEY", "testkey-32bytes-pad0000000000aa")
        provider = await initialize_secret_provider(object())
        assert SecretRegistry.get() is provider

    def test_vault_addr_validation(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "not-a-url")
        monkeypatch.setenv("VAULT_TOKEN", "token")
        with pytest.raises(ValueError, match="valid http"):
            validate_provider_env("vault")

    def test_transit_addr_validation(self, monkeypatch):
        monkeypatch.setenv("VAULT_ADDR", "not-a-url")
        monkeypatch.setenv("VAULT_TOKEN", "token")
        with pytest.raises(ValueError, match="valid http"):
            validate_provider_env("transit")


class TestSecretEnvResolution:
    @pytest.mark.asyncio
    async def test_secret_reference_resolves(self):
        set_current_user({"id": "u1", "organization_id": "o1"})
        try:
            provider = _FakeProvider({"api-token": "resolved-value"})
            env = {"TOKEN": f"{SECRET_REF_PREFIX}api-token"}
            out = await resolve_env_vars(env, provider)
            assert out["TOKEN"] == "resolved-value"
        finally:
            set_current_user(None)

    @pytest.mark.asyncio
    async def test_missing_secret_reference_fails_clearly(self):
        set_current_user({"id": "u1", "organization_id": "o1"})
        try:
            provider = _FakeProvider({})
            env = {"TOKEN": f"{SECRET_REF_PREFIX}missing-token"}
            with pytest.raises(KeyError, match="Secret not found"):
                await resolve_env_vars(env, provider)
        finally:
            set_current_user(None)

    @pytest.mark.asyncio
    async def test_plaintext_env_vars_unchanged(self):
        set_current_user({"id": "u1", "organization_id": "o1"})
        try:
            provider = _FakeProvider({})
            env = {"PLAIN": "abc123", "URL": "https://example.com"}
            out = await resolve_env_vars(env, provider)
            assert out == env
        finally:
            set_current_user(None)
