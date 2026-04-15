"""Connections management — enterprise OAuth integrations and PAT tokens."""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from lucent.db import get_pool
from lucent.logging import get_logger

from ._shared import _check_csrf, get_user_context, templates

logger = get_logger("web.routes.connections")

router = APIRouter()

# Env var tokens that indicate a built-in connection
_ENV_TOKEN_MAP = {
    "github": "GITHUB_TOKEN",
}


def _detect_env_connections() -> dict[str, dict]:
    """Detect connections available via environment variables."""
    found = {}
    for provider, env_var in _ENV_TOKEN_MAP.items():
        token = os.environ.get(env_var, "")
        if token:
            # Mask the token for display
            masked = token[:4] + "•" * 8 + token[-4:] if len(token) > 12 else "•" * len(token)
            found[provider] = {
                "source": "env",
                "env_var": env_var,
                "masked_token": masked,
                "display_name": f"Environment ({env_var})",
            }
    return found


@router.get("/connections", response_class=HTMLResponse)
async def connections_page(request: Request):
    """Connections page — manage enterprise OAuth integrations."""
    user = await get_user_context(request)
    pool = await get_pool()

    # Fetch existing credentials for this org
    from lucent.integrations.credential_repository import CredentialRepository
    repo = CredentialRepository(pool)
    
    credentials = await repo.list_credentials(
        organization_id=str(user.organization_id),
        owner_user_id=str(user.id),
    )

    # Detect env var connections
    env_connections = _detect_env_connections()

    # Check if encryption is available (Vault transit)
    encryption_available = False
    encryption_method = None
    try:
        from lucent.integrations.encryption import get_encryption_backend
        backend = get_encryption_backend()
        encryption_available = True
        encryption_method = type(backend).__name__
    except Exception as exc:
        logger.warning("Credential encryption unavailable: %s", exc)

    # Check which OAuth providers are configured
    oauth_configured = {}
    for provider in ("github", "slack", "jira"):
        client_id = os.environ.get(f"LUCENT_OAUTH_{provider.upper()}_CLIENT_ID", "")
        oauth_configured[provider] = bool(client_id)

    # Available providers
    providers = [
        {
            "id": "github",
            "name": "GitHub",
            "description": "Access repositories, issues, pull requests, and code",
            "icon": "github",
            "scopes": ["repo", "read:org", "read:user"],
            "pat_url": "https://github.com/settings/tokens",
            "pat_prefix": "ghp_",
        },
        {
            "id": "slack",
            "name": "Slack",
            "description": "Send messages, read channels, and manage workflows",
            "icon": "slack",
            "scopes": ["channels:read", "chat:write", "users:read"],
            "pat_url": "https://api.slack.com/apps",
            "pat_prefix": "xoxb-",
        },
        {
            "id": "jira",
            "name": "Jira",
            "description": "Track issues, manage projects, and sync work items",
            "icon": "jira",
            "scopes": ["read:jira-work", "write:jira-work"],
            "pat_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
            "pat_prefix": "",
        },
    ]

    # Map credentials to providers
    connected = {}
    for cred in credentials:
        itype = cred.get("integration_type")
        if itype:
            connected[itype] = cred

    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "user": user,
            "providers": providers,
            "connected": connected,
            "credentials": credentials,
            "env_connections": env_connections,
            "encryption_available": encryption_available,
            "oauth_configured": oauth_configured,
        },
    )


@router.post("/connections/pat")
async def save_pat_web(request: Request):
    """Save a personal access token."""
    user = await get_user_context(request)
    body = await request.json()
    provider = body.get("provider")
    token = body.get("token", "").strip()
    display_name = body.get("display_name", "").strip()

    if provider not in ("github", "slack", "jira"):
        return JSONResponse({"error": "Invalid provider"}, status_code=400)
    if not token:
        return JSONResponse({"error": "Token is required"}, status_code=400)

    pool = await get_pool()
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService
    from lucent.integrations.credential_models import CredentialCreate, CredentialKind

    repo = CredentialRepository(pool)
    service = CredentialService(repo)

    try:
        result = await service.create_credential(
            payload=CredentialCreate(
                integration_type=provider,
                credential_kind=CredentialKind.API_KEY,
                display_name=display_name or f"{provider.title()} PAT",
                access_token=token,
                scopes=[],
            ),
            user=user,
        )
        return JSONResponse({"status": "saved", "id": str(result["id"])})
    except Exception as e:
        logger.error("PAT save failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/connections/oauth/start")
async def oauth_start_web(request: Request):
    """Start OAuth flow — session-authenticated web endpoint."""
    user = await get_user_context(request)
    body = await request.json()
    provider = body.get("provider")
    
    if provider not in ("github", "slack", "jira"):
        raise HTTPException(400, "Invalid provider")

    pool = await get_pool()
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService
    from lucent.integrations.credential_models import OAuthStartRequest

    repo = CredentialRepository(pool)
    service = CredentialService(repo)

    try:
        result = await service.start_oauth(
            actor_user_id=str(user.id),
            actor_org_id=str(user.organization_id),
            actor_role=user.role.value,
            request_body=OAuthStartRequest(
                provider=provider,
                redirect_uri=body.get("redirect_uri", f"{request.base_url}connections/oauth/callback"),
                scopes=body.get("scopes"),
            ),
        )
        return JSONResponse({"authorization_url": result.authorization_url, "state": result.state})
    except Exception as e:
        logger.error("OAuth start failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/connections/oauth/callback", response_class=HTMLResponse)
async def oauth_callback_web(request: Request):
    """OAuth callback — exchanges code for tokens."""
    user = await get_user_context(request)
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        raise HTTPException(400, "Missing code or state parameter")

    pool = await get_pool()
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService
    from lucent.integrations.credential_models import OAuthCallbackRequest

    repo = CredentialRepository(pool)
    service = CredentialService(repo)

    try:
        result = await service.complete_oauth(
            actor_user_id=str(user.id),
            actor_org_id=str(user.organization_id),
            actor_role=user.role.value,
            request_body=OAuthCallbackRequest(
                code=code,
                state=state,
                redirect_uri=f"{request.base_url}connections/oauth/callback",
            ),
        )
        # Redirect back to connections page with success
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/connections?connected=true", status_code=303)
    except Exception as e:
        logger.error("OAuth callback failed: %s", e)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/connections?error={str(e)[:100]}", status_code=303)


@router.post("/connections/{credential_id}/revoke")
async def revoke_connection_web(request: Request, credential_id: str):
    """Revoke a connection."""
    user = await get_user_context(request)
    pool = await get_pool()
    
    from lucent.integrations.credential_repository import CredentialRepository
    repo = CredentialRepository(pool)
    
    await repo.delete_credential(credential_id, str(user.organization_id))
    
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/connections", status_code=303)
