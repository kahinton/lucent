"""OAuth2 helpers for enterprise tool credential onboarding and refresh."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx


class OAuthConfigError(RuntimeError):
    """Raised when provider OAuth config is missing."""


class OAuthExchangeError(RuntimeError):
    """Raised when token exchange/refresh fails."""


@dataclass(frozen=True)
class OAuthProviderConfig:
    name: str
    authorize_url: str
    token_url: str
    scope_separator: str = " "
    supports_pkce: bool = True
    default_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OAuthTokenResponse:
    access_token: str
    refresh_token: str | None
    token_type: str | None
    scope: str | None
    raw: dict[str, Any]
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None


PROVIDERS: dict[str, OAuthProviderConfig] = {
    "github": OAuthProviderConfig(
        name="github",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        default_scopes=("repo", "read:org"),
    ),
    "slack": OAuthProviderConfig(
        name="slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        default_scopes=("chat:write", "channels:read"),
    ),
    "jira": OAuthProviderConfig(
        name="jira",
        authorize_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",
        default_scopes=("read:jira-user", "read:jira-work"),
    ),
}


class OAuthService:
    """Provider-agnostic OAuth2 URL construction and token exchange."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    def get_provider(self, provider: str) -> OAuthProviderConfig:
        cfg = PROVIDERS.get(provider)
        if not cfg:
            raise OAuthConfigError(f"Unsupported OAuth provider '{provider}'")
        return cfg

    def get_client_config(self, provider: str) -> tuple[str, str]:
        upper = provider.upper()
        client_id = os.environ.get(f"LUCENT_OAUTH_{upper}_CLIENT_ID")
        client_secret = os.environ.get(f"LUCENT_OAUTH_{upper}_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise OAuthConfigError(
                f"OAuth client config missing for {provider}. "
                f"Set LUCENT_OAUTH_{upper}_CLIENT_ID and LUCENT_OAUTH_{upper}_CLIENT_SECRET"
            )
        return client_id, client_secret

    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        redirect_uri: str,
        scopes: list[str],
        pkce_challenge: str | None = None,
    ) -> str:
        cfg = self.get_provider(provider)
        client_id, _ = self.get_client_config(provider)
        scopes_effective = scopes or list(cfg.default_scopes)

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": cfg.scope_separator.join(scopes_effective),
        }
        if pkce_challenge:
            params["code_challenge"] = pkce_challenge
            params["code_challenge_method"] = "S256"
        return f"{cfg.authorize_url}?{urlencode(params)}"

    @staticmethod
    def generate_state() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_state(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    @staticmethod
    def generate_pkce_verifier_and_challenge() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    async def exchange_code(
        self,
        *,
        provider: str,
        code: str,
        redirect_uri: str,
        pkce_verifier: str | None = None,
    ) -> OAuthTokenResponse:
        cfg = self.get_provider(provider)
        client_id, client_secret = self.get_client_config(provider)

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        if pkce_verifier:
            payload["code_verifier"] = pkce_verifier

        data = await self._post_token(cfg.token_url, payload)
        return self._normalize_token_response(data)

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
    ) -> OAuthTokenResponse:
        cfg = self.get_provider(provider)
        client_id, client_secret = self.get_client_config(provider)

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }

        data = await self._post_token(cfg.token_url, payload)
        return self._normalize_token_response(data)

    async def _post_token(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(url, data=payload, headers=headers)

        if resp.status_code >= 400:
            raise OAuthExchangeError(f"OAuth token endpoint returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise OAuthExchangeError("OAuth token response was not JSON") from exc

        if not isinstance(data, dict):
            raise OAuthExchangeError("OAuth token response has invalid shape")

        if data.get("ok") is False:
            raise OAuthExchangeError(data.get("error", "OAuth token exchange failed"))

        if not data.get("access_token"):
            raise OAuthExchangeError("OAuth token response missing access_token")

        return data

    @staticmethod
    def _normalize_token_response(data: dict[str, Any]) -> OAuthTokenResponse:
        now = datetime.now(UTC)

        def _parse_exp(key: str) -> datetime | None:
            value = data.get(key)
            if value is None:
                return None
            try:
                seconds = int(value)
            except (TypeError, ValueError):
                return None
            return now + timedelta(seconds=max(seconds, 0))

        return OAuthTokenResponse(
            access_token=str(data["access_token"]),
            refresh_token=(str(data["refresh_token"]) if data.get("refresh_token") else None),
            token_type=(str(data["token_type"]) if data.get("token_type") else None),
            scope=(str(data["scope"]) if data.get("scope") else None),
            raw=data,
            access_token_expires_at=_parse_exp("expires_in"),
            refresh_token_expires_at=_parse_exp("refresh_token_expires_in"),
        )
