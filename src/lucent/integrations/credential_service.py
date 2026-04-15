"""Credential management service with OAuth flows, refresh, and scope enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from lucent.api.deps import CurrentUser
from lucent.integrations.credential_models import (
    CredentialCreate,
    CredentialRefreshResponse,
    CredentialScopeType,
    OAuthCallbackRequest,
    OAuthStartRequest,
    OAuthStartResponse,
)
from lucent.integrations.credential_repository import CredentialRepository
from lucent.integrations.encryption import (
    BackendCredentialEncryptor,
    CredentialEncryptor,
    EncryptionError,
)
from lucent.integrations.oauth import OAuthService
from lucent.rbac import Role


@dataclass(frozen=True)
class ScopeOwnership:
    scope_type: str
    owner_user_id: UUID | None
    owner_agent_id: UUID | None


class CredentialService:
    """Application-layer logic for enterprise credential lifecycle."""

    def __init__(
        self,
        repo: CredentialRepository,
        *,
        oauth: OAuthService | None = None,
        encryptor: CredentialEncryptor | None = None,
    ) -> None:
        self.repo = repo
        self.oauth = oauth or OAuthService()
        self._encryptor = encryptor

    async def create_credential(
        self, payload: CredentialCreate, user: CurrentUser
    ) -> dict[str, Any]:
        org_id = self._require_org(user)
        ownership = self._resolve_ownership(
            scope_type=payload.scope_type.value,
            owner_user_id=payload.owner_user_id,
            owner_agent_id=payload.owner_agent_id,
            actor=user,
        )

        secret_payload = {
            "access_token": payload.access_token,
            "refresh_token": payload.refresh_token,
        }
        encrypted_secret_payload = self.encryptor.encrypt(secret_payload)
        encrypted_metadata = self.encryptor.encrypt(payload.metadata) if payload.metadata else None

        return await self.repo.create_credential(
            organization_id=str(org_id),
            integration_type=payload.integration_type.value,
            credential_kind=payload.credential_kind.value,
            scope_type=ownership.scope_type,
            owner_user_id=(str(ownership.owner_user_id) if ownership.owner_user_id else None),
            owner_agent_id=(str(ownership.owner_agent_id) if ownership.owner_agent_id else None),
            display_name=payload.display_name,
            scopes=payload.scopes,
            encrypted_secret_payload=encrypted_secret_payload,
            encrypted_metadata=encrypted_metadata,
            access_token_expires_at=payload.access_token_expires_at,
            refresh_token_expires_at=payload.refresh_token_expires_at,
            created_by=str(user.id),
        )

    async def list_credentials(
        self,
        *,
        user: CurrentUser,
        scope_type: str | None = None,
        owner_user_id: UUID | None = None,
        owner_agent_id: UUID | None = None,
        integration_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        org_id = self._require_org(user)

        if user.role < Role.ADMIN:
            scope_type = CredentialScopeType.USER.value
            owner_user_id = user.id
            owner_agent_id = None

        return await self.repo.list_credentials(
            str(org_id),
            scope_type=scope_type,
            owner_user_id=(str(owner_user_id) if owner_user_id else None),
            owner_agent_id=(str(owner_agent_id) if owner_agent_id else None),
            integration_type=integration_type,
            status=status,
            limit=limit,
        )

    async def get_credential(self, credential_id: UUID, user: CurrentUser) -> dict[str, Any]:
        org_id = self._require_org(user)
        row = await self.repo.get_credential(str(credential_id), str(org_id))
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
        self._authorize_row_access(row, user)
        return row

    async def update_credential(
        self,
        credential_id: UUID,
        user: CurrentUser,
        *,
        display_name: str | None = None,
        scopes: list[str] | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        access_token_expires_at: datetime | None = None,
        refresh_token_expires_at: datetime | None = None,
        metadata: dict[str, str] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        existing = await self.get_credential(credential_id, user)

        encrypted_secret_payload = None
        rotate = False
        if access_token is not None or refresh_token is not None:
            current_secret = self.encryptor.decrypt(existing["encrypted_secret_payload"])
            if access_token is not None:
                current_secret["access_token"] = access_token
            if refresh_token is not None:
                rotate = current_secret.get("refresh_token") != refresh_token
                current_secret["refresh_token"] = refresh_token
            encrypted_secret_payload = self.encryptor.encrypt(current_secret)

        encrypted_metadata = self.encryptor.encrypt(metadata) if metadata is not None else None

        updated = await self.repo.update_credential(
            str(credential_id),
            str(existing["organization_id"]),
            updated_by=str(user.id),
            display_name=display_name,
            scopes=scopes,
            encrypted_secret_payload=encrypted_secret_payload,
            encrypted_metadata=encrypted_metadata,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
            status=status,
            rotate_token_version=rotate,
        )
        if not updated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
        return updated

    async def delete_credential(self, credential_id: UUID, user: CurrentUser) -> dict[str, bool]:
        existing = await self.get_credential(credential_id, user)
        deleted = await self.repo.delete_credential(
            str(credential_id), str(existing["organization_id"])
        )
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
        return {"deleted": True}

    async def start_oauth(
        self, payload: OAuthStartRequest, user: CurrentUser
    ) -> OAuthStartResponse:
        org_id = self._require_org(user)
        ownership = self._resolve_ownership(
            scope_type=payload.scope_type.value,
            owner_user_id=payload.owner_user_id,
            owner_agent_id=payload.owner_agent_id,
            actor=user,
        )

        state = self.oauth.generate_state()
        state_hash = self.oauth.hash_state(state)

        pkce_verifier = None
        pkce_challenge = None
        provider_cfg = self.oauth.get_provider(payload.provider.value)
        if provider_cfg.supports_pkce:
            pkce_verifier, pkce_challenge = self.oauth.generate_pkce_verifier_and_challenge()

        expires_at = datetime.now(UTC) + timedelta(minutes=10)

        await self.repo.create_oauth_state(
            organization_id=str(org_id),
            provider=payload.provider.value,
            state_hash=state_hash,
            pkce_verifier=pkce_verifier,
            redirect_uri=payload.redirect_uri,
            requested_scopes=payload.scopes,
            scope_type=ownership.scope_type,
            owner_user_id=(str(ownership.owner_user_id) if ownership.owner_user_id else None),
            owner_agent_id=(str(ownership.owner_agent_id) if ownership.owner_agent_id else None),
            created_by=str(user.id),
            expires_at=expires_at,
        )

        auth_url = self.oauth.build_authorization_url(
            provider=payload.provider.value,
            state=state,
            redirect_uri=payload.redirect_uri,
            scopes=payload.scopes,
            pkce_challenge=pkce_challenge,
        )

        return OAuthStartResponse(
            provider=payload.provider,
            authorization_url=auth_url,
            state=state,
            expires_at=expires_at,
        )

    async def complete_oauth(
        self,
        payload: OAuthCallbackRequest,
        *,
        display_name: str,
        user: CurrentUser,
    ) -> dict[str, Any]:
        org_id = self._require_org(user)
        state_hash = self.oauth.hash_state(payload.state)
        state_row = await self.repo.consume_oauth_state(
            organization_id=str(org_id),
            provider=payload.provider.value,
            state_hash=state_hash,
        )
        if not state_row:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired OAuth state")

        ownership = self._resolve_ownership(
            scope_type=state_row["scope_type"],
            owner_user_id=state_row.get("owner_user_id"),
            owner_agent_id=state_row.get("owner_agent_id"),
            actor=user,
        )

        token = await self.oauth.exchange_code(
            provider=payload.provider.value,
            code=payload.code,
            redirect_uri=payload.redirect_uri,
            pkce_verifier=state_row.get("pkce_verifier"),
        )

        encrypted_secret_payload = self.encryptor.encrypt(
            {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "token_type": token.token_type,
            }
        )
        encrypted_metadata = self.encryptor.encrypt({"oauth_raw": token.raw})

        return await self.repo.create_credential(
            organization_id=str(org_id),
            integration_type=payload.provider.value,
            credential_kind="oauth2",
            scope_type=ownership.scope_type,
            owner_user_id=(str(ownership.owner_user_id) if ownership.owner_user_id else None),
            owner_agent_id=(str(ownership.owner_agent_id) if ownership.owner_agent_id else None),
            display_name=display_name,
            scopes=state_row.get("requested_scopes") or [],
            encrypted_secret_payload=encrypted_secret_payload,
            encrypted_metadata=encrypted_metadata,
            access_token_expires_at=token.access_token_expires_at,
            refresh_token_expires_at=token.refresh_token_expires_at,
            created_by=str(user.id),
        )

    async def refresh_credential(
        self,
        credential_id: UUID,
        user: CurrentUser,
    ) -> CredentialRefreshResponse:
        row = await self.get_credential(credential_id, user)
        if row["credential_kind"] != "oauth2":
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Only OAuth2 credentials can be refreshed"
            )

        secret_payload = self.encryptor.decrypt(row["encrypted_secret_payload"])
        refresh_token = secret_payload.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status.HTTP_409_CONFLICT, "Credential has no refresh token")

        refreshed = await self.oauth.refresh_access_token(
            provider=row["integration_type"],
            refresh_token=refresh_token,
        )

        rotated_refresh = bool(refreshed.refresh_token and refreshed.refresh_token != refresh_token)
        new_payload = {
            "access_token": refreshed.access_token,
            "refresh_token": refreshed.refresh_token or refresh_token,
            "token_type": refreshed.token_type,
        }

        updated = await self.repo.mark_refreshed(
            str(credential_id),
            str(row["organization_id"]),
            updated_by=str(user.id),
            encrypted_secret_payload=self.encryptor.encrypt(new_payload),
            access_token_expires_at=refreshed.access_token_expires_at,
            refresh_token_expires_at=refreshed.refresh_token_expires_at,
            rotated_refresh_token=rotated_refresh,
        )
        if not updated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")

        return CredentialRefreshResponse(
            id=updated["id"],
            refreshed=True,
            rotated_refresh_token=rotated_refresh,
            access_token_expires_at=updated.get("access_token_expires_at"),
            refresh_token_version=updated["refresh_token_version"],
        )

    def _require_org(self, user: CurrentUser) -> UUID:
        if not user.organization_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")
        return user.organization_id

    @property
    def encryptor(self) -> CredentialEncryptor:
        if self._encryptor is None:
            try:
                self._encryptor = BackendCredentialEncryptor()
            except EncryptionError as exc:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "Credential encryption not configured on this server",
                ) from exc
        return self._encryptor

    def _resolve_ownership(
        self,
        *,
        scope_type: str,
        owner_user_id: UUID | str | None,
        owner_agent_id: UUID | str | None,
        actor: CurrentUser,
    ) -> ScopeOwnership:
        is_admin = actor.role >= Role.ADMIN

        if scope_type == CredentialScopeType.USER.value:
            target_user_id = UUID(str(owner_user_id)) if owner_user_id else actor.id
            if not is_admin and target_user_id != actor.id:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "Cannot manage credentials for other users"
                )
            return ScopeOwnership(
                scope_type=scope_type, owner_user_id=target_user_id, owner_agent_id=None
            )

        if scope_type == CredentialScopeType.AGENT.value:
            if not is_admin:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "Agent-scoped credentials require admin role"
                )
            if not owner_agent_id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "owner_agent_id is required"
                )
            return ScopeOwnership(
                scope_type=scope_type,
                owner_user_id=None,
                owner_agent_id=UUID(str(owner_agent_id)),
            )

        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid scope_type")

    def _authorize_row_access(self, row: dict[str, Any], user: CurrentUser) -> None:
        if user.role >= Role.ADMIN:
            return
        if row.get("scope_type") != CredentialScopeType.USER.value:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
        owner_user_id = row.get("owner_user_id")
        if owner_user_id is None or UUID(str(owner_user_id)) != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
