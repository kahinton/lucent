"""Tests for ``lucent.integrations.connection_flags``.

Covers each accessor's default and override behavior, the OAuth
configured probe, and the ``connection_feature_state`` aggregator.
"""

from __future__ import annotations

import pytest

from lucent.integrations import connection_flags as cf

# ---------------------------------------------------------------------------
# Per-flag accessors — defaults
# ---------------------------------------------------------------------------

ALL_FLAG_VARS = [
    "LUCENT_CONNECTIONS_PAT_ENABLED",
    "LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED",
    "LUCENT_CONNECTIONS_OAUTH_ENABLED",
    "LUCENT_WORKSPACE_INTEGRATIONS_ENABLED",
    "LUCENT_GITHUB_APP_ENABLED",
    "LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every connection flag env var so each test starts from defaults."""
    for var in ALL_FLAG_VARS:
        monkeypatch.delenv(var, raising=False)
    for provider in ("github", "slack", "jira"):
        monkeypatch.delenv(f"LUCENT_OAUTH_{provider.upper()}_CLIENT_ID", raising=False)


def test_pat_enabled_default_true() -> None:
    assert cf.pat_enabled() is True


def test_env_token_claim_enabled_default_true() -> None:
    assert cf.env_token_claim_enabled() is True


def test_oauth_enabled_default_true() -> None:
    assert cf.oauth_enabled() is True


def test_workspace_integrations_enabled_default_true() -> None:
    assert cf.workspace_integrations_enabled() is True


def test_github_app_enabled_default_false() -> None:
    assert cf.github_app_enabled() is False


def test_require_user_github_for_repo_acl_default_false() -> None:
    assert cf.require_user_github_for_repo_acl() is False


# ---------------------------------------------------------------------------
# _bool_env truthiness — accepted forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "On"])
def test_truthy_values_enable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LUCENT_GITHUB_APP_ENABLED", value)
    assert cf.github_app_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "garbage"])
def test_non_truthy_values_disable_flag(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LUCENT_CONNECTIONS_PAT_ENABLED", value)
    if value == "":
        # Empty string falls back to default (True) per design.
        assert cf.pat_enabled() is True
    else:
        assert cf.pat_enabled() is False


# ---------------------------------------------------------------------------
# OAuth-configured probe
# ---------------------------------------------------------------------------


def test_oauth_client_configured_false_when_unset() -> None:
    assert cf.oauth_client_configured("github") is False


def test_oauth_client_configured_true_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_OAUTH_GITHUB_CLIENT_ID", "abc")
    assert cf.oauth_client_configured("github") is True
    # Other providers stay False.
    assert cf.oauth_client_configured("slack") is False


def test_provider_oauth_configured_returns_all_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_OAUTH_SLACK_CLIENT_ID", "xyz")
    cfg = cf.provider_oauth_configured()
    assert cfg == {"github": False, "slack": True, "jira": False}


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def test_connection_feature_state_defaults_match_design() -> None:
    state = cf.connection_feature_state()
    d = state.to_dict()
    assert d == {
        "pat_enabled": True,
        "env_token_claim_enabled": True,
        "oauth_enabled": True,
        "workspace_integrations_enabled": True,
        "github_app_enabled": False,
        "require_user_github_for_repo_acl": False,
        "provider_oauth_configured": {
            "github": False,
            "slack": False,
            "jira": False,
        },
    }


def test_connection_feature_state_reflects_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCENT_CONNECTIONS_PAT_ENABLED", "false")
    monkeypatch.setenv("LUCENT_GITHUB_APP_ENABLED", "true")
    monkeypatch.setenv("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", "1")
    monkeypatch.setenv("LUCENT_OAUTH_GITHUB_CLIENT_ID", "client-abc")

    state = cf.connection_feature_state()
    assert state.pat_enabled is False
    assert state.github_app_enabled is True
    assert state.require_user_github_for_repo_acl is True
    assert state.provider_oauth_configured["github"] is True
    # Untouched flags keep defaults.
    assert state.oauth_enabled is True
    assert state.workspace_integrations_enabled is True


def test_connection_feature_state_is_frozen() -> None:
    state = cf.connection_feature_state()
    with pytest.raises((AttributeError, Exception)):
        state.pat_enabled = False  # type: ignore[misc]
