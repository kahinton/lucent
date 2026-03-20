"""FastAPI router for integrations — webhooks, admin CRUD, pairing, and link management.

Webhook endpoint (``POST /webhook/{provider}``) acks immediately and
enqueues the event for async processing by ``IntegrationService``.

Admin endpoints (``/api/v1/integrations/*``) are RBAC-gated behind
``MANAGE_INTEGRATIONS`` (admin + owner roles only).

Pairing endpoints (``link``, ``verify``) allow any authenticated user
to manage their own identity links.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from lucent.api.deps import AdminUser, AuthenticatedUser
from lucent.db.audit import (
    CHALLENGE_ISSUED,
    CHALLENGE_SUCCEEDED,
    INTEGRATION_CREATED,
    INTEGRATION_DISABLED,
    INTEGRATION_UPDATED,
    LINK_ACTIVATED,
    LINK_REVOKED,
    AuditRepository,
)
from lucent.db.pool import get_pool
from lucent.integrations.encryption import EncryptionError, FernetEncryptor
from lucent.integrations.models import (
    IntegrationCreate,
    IntegrationListResponse,
    IntegrationResponse,
    IntegrationStatus,
    IntegrationUpdate,
    PairingChallengeCreate,
    PairingChallengeResponse,
    UserLinkCreate,
    UserLinkListResponse,
    UserLinkResponse,
    VerificationMethod,
)
from lucent.integrations.repositories import (
    IntegrationRepo,
    PairingChallengeRepo,
    UserLinkRepo,
)
from lucent.integrations.service import IntegrationService
from lucent.secrets import SecretRegistry, SecretScope
from lucent.secrets.utils import is_secret_reference, secret_key_from_reference

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

webhook_router = APIRouter(tags=["Integrations - Webhooks"])
admin_router = APIRouter(tags=["Integrations - Admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_encryptor() -> FernetEncryptor:
    """Lazily create a FernetEncryptor from the environment."""
    try:
        return FernetEncryptor()
    except EncryptionError as exc:
        logger.error("Credential encryption not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Credential encryption not configured on this server",
        ) from exc


def _integration_to_response(row: dict[str, Any]) -> IntegrationResponse:
    """Convert a DB row dict to an IntegrationResponse (never exposes config)."""
    channels = row.get("allowed_channels") or []
    if isinstance(channels, str):
        import json

        channels = json.loads(channels)
    return IntegrationResponse(
        id=row["id"],
        organization_id=row["organization_id"],
        type=row["type"],
        status=row["status"],
        external_workspace_id=row.get("external_workspace_id"),
        allowed_channels=channels,
        config_version=row.get("config_version", 1),
        created_by=row["created_by"],
        updated_by=row.get("updated_by"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        disabled_at=row.get("disabled_at"),
        revoked_at=row.get("revoked_at"),
    )


def _link_to_response(row: dict[str, Any]) -> UserLinkResponse:
    """Convert a DB row dict to a UserLinkResponse."""
    return UserLinkResponse(
        id=row["id"],
        organization_id=row["organization_id"],
        integration_id=row["integration_id"],
        user_id=row["user_id"],
        provider=row["provider"],
        external_user_id=row["external_user_id"],
        external_workspace_id=row.get("external_workspace_id"),
        status=row["status"],
        verification_method=row["verification_method"],
        linked_at=row.get("linked_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _resolve_integration_config_secrets(
    config: dict[str, Any], integration: dict[str, Any]
) -> dict[str, Any]:
    """Resolve secret:// references in integration config at point of use."""
    provider = SecretRegistry.get()
    scope = SecretScope(
        organization_id=str(integration["organization_id"]),
        owner_user_id=str(integration["created_by"]),
    )
    resolved = dict(config)
    for key, value in config.items():
        if not (isinstance(value, str) and is_secret_reference(value)):
            continue
        secret_key = secret_key_from_reference(value)
        if not secret_key:
            raise ValueError(f"Invalid secret reference for config key '{key}'")
        secret_value = await provider.get(secret_key, scope)
        if secret_value is None:
            raise KeyError(f"Secret not found for config key '{key}' (reference '{secret_key}')")
        resolved[key] = secret_value
    return resolved


# ===========================================================================
# Webhook endpoint — async ack + background processing
# ===========================================================================


@webhook_router.post("/webhook/{provider}")
async def receive_webhook(
    provider: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Receive an inbound webhook from a platform (Slack, Discord, etc.).

    Immediately returns 200 to satisfy the platform's timeout requirements,
    then processes the event asynchronously via ``IntegrationService``.

    Signature verification is handled by ``SignatureVerificationMiddleware``
    before this handler is reached.
    """
    pool = await get_pool()
    service = IntegrationService(pool)

    # Parse the raw request body (already buffered by middleware)
    body = await request.body()

    # Check for Slack URL verification challenge (must be synchronous)
    if provider == "slack":
        import json as _json

        try:
            payload = _json.loads(body)
            if payload.get("type") == "url_verification":
                return {"challenge": payload.get("challenge", "")}
        except (ValueError, KeyError):
            pass

    # Look up the active integration for this provider
    # The org is determined from the integration config, not from auth
    # (webhooks are unauthenticated — signature verification is the auth).
    from lucent.integrations.slack_adapter import SlackAdapter

    # Build adapter from integration config
    async def _process_webhook() -> None:
        """Background task: parse event, resolve integration, dispatch."""
        try:
            # Find all active integrations of this type
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM integrations WHERE type = $1 AND status = 'active'",
                    provider,
                )

            if not rows:
                logger.warning(
                    "Webhook received for provider '%s' but no active integration",
                    provider,
                )
                return

            integration = dict(rows[0])

            # Build adapter
            encryptor = FernetEncryptor()
            config = encryptor.decrypt(integration["encrypted_config"])
            config = await _resolve_integration_config_secrets(config, integration)

            if provider == "slack":
                adapter = SlackAdapter(
                    bot_token=config.get("bot_token", ""),
                    signing_secret=config.get("signing_secret", ""),
                )
            else:
                logger.warning("No adapter implementation for provider: %s", provider)
                return

            # Parse event from the buffered body
            # Re-create a minimal request-like object for the adapter
            from starlette.requests import Request as StarletteRequest

            scope = {
                "type": "http",
                "method": "POST",
                "headers": list(request.scope.get("headers", [])),
                "path": request.url.path,
            }
            mock_request = StarletteRequest(scope)
            mock_request._body = body

            event = await adapter.parse_event(mock_request)

            # Dispatch through the full pipeline
            result = await service.handle_event(event, integration, adapter)
            if not result.success:
                logger.warning(
                    "Webhook processing failed: provider=%s stage=%s",
                    provider, result.error_stage,
                )

            # Clean up adapter resources
            if hasattr(adapter, "close"):
                await adapter.close()

        except Exception:
            logger.exception("Background webhook processing failed: provider=%s", provider)

    background_tasks.add_task(_process_webhook)

    return {"status": "accepted"}


# ===========================================================================
# Admin CRUD — Integration management (MANAGE_INTEGRATIONS required)
# ===========================================================================


@admin_router.post(
    "",
    response_model=IntegrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_integration(
    body: IntegrationCreate,
    user: AdminUser,
) -> IntegrationResponse:
    """Create a new platform integration for the organization."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = IntegrationRepo(pool)
    encryptor = _get_encryptor()

    # Check for existing active integration of same type+workspace
    existing = await repo.get_active_by_type(
        str(user.organization_id),
        body.type.value,
        body.external_workspace_id,
    )
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"An active {body.type.value} integration already exists for this workspace",
        )

    encrypted_config = encryptor.encrypt(body.config)

    row = await repo.create(
        organization_id=str(user.organization_id),
        type=body.type.value,
        encrypted_config=encrypted_config,
        created_by=str(user.id),
        external_workspace_id=body.external_workspace_id,
        allowed_channels=body.allowed_channels,
    )

    # Audit
    try:
        audit = AuditRepository(pool)
        await audit.log_integration_event(
            event_type=INTEGRATION_CREATED,
            organization_id=user.organization_id,
            user_id=user.id,
            integration_id=UUID(str(row["id"])),
            context={"type": body.type.value},
        )
    except Exception:
        logger.warning("Failed to audit integration creation", exc_info=True)

    return _integration_to_response(row)


@admin_router.get("", response_model=IntegrationListResponse)
async def list_integrations(
    user: AdminUser,
    integration_status: str | None = None,
    limit: int = 50,
) -> IntegrationListResponse:
    """List all integrations for the organization."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = IntegrationRepo(pool)

    rows = await repo.list_by_org(
        str(user.organization_id),
        status=integration_status,
        limit=min(limit, 100),
    )

    return IntegrationListResponse(
        integrations=[_integration_to_response(r) for r in rows],
        total_count=len(rows),
    )


@admin_router.get("/{integration_id}", response_model=IntegrationResponse)
async def get_integration(
    integration_id: UUID,
    user: AdminUser,
) -> IntegrationResponse:
    """Get a single integration by ID."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = IntegrationRepo(pool)

    row = await repo.get_by_id(str(integration_id), str(user.organization_id))
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")

    return _integration_to_response(row)


@admin_router.patch("/{integration_id}", response_model=IntegrationResponse)
async def update_integration(
    integration_id: UUID,
    body: IntegrationUpdate,
    user: AdminUser,
) -> IntegrationResponse:
    """Update an integration's config, channels, or status."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = IntegrationRepo(pool)
    org_id = str(user.organization_id)
    int_id = str(integration_id)

    # Verify integration exists
    existing = await repo.get_by_id(int_id, org_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")

    # Handle status transitions
    if body.status is not None:
        transition_result = None
        if body.status == IntegrationStatus.ACTIVE:
            transition_result = await repo.activate(int_id, org_id, updated_by=str(user.id))
        elif body.status == IntegrationStatus.DISABLED:
            transition_result = await repo.disable(int_id, org_id, updated_by=str(user.id))
            if transition_result:
                # Orphan all active links when integration is disabled
                link_repo = UserLinkRepo(pool)
                await link_repo.bulk_orphan_by_integration(int_id, org_id)
        elif body.status == IntegrationStatus.REVOKED:
            transition_result = await repo.revoke(int_id, org_id, updated_by=str(user.id))
            if transition_result:
                link_repo = UserLinkRepo(pool)
                await link_repo.bulk_orphan_by_integration(int_id, org_id)
        elif body.status == IntegrationStatus.DELETED:
            transition_result = await repo.soft_delete(int_id, org_id, updated_by=str(user.id))

        if transition_result is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot transition from '{existing['status']}' to '{body.status.value}'",
            )

        # Audit status change
        try:
            audit = AuditRepository(pool)
            event_type = (
                INTEGRATION_DISABLED
                if body.status == IntegrationStatus.DISABLED
                else INTEGRATION_UPDATED
            )
            await audit.log_integration_event(
                event_type=event_type,
                organization_id=user.organization_id,
                user_id=user.id,
                integration_id=integration_id,
                context={"new_status": body.status.value, "old_status": existing["status"]},
            )
        except Exception:
            logger.warning("Failed to audit integration status change", exc_info=True)

        # If only status was updated, return the transition result
        if body.config is None and body.allowed_channels is None:
            return _integration_to_response(transition_result)

    # Handle config/channels update
    kwargs: dict[str, Any] = {"updated_by": str(user.id)}
    if body.allowed_channels is not None:
        kwargs["allowed_channels"] = body.allowed_channels
    if body.config is not None:
        encryptor = _get_encryptor()
        kwargs["encrypted_config"] = encryptor.encrypt(body.config)

    if len(kwargs) > 1:  # more than just updated_by
        row = await repo.update(int_id, org_id, **kwargs)
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")

        try:
            audit = AuditRepository(pool)
            await audit.log_integration_event(
                event_type=INTEGRATION_UPDATED,
                organization_id=user.organization_id,
                user_id=user.id,
                integration_id=integration_id,
                context={
                    "config_updated": body.config is not None,
                    "channels_updated": body.allowed_channels is not None,
                },
            )
        except Exception:
            logger.warning("Failed to audit integration update", exc_info=True)

        return _integration_to_response(row)

    # Re-fetch if only status changed
    updated = await repo.get_by_id(int_id, org_id)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")
    return _integration_to_response(updated)


@admin_router.delete("/{integration_id}", status_code=status.HTTP_200_OK)
async def delete_integration(
    integration_id: UUID,
    user: AdminUser,
) -> dict[str, str]:
    """Soft-delete an integration."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = IntegrationRepo(pool)
    org_id = str(user.organization_id)

    result = await repo.soft_delete(str(integration_id), org_id, updated_by=str(user.id))
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found or cannot be deleted")

    # Orphan all active links
    link_repo = UserLinkRepo(pool)
    await link_repo.bulk_orphan_by_integration(str(integration_id), org_id)

    return {"id": str(integration_id), "status": "deleted"}


# ===========================================================================
# Pairing endpoints — any authenticated user
# ===========================================================================


@admin_router.post(
    "/link",
    response_model=PairingChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_pairing_code(
    body: PairingChallengeCreate,
    user: AuthenticatedUser,
) -> PairingChallengeResponse:
    """Generate a pairing code for the current user to link their platform identity.

    The plaintext code is returned once and never stored. The user
    sends this code as a DM in the target platform to complete linking.
    """
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    integration_repo = IntegrationRepo(pool)

    # Verify the integration exists and belongs to user's org
    integration = await integration_repo.get_by_id(
        str(body.integration_id), str(user.organization_id),
    )
    if not integration:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")
    if integration["status"] != IntegrationStatus.ACTIVE.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Integration is not active")

    challenge_repo = PairingChallengeRepo(pool)
    from lucent.integrations.identity import PairingChallengeService

    svc = PairingChallengeService(challenge_repo)

    try:
        challenge, plaintext = await svc.generate(
            integration_id=str(body.integration_id),
            user_id=str(user.id),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    # Audit
    try:
        audit = AuditRepository(pool)
        await audit.log_integration_event(
            event_type=CHALLENGE_ISSUED,
            organization_id=user.organization_id,
            user_id=user.id,
            integration_id=body.integration_id,
        )
    except Exception:
        logger.warning("Failed to audit pairing code generation", exc_info=True)

    return PairingChallengeResponse(
        id=challenge["id"],
        integration_id=challenge["integration_id"],
        user_id=challenge["user_id"],
        code=plaintext,
        expires_at=challenge["expires_at"],
        status=challenge["status"],
        created_at=challenge["created_at"],
    )


@admin_router.post("/verify")
async def verify_pairing_code(
    body: dict[str, str],
    user: AuthenticatedUser,
) -> dict[str, Any]:
    """Verify a pairing code and activate the identity link.

    Request body: ``{"code": "...", "integration_id": "..."}``

    This endpoint is called from the platform side (e.g., a Slack DM command)
    after the user receives a pairing code from the Lucent web UI. It can also
    be called directly by authenticated users who know their external identity.
    """
    code = body.get("code")
    integration_id = body.get("integration_id")
    external_user_id = body.get("external_user_id")
    provider = body.get("provider")

    if not code or not integration_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Both 'code' and 'integration_id' are required",
        )

    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    integration_repo = IntegrationRepo(pool)

    # Verify integration exists in user's org
    integration = await integration_repo.get_by_id(
        integration_id, str(user.organization_id),
    )
    if not integration:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")

    # Determine provider from integration if not provided
    if not provider:
        provider = integration["type"]

    # Use the service for full redeem flow
    service = IntegrationService(pool)
    result = await service.verify_link_code(
        code=code,
        external_user_id=external_user_id or str(user.id),
        integration_id=integration_id,
        organization_id=str(user.organization_id),
        provider=provider,
        external_workspace_id=integration.get("external_workspace_id"),
    )

    if not result:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired pairing code")

    # Audit
    try:
        audit = AuditRepository(pool)
        await audit.log_integration_event(
            event_type=CHALLENGE_SUCCEEDED,
            organization_id=user.organization_id,
            user_id=user.id,
            integration_id=UUID(integration_id),
        )
    except Exception:
        logger.warning("Failed to audit pairing code verification", exc_info=True)

    return {"linked": True, "provider": provider}


# ===========================================================================
# Admin link management — MANAGE_INTEGRATIONS required
# ===========================================================================


@admin_router.get("/links", response_model=UserLinkListResponse)
async def list_user_links(
    user: AdminUser,
    integration_id: UUID | None = None,
    link_status: str | None = None,
    limit: int = 50,
) -> UserLinkListResponse:
    """List user links for the organization, with optional filters."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = UserLinkRepo(pool)
    org_id = str(user.organization_id)

    if integration_id:
        rows = await repo.list_by_integration(
            str(integration_id), org_id, status=link_status,
        )
    else:
        rows = await repo.list_by_org(org_id, status=link_status, limit=min(limit, 100))

    return UserLinkListResponse(
        links=[_link_to_response(r) for r in rows],
        total_count=len(rows),
    )


@admin_router.post(
    "/links",
    response_model=UserLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_link(
    body: UserLinkCreate,
    user: AdminUser,
) -> UserLinkResponse:
    """Admin-create a user link (skips pairing code flow)."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = UserLinkRepo(pool)
    integration_repo = IntegrationRepo(pool)
    org_id = str(user.organization_id)

    # Verify integration belongs to org
    integration = await integration_repo.get_by_id(str(body.integration_id), org_id)
    if not integration:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found")

    row = await repo.create(
        organization_id=org_id,
        integration_id=str(body.integration_id),
        user_id=str(body.user_id),
        provider=integration["type"],
        external_user_id=body.external_user_id,
        external_workspace_id=body.external_workspace_id,
        verification_method=VerificationMethod.ADMIN.value,
    )

    # Auto-activate admin-created links
    activated = await repo.activate(str(row["id"]), org_id)
    if activated:
        row = activated

    # Audit
    try:
        audit = AuditRepository(pool)
        await audit.log_integration_event(
            event_type=LINK_ACTIVATED,
            organization_id=user.organization_id,
            user_id=user.id,
            integration_id=body.integration_id,
            context={
                "linked_user_id": str(body.user_id),
                "external_user_id": body.external_user_id,
                "method": "admin",
            },
        )
    except Exception:
        logger.warning("Failed to audit admin link creation", exc_info=True)

    return _link_to_response(row)


@admin_router.delete("/links/{link_id}")
async def revoke_user_link(
    link_id: UUID,
    user: AdminUser,
) -> dict[str, str]:
    """Revoke (soft-delete) a user link."""
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User has no organization")

    pool = await get_pool()
    repo = UserLinkRepo(pool)
    org_id = str(user.organization_id)

    result = await repo.revoke(str(link_id), org_id, revoked_by=str(user.id))
    if not result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User link not found or cannot be revoked")

    # Audit
    try:
        audit = AuditRepository(pool)
        await audit.log_integration_event(
            event_type=LINK_REVOKED,
            organization_id=user.organization_id,
            user_id=user.id,
            context={"link_id": str(link_id)},
        )
    except Exception:
        logger.warning("Failed to audit link revocation", exc_info=True)

    return {"id": str(link_id), "status": "revoked"}
