"""Tests for shipped MCP connectors and their per-user credential slots.

The property under test is the one that makes a *shared* connector usable by a
team: the endpoint is one definition for everybody, and the credential resolves
per caller. Nothing in the shipped definition may contain a token.
"""

from __future__ import annotations

import pytest

from lucent.services.builtin_connectors import (
    BUILTIN_CONNECTORS,
    builtin_connector_names,
    credential_slots_for,
    get_builtin_connector,
    list_builtin_connectors,
)


class TestShippedDefinitions:
    def test_names_are_unique_and_fit_the_column(self):
        names = [connector.name for connector in BUILTIN_CONNECTORS]
        assert len(names) == len(set(names))
        for name in names:
            assert len(name) <= 64, name

    def test_every_endpoint_is_absolute_https(self):
        for connector in BUILTIN_CONNECTORS:
            assert connector.url.startswith("https://"), connector.name
            if connector.readonly_url:
                assert connector.readonly_url.startswith("https://"), connector.name

    def test_github_ships_the_readonly_endpoint(self):
        """A built-in that can push to every repo the token reaches is a bad default."""
        github = get_builtin_connector("github")
        assert github.readonly_url.endswith("/readonly")
        assert github.effective_url == github.readonly_url

    def test_every_connector_declares_at_least_one_credential_slot(self):
        for connector in BUILTIN_CONNECTORS:
            assert connector.slots, connector.name

    def test_slots_have_a_stable_secret_key(self):
        """The key is documented to users, so it must not be per-organization."""
        for connector in BUILTIN_CONNECTORS:
            for slot in connector.slots:
                assert slot.secret_key, (connector.name, slot.name)
                assert "." in slot.secret_key, slot.secret_key
                assert slot.label.strip(), slot.secret_key


class TestNoTokensAreStored:
    def test_headers_reference_secrets_and_never_contain_values(self):
        for connector in BUILTIN_CONNECTORS:
            for header, value in connector.header_templates().items():
                assert value.startswith("secret://"), (connector.name, header, value)

    def test_env_vars_are_references_only(self):
        for connector in BUILTIN_CONNECTORS:
            for key, value in connector.env_vars().items():
                assert value.startswith("secret://"), (connector.name, key, value)

    def test_descriptor_never_leaks_a_value(self):
        for descriptor in list_builtin_connectors():
            for slot in descriptor["credential_slots"]:
                assert "secret_key" in slot
                assert not any(
                    field_name in slot
                    for field_name in ("value", "token", "secret_value")
                ), slot


class TestHeaderRendering:
    def test_bearer_format(self):
        connector = get_builtin_connector("github")
        slot = connector.slots[0]
        assert slot.header_value("ghp_abc") == "Bearer ghp_abc"

    def test_plain_format(self):
        from lucent.services.builtin_connectors import CredentialSlot

        slot = CredentialSlot(name="K", header="X-Api-Key", format="{value}")
        assert slot.header_value("abc") == "abc"


class TestLookup:
    def test_names_round_trip(self):
        for name in builtin_connector_names():
            assert get_builtin_connector(name).name == name

    def test_lookup_is_case_tolerant(self):
        assert get_builtin_connector("  GitHub ").name == "github"

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            get_builtin_connector("nope")

    def test_credential_slots_for_returns_descriptors(self):
        slots = credential_slots_for("github")
        assert slots and slots[0]["secret_key"] == "github.mcp.token"


class TestMissingCredentialBehaviour:
    """A missing per-user secret must degrade, not abort the turn.

    This was a real defect: config assembly raised a bare KeyError, so a user who
    had not yet added their token lost *every* tool in the turn rather than just
    that connector.
    """

    @pytest.mark.asyncio
    async def test_missing_secret_skips_only_that_server(self, monkeypatch):
        from lucent.api.routers import chat
        from lucent.secrets import SecretRegistry

        # The registry is populated by server startup; a unit test has no provider.
        monkeypatch.setattr(SecretRegistry, "get", classmethod(lambda cls: object()))

        async def _boom(value, provider, *, env_key=""):
            raise KeyError("Secret not found (reference 'github.mcp.token')")

        monkeypatch.setattr(
            "lucent.secrets.utils.resolve_secret_reference", _boom
        )
        servers = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "github",
                "server_type": "http",
                "url": "https://api.githubcopilot.com/mcp/readonly",
                "headers": {"Authorization": "secret://github.mcp.token"},
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "plain",
                "server_type": "http",
                "url": "https://mcp.example.com/mcp",
                "headers": {},
            },
        ]
        config, _discovery, missing = await chat._build_agent_mcp_config(servers)

        assert "mcp-11111111-1111-1111-1111-111111111111" not in config
        assert "mcp-22222222-2222-2222-2222-222222222222" in config
        assert "github" in missing
        assert "github.mcp.token" in " ".join(missing["github"])

    def test_notice_names_the_connector_and_the_fix(self):
        from lucent.api.routers.chat import _render_missing_credentials_notice

        notice = _render_missing_credentials_notice(
            {"github": ["Secret not found (reference 'github.mcp.token')"]}
        )
        assert "github" in notice
        assert "Settings" in notice
        # The model must be told not to over-claim a missing capability.
        assert "cannot access the service at all" in notice

    def test_empty_missing_map_renders_nothing(self):
        from lucent.api.routers.chat import _render_missing_credentials_notice

        assert _render_missing_credentials_notice({}) == ""
