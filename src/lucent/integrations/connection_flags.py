"""Centralized feature-flag accessors for the Connections subsystem.

This module is the **only** place that reads ``LUCENT_CONNECTIONS_*``,
``LUCENT_WORKSPACE_INTEGRATIONS_*``, ``LUCENT_GITHUB_APP_*``, and the
related ``LUCENT_OAUTH_<PROVIDER>_CLIENT_ID`` envs for connection-feature
gating. Call sites import accessors here instead of touching ``os.environ``
directly so behavior is uniform and trivial to test.

Defaults follow the Connections design (memory: two-tier connections):

    LUCENT_CONNECTIONS_PAT_ENABLED                     true   (local-friendly)
    LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED         true   (local-friendly)
    LUCENT_CONNECTIONS_OAUTH_ENABLED                   true
    LUCENT_WORKSPACE_INTEGRATIONS_ENABLED              true
    LUCENT_GITHUB_APP_ENABLED                          false
    LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL false

Enterprise-friendly profiles will flip ``PAT_ENABLED`` and
``ENV_TOKEN_CLAIM_ENABLED`` to ``false`` and ``GITHUB_APP_ENABLED`` /
``REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL`` to ``true``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

# Provider IDs we render OAuth buttons for. Keep aligned with
# ``lucent.integrations.oauth.PROVIDERS`` and the connections page.
_OAUTH_PROVIDERS: tuple[str, ...] = ("github", "slack", "jira")


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean env var. Accepts 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Individual flag accessors — one per env var
# ---------------------------------------------------------------------------


def pat_enabled() -> bool:
    """Whether users can save personal access tokens via the Connections page."""
    return _bool_env("LUCENT_CONNECTIONS_PAT_ENABLED", True)


def env_token_claim_enabled() -> bool:
    """Whether environment tokens (e.g. ``GITHUB_TOKEN``) can be claimed
    into a user credential. Local-friendly default; recommend disabling
    in shared/enterprise deployments.
    """
    return _bool_env("LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED", True)


def oauth_enabled() -> bool:
    """Whether OAuth account-connection UI/flows are surfaced.

    A provider button is only shown if BOTH this flag is on AND the
    provider has a configured ``LUCENT_OAUTH_<PROVIDER>_CLIENT_ID``.
    """
    return _bool_env("LUCENT_CONNECTIONS_OAUTH_ENABLED", True)


def workspace_integrations_enabled() -> bool:
    """Whether the workspace/system integrations section is exposed."""
    return _bool_env("LUCENT_WORKSPACE_INTEGRATIONS_ENABLED", True)


def github_app_enabled() -> bool:
    """Whether GitHub App install/webhook features are exposed.

    Off by default — flip on once App credentials and webhook routing
    are wired up for the deployment.
    """
    return _bool_env("LUCENT_GITHUB_APP_ENABLED", False)


def require_user_github_for_repo_acl() -> bool:
    """Strict-mode toggle for repository ACL.

    When ``False`` (default), ``GitHubRepoAccessService`` preserves
    backwards-compatible behavior of allowing access for users with no
    GitHub credential. When ``True``, missing-credential users are denied.
    """
    return _bool_env("LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL", False)


def oauth_client_configured(provider: str) -> bool:
    """Whether a ``LUCENT_OAUTH_<PROVIDER>_CLIENT_ID`` is set for ``provider``."""
    return bool(os.environ.get(f"LUCENT_OAUTH_{provider.upper()}_CLIENT_ID", ""))


def provider_oauth_configured(
    providers: Iterable[str] = _OAUTH_PROVIDERS,
) -> dict[str, bool]:
    """Return ``{provider: bool}`` for each known OAuth provider."""
    return {p: oauth_client_configured(p) for p in providers}


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionFeatureState:
    """Snapshot of all Connections feature flags + per-provider OAuth config.

    Constructed by :func:`connection_feature_state`. The page read model
    embeds this verbatim under ``feature_flags``.
    """

    pat_enabled: bool
    env_token_claim_enabled: bool
    oauth_enabled: bool
    workspace_integrations_enabled: bool
    github_app_enabled: bool
    require_user_github_for_repo_acl: bool
    provider_oauth_configured: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Plain-dict form suitable for templates / JSON serialization."""
        return {
            "pat_enabled": self.pat_enabled,
            "env_token_claim_enabled": self.env_token_claim_enabled,
            "oauth_enabled": self.oauth_enabled,
            "workspace_integrations_enabled": self.workspace_integrations_enabled,
            "github_app_enabled": self.github_app_enabled,
            "require_user_github_for_repo_acl": self.require_user_github_for_repo_acl,
            "provider_oauth_configured": dict(self.provider_oauth_configured),
        }


def connection_feature_state(
    providers: Iterable[str] = _OAUTH_PROVIDERS,
) -> ConnectionFeatureState:
    """Aggregate every connections flag into a single snapshot.

    No caching — each call re-reads the environment so unit tests using
    ``monkeypatch.setenv`` see updates immediately. The cost is negligible
    relative to the page render that consumes the result.
    """
    return ConnectionFeatureState(
        pat_enabled=pat_enabled(),
        env_token_claim_enabled=env_token_claim_enabled(),
        oauth_enabled=oauth_enabled(),
        workspace_integrations_enabled=workspace_integrations_enabled(),
        github_app_enabled=github_app_enabled(),
        require_user_github_for_repo_acl=require_user_github_for_repo_acl(),
        provider_oauth_configured=provider_oauth_configured(providers),
    )
