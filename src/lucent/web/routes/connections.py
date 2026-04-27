"""Connections management — enterprise OAuth integrations and PAT tokens."""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from lucent.db import get_pool
from lucent.integrations.connection_flags import (
    connection_feature_state,
    env_token_claim_enabled,
    pat_enabled,
)
from lucent.logging import get_logger
from lucent.rbac import Permission, Role

from ._shared import _check_csrf, _get_csrf_for_request, get_user_context, templates


async def _check_csrf_json(request: Request) -> None:
    """CSRF check for session-cookie JSON endpoints.

    Reads the token from the ``X-CSRF-Token`` header (the convention used
    by other JSON web routes — see ``sandboxes.py``). Falls back to
    ``X-CSRFToken`` for legacy clients. Raises 403 on failure.
    """
    token = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or ""
    await _check_csrf(request, form_token=token)


def _feature_disabled(name: str, reason: str) -> JSONResponse:
    """Structured 403 response for a hard feature-flag rejection.

    Returned as 403 (not 400) because the user is not allowed to perform
    the action in this deployment, regardless of payload validity.
    """
    return JSONResponse(
        {"error": reason, "code": "feature_disabled", "feature": name},
        status_code=403,
    )

logger = get_logger("web.routes.connections")

router = APIRouter()

# Env var tokens that indicate a built-in connection
_ENV_TOKEN_MAP = {
    "github": "GITHUB_TOKEN",
}

# Provider catalog used by both the page and the mutation endpoints.
# Single source of truth so the read-model builder and POST handlers
# can't drift apart.
_PROVIDERS: list[dict[str, object]] = [
    {
        "id": "github",
        "name": "GitHub",
        "description": "Access repositories, issues, pull requests, and code",
        "icon": "github",
        "scopes": ["repo", "read:org", "read:user"],
        "pat_url": "https://github.com/settings/tokens",
        "pat_prefix": "ghp_",
        "supports_pat": True,
        "supports_oauth": True,
        "supports_workspace_app": True,
    },
    {
        "id": "slack",
        "name": "Slack",
        "description": "Send messages, read channels, and manage workflows",
        "icon": "slack",
        "scopes": ["channels:read", "chat:write", "users:read"],
        "pat_url": "https://api.slack.com/apps",
        "pat_prefix": "xoxb-",
        "supports_pat": True,
        "supports_oauth": True,
        "supports_workspace_app": True,
    },
    {
        "id": "jira",
        "name": "Jira",
        "description": "Track issues, manage projects, and sync work items",
        "icon": "jira",
        "scopes": ["read:jira-work", "write:jira-work"],
        "pat_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
        "pat_prefix": "",
        "supports_pat": True,
        "supports_oauth": True,
        "supports_workspace_app": False,
    },
]


def _detect_env_connections() -> dict[str, dict]:
    """Detect connections available via environment variables.

    Returns an empty dict when ``LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED``
    is off, so deployments that disable env-token claiming do not even
    surface the env tokens in the read model.
    """
    if not env_token_claim_enabled():
        return {}
    found: dict[str, dict] = {}
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


async def _verify_github_token(token: str) -> dict | None:
    """Verify a GitHub token and return user info, or None on failure."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "login": data.get("login"),
                    "name": data.get("name"),
                    "avatar_url": data.get("avatar_url"),
                    "html_url": data.get("html_url"),
                }
    except Exception as e:
        logger.warning("GitHub token verification failed: %s", e)
    return None


def _build_provider_capabilities(
    feature_flags: dict[str, object],
) -> list[dict[str, object]]:
    """Project the static provider catalog through the active feature flags."""
    pat_on = bool(feature_flags["pat_enabled"])
    oauth_on = bool(feature_flags["oauth_enabled"])
    app_on = bool(feature_flags["github_app_enabled"])
    oauth_configured: dict[str, bool] = dict(
        feature_flags.get("provider_oauth_configured") or {}
    )

    out: list[dict[str, object]] = []
    for p in _PROVIDERS:
        pid = str(p["id"])
        is_oauth_configured = oauth_configured.get(pid, False)
        out.append(
            {
                "id": pid,
                "name": p["name"],
                "description": p["description"],
                "icon": p["icon"],
                "default_scopes": list(p["scopes"]),  # type: ignore[arg-type]
                "pat_url": p["pat_url"],
                "pat_prefix": p["pat_prefix"],
                "supports_pat": bool(p["supports_pat"]) and pat_on,
                "supports_oauth": (
                    bool(p["supports_oauth"]) and oauth_on and is_oauth_configured
                ),
                # GitHub App is the only "workspace app" today; gate by its
                # own flag. Other providers report False until plumbed.
                "supports_workspace_app": (
                    bool(p["supports_workspace_app"]) and pid == "github" and app_on
                ),
                "oauth_configured": is_oauth_configured,
            }
        )
    return out


def _credential_to_account(cred: dict) -> dict[str, object]:
    """Project an ``enterprise_credentials`` row into the read-model shape."""
    metadata = cred.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    source = metadata.get("source") or "pat"
    if source == "env_var":
        source = "env"
    return {
        "id": cred.get("id"),
        "provider": cred.get("integration_type"),
        "credential_kind": cred.get("credential_kind"),
        "display_name": cred.get("display_name"),
        "scopes": cred.get("scopes") or [],
        "status": cred.get("status"),
        "expires_at": cred.get("access_token_expires_at"),
        "source": source,
        "metadata_safe": {
            k: v
            for k, v in metadata.items()
            if k in {"github_login", "github_name", "github_avatar", "env_var"}
        },
    }


def _integration_to_workspace_connection(
    row: dict,
    *,
    can_manage: bool,
) -> dict[str, object]:
    """Project an ``integrations`` row into the read-model shape."""
    return {
        "id": row.get("id"),
        "type": row.get("type"),
        "display_name": row.get("external_workspace_id") or row.get("type"),
        "external_workspace_id": row.get("external_workspace_id"),
        "install_id": row.get("install_id"),
        "status": row.get("status"),
        "installed_by": row.get("created_by"),
        "installed_at": row.get("created_at"),
        "health": {
            "status": row.get("health_status") or "unknown",
            "detail": row.get("health_detail"),
            "checked_at": row.get("health_checked_at"),
        },
        "actions": {
            "can_disable": can_manage,
            "can_revoke": can_manage,
        },
    }


async def build_connections_view_model(
    *,
    user,
    pool,
) -> dict[str, object]:
    """Build the explicit Connections page read model.

    Sections:
      * ``feature_flags``         — snapshot from ``connection_feature_state()``
      * ``admin_permissions``     — what the current user can mutate
      * ``provider_capabilities`` — static catalog × flags
      * ``workspace_connections`` — rows from ``integrations`` (org-scoped)
      * ``your_connected_accounts`` — rows from ``enterprise_credentials`` for
                                     the current user (scope_type='user')
      * ``env_detected``          — env-token candidates (empty if disabled)
    """
    flags = connection_feature_state().to_dict()

    can_manage = user.has_permission(Permission.MANAGE_INTEGRATIONS)
    is_owner = getattr(user.role, "value", str(user.role)) == Role.OWNER.value
    admin_permissions = {
        "manage_integrations": bool(can_manage),
        "is_owner": bool(is_owner),
    }

    # Personal section — current user's enterprise credentials.
    from lucent.integrations.credential_repository import CredentialRepository
    cred_repo = CredentialRepository(pool)
    credentials = await cred_repo.list_credentials(
        organization_id=str(user.organization_id),
        owner_user_id=str(user.id),
        scope_type="user",
    )
    your_connected_accounts = [_credential_to_account(c) for c in credentials]

    # Workspace section — integrations rows for the org. Only fetch when
    # the workspace integrations feature is on; an off flag means
    # "don't even show this part of the page".
    workspace_connections: list[dict[str, object]] = []
    if flags["workspace_integrations_enabled"]:
        from lucent.integrations.repositories import IntegrationRepo
        int_repo = IntegrationRepo(pool)
        integrations_rows = await int_repo.list_by_org(str(user.organization_id))
        workspace_connections = [
            _integration_to_workspace_connection(r, can_manage=can_manage)
            for r in integrations_rows
            if r.get("status") != "deleted"
        ]

    return {
        "feature_flags": flags,
        "admin_permissions": admin_permissions,
        "provider_capabilities": _build_provider_capabilities(flags),
        "workspace_connections": workspace_connections,
        "your_connected_accounts": your_connected_accounts,
        "env_detected": _detect_env_connections(),
    }


@router.get("/connections", response_class=HTMLResponse)
async def connections_page(request: Request):
    """Connections page — manage workspace integrations + personal accounts."""
    user = await get_user_context(request)
    pool = await get_pool()

    view_model = await build_connections_view_model(user=user, pool=pool)

    # Check if encryption is available (Vault transit) — kept for the
    # template's existing banner; orthogonal to the read model.
    encryption_available = False
    encryption_method = None
    try:
        from lucent.integrations.encryption import get_encryption_backend
        backend = get_encryption_backend()
        encryption_available = True
        encryption_method = type(backend).__name__
    except Exception as exc:
        logger.warning("Credential encryption unavailable: %s", exc)

    # Back-compat aliases for the existing template (UI rewrite happens
    # in a follow-up task — do not touch connections.html here). The new
    # explicit fields are added alongside the legacy keys.
    flags = view_model["feature_flags"]
    legacy_connected = {
        a["provider"]: a for a in view_model["your_connected_accounts"]  # type: ignore[index]
    }
    legacy_providers = [
        {
            "id": p["id"],
            "name": p["name"],
            "description": p["description"],
            "icon": p["icon"],
            "scopes": p["default_scopes"],
            "pat_url": p["pat_url"],
            "pat_prefix": p["pat_prefix"],
        }
        for p in view_model["provider_capabilities"]  # type: ignore[union-attr]
    ]

    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "user": user,
            "csrf_token": _get_csrf_for_request(request),
            # New explicit read model
            "view_model": view_model,
            "feature_flags": view_model["feature_flags"],
            "admin_permissions": view_model["admin_permissions"],
            "provider_capabilities": view_model["provider_capabilities"],
            "workspace_connections": view_model["workspace_connections"],
            "your_connected_accounts": view_model["your_connected_accounts"],
            "env_detected": view_model["env_detected"],
            # Legacy keys preserved so older templates keep rendering
            # until any remaining call sites migrate.
            "providers": legacy_providers,
            "connected": legacy_connected,
            "credentials": [
                {
                    "id": a["id"],
                    "integration_type": a["provider"],
                    "display_name": a["display_name"],
                    "credential_kind": a["credential_kind"],
                    "scopes": a["scopes"],
                    "status": a["status"],
                }
                for a in view_model["your_connected_accounts"]  # type: ignore[union-attr]
            ],
            "env_connections": view_model["env_detected"],
            "encryption_available": encryption_available,
            "oauth_configured": flags["provider_oauth_configured"],  # type: ignore[index]
        },
    )


@router.post("/connections/pat")
async def save_pat_web(request: Request):
    """Save a personal access token.

    Hardened: requires CSRF (header ``X-CSRF-Token``) and respects
    ``LUCENT_CONNECTIONS_PAT_ENABLED``. The flag check is enforced
    server-side regardless of whether the UI hides the form.
    """
    user = await get_user_context(request)
    await _check_csrf_json(request)

    if not pat_enabled():
        return _feature_disabled(
            "LUCENT_CONNECTIONS_PAT_ENABLED",
            "Personal access tokens are disabled in this deployment",
        )

    body = await request.json()
    provider = body.get("provider")
    token = body.get("token", "").strip()
    display_name = body.get("display_name", "").strip()

    if provider not in ("github", "slack", "jira"):
        return JSONResponse({"error": "Invalid provider"}, status_code=400)
    if not token:
        return JSONResponse({"error": "Token is required"}, status_code=400)

    pool = await get_pool()
    from lucent.integrations.credential_models import CredentialCreate, CredentialKind
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService

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


@router.post("/connections/env/claim")
async def claim_env_token(request: Request):
    """Verify an env var token against the provider API and store it as a user credential.

    Hardened: requires CSRF and respects
    ``LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED``.
    """
    user = await get_user_context(request)
    await _check_csrf_json(request)

    if not env_token_claim_enabled():
        return _feature_disabled(
            "LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED",
            "Environment-token claiming is disabled in this deployment",
        )

    body = await request.json()
    provider = body.get("provider")

    if provider not in _ENV_TOKEN_MAP:
        return JSONResponse({"error": "Invalid provider"}, status_code=400)

    env_var = _ENV_TOKEN_MAP[provider]
    token = os.environ.get(env_var, "")
    if not token:
        return JSONResponse({"error": f"{env_var} is not set"}, status_code=400)

    # Verify the token against the provider API
    github_user = None
    if provider == "github":
        github_user = await _verify_github_token(token)
        if not github_user:
            return JSONResponse({"error": "Token verification failed — could not authenticate with GitHub API"}, status_code=400)

    # Store as a proper user credential
    pool = await get_pool()
    from lucent.integrations.credential_models import CredentialCreate, CredentialKind
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService

    repo = CredentialRepository(pool)
    service = CredentialService(repo)

    display_name = f"GitHub (@{github_user['login']})" if github_user else f"{provider.title()} (env)"
    metadata = {}
    if github_user:
        metadata = {
            "github_login": github_user["login"],
            "github_name": github_user.get("name") or "",
            "github_avatar": github_user.get("avatar_url") or "",
            "source": "env_var",
            "env_var": env_var,
        }

    try:
        result = await service.create_credential(
            payload=CredentialCreate(
                integration_type=provider,
                credential_kind=CredentialKind.API_KEY,
                display_name=display_name,
                access_token=token,
                scopes=[],
                metadata=metadata,
            ),
            user=user,
        )
        return JSONResponse({
            "status": "claimed",
            "id": str(result["id"]),
            "github_user": github_user,
        })
    except Exception as e:
        logger.error("Env token claim failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/connections/oauth/start")
async def oauth_start_web(request: Request):
    """Start OAuth flow — session-authenticated web endpoint.

    Hardened: requires CSRF. This endpoint mints state and writes it to
    the database (``service.start_oauth``), so it counts as a state-
    mutating request and is CSRF-protected.
    """
    user = await get_user_context(request)
    await _check_csrf_json(request)
    body = await request.json()
    provider = body.get("provider")

    if provider not in ("github", "slack", "jira"):
        raise HTTPException(400, "Invalid provider")

    pool = await get_pool()
    from lucent.integrations.credential_models import OAuthStartRequest
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService

    repo = CredentialRepository(pool)
    service = CredentialService(repo)

    try:
        result = await service.start_oauth(
            actor_user_id=str(user.id),
            actor_org_id=str(user.organization_id),
            actor_role=user.role.value,
            request_body=OAuthStartRequest(
                provider=provider,
                redirect_uri=body.get("redirect_uri", f"{request.base_url}settings/connections/oauth/callback"),
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
    from lucent.integrations.credential_models import OAuthCallbackRequest
    from lucent.integrations.credential_repository import CredentialRepository
    from lucent.integrations.credential_service import CredentialService

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
                redirect_uri=f"{request.base_url}settings/connections/oauth/callback",
            ),
        )
        # Redirect back to connections page with success
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/settings/connections?connected=true", status_code=303)
    except Exception as e:
        logger.error("OAuth callback failed: %s", e)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/settings/connections?error={str(e)[:100]}", status_code=303)


@router.post("/connections/{credential_id}/revoke")
async def revoke_connection_web(request: Request, credential_id: str):
    """Revoke a connection.

    Hardened: requires CSRF. The form template emits ``csrf_token`` as a
    hidden input; the previous version of this handler never validated
    it. We now read it from either the form field (for the rendered
    HTML form) or the ``X-CSRF-Token`` header (for fetch() callers).
    """
    user = await get_user_context(request)

    # Accept either form-field or header CSRF (the template uses the
    # form field; the OAuth/PAT JS uses the header).
    header_token = request.headers.get("X-CSRF-Token") or ""
    if header_token:
        await _check_csrf(request, form_token=header_token)
    else:
        await _check_csrf(request)

    pool = await get_pool()

    from lucent.integrations.credential_repository import CredentialRepository
    repo = CredentialRepository(pool)

    await repo.delete_credential(credential_id, str(user.organization_id))

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/settings/connections", status_code=303)
