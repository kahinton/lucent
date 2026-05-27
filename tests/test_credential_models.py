"""Tests for enterprise credential models."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from lucent.integrations.credential_models import (
    CredentialCreate,
    CredentialIntegrationType,
    CredentialKind,
    CredentialScopeType,
    CredentialStatus,
    OAuthCallbackRequest,
    OAuthProvider,
    OAuthStartRequest,
)


class TestCredentialEnums:
    def test_enum_values_match_migration(self) -> None:
        assert {v.value for v in CredentialIntegrationType} == {"github", "slack", "jira", "custom"}
        assert {v.value for v in CredentialKind} == {"oauth2", "api_key", "service_account"}
        assert {v.value for v in CredentialScopeType} == {"user", "agent"}
        assert {v.value for v in CredentialStatus} == {"active", "revoked", "expired"}
        assert {v.value for v in OAuthProvider} == {"github", "slack", "jira"}


class TestCredentialCreate:
    def test_valid_user_scoped_create(self) -> None:
        model = CredentialCreate(
            integration_type=CredentialIntegrationType.GITHUB,
            display_name="GitHub Primary",
            scope_type=CredentialScopeType.USER,
        )
        assert model.credential_kind == CredentialKind.OAUTH2

    def test_valid_agent_scoped_create(self) -> None:
        model = CredentialCreate(
            integration_type=CredentialIntegrationType.SLACK,
            display_name="Slack Bot",
            scope_type=CredentialScopeType.AGENT,
            owner_agent_id=uuid4(),
        )
        assert model.scope_type == CredentialScopeType.AGENT

    def test_missing_display_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CredentialCreate(integration_type=CredentialIntegrationType.JIRA, display_name="")


class TestOAuthModels:
    def test_start_request_valid(self) -> None:
        req = OAuthStartRequest(
            provider=OAuthProvider.GITHUB,
            display_name="GitHub OAuth",
            redirect_uri="https://example.com/callback",
        )
        assert req.provider == OAuthProvider.GITHUB

    def test_callback_requires_code(self) -> None:
        with pytest.raises(ValidationError):
            OAuthCallbackRequest(
                provider=OAuthProvider.SLACK,
                code="",
                state="abc",
                redirect_uri="https://example.com/callback",
            )
