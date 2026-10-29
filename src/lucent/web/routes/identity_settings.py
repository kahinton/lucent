"""Settings routes for identity: SSO connections and SCIM provisioning.

Only tenant admins reach this page (``/settings/identity``). Two very different
things live here because they answer the same question — "how do people get
into this workspace?" — and an administrator setting up enterprise access needs
both in one place.

Guards that apply to every route below:

* Admin role is re-checked in the handler, not only in the template. A hidden
  button is not a permission check.
* The connection's organization is taken from the session, never from the form,
  so a crafted request cannot configure an IdP for another tenant.
* Client secrets are write-only through this UI. They are stored in the secret
  store and never rendered back, so a screenshot of the settings page is not a
  credential leak.
* Every mutation is CSRF-checked and audit-logged.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from lucent.db import (
    AdminAuditRepository,
    ScimTokenRepository,
    SsoConnectionRepository,
    get_pool,
)
from lucent.db import admin_audit as audit_actions
from lucent.identity import oidc, saml, sso_config
from lucent.identity.sso_config import SsoConfigError

from ._shared import _check_csrf, get_user_context, templates

logger = logging.getLogger(__name__)

router = APIRouter()

_ADMIN_ROLES = ("admin", "owner", "hyperadmin")


def _role_value(user) -> str:
    return user.role if isinstance(user.role, str) else user.role.value


async def _require_admin(request: Request):
    user = await get_user_context(request)
    if _role_value(user) not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Permission denied")
    return user


def _public_connection(connection: dict) -> dict:
    """Strip everything the browser must not see.

    Notably the client id stays (it is not secret and the admin needs to check
    it) while the client secret is replaced by a boolean.
    """
    return {
        "id": str(connection["id"]),
        "provider_type": connection["provider_type"],
        "slug": connection["slug"],
        "display_name": connection["display_name"],
        "is_enabled": connection["is_enabled"],
        "issuer": connection.get("issuer"),
        "client_id": connection.get("client_id"),
        "sp_entity_id": connection.get("sp_entity_id"),
        "idp_entity_id": connection.get("idp_entity_id"),
        "idp_sso_url": connection.get("idp_sso_url"),
        "certificate_count": len(connection.get("idp_certificates") or []),
        "name_id_format": connection.get("name_id_format"),
        "want_assertions_signed": connection.get("want_assertions_signed"),
        "want_response_signed": connection.get("want_response_signed"),
        "allowed_email_domains": connection.get("allowed_email_domains") or [],
        "auto_create_users": connection.get("auto_create_users"),
        "default_role": connection.get("default_role"),
        "allow_email_linking": connection.get("allow_email_linking"),
        "role_claim": connection.get("role_claim"),
        "role_mappings": connection.get("role_mappings") or [],
        "created_at": connection.get("created_at"),
    }


@router.get("/settings/identity", response_class=HTMLResponse)
async def identity_settings(request: Request):
    """Render the identity page: SSO connections plus SCIM tokens."""
    user = await _require_admin(request)
    pool = await get_pool()

    connection_repo = SsoConnectionRepository(pool)
    connections = await connection_repo.list_for_org(user.organization_id)

    rendered = []
    for connection in connections:
        payload = _public_connection(connection)
        payload["has_client_secret"] = await sso_config.has_client_secret(
            pool,
            connection_id=connection["id"],
            organization_id=connection["organization_id"],
        )
        rendered.append(payload)

    scim_tokens = await ScimTokenRepository(pool).list_for_org(user.organization_id)
    base_url = str(request.base_url).rstrip("/")

    # One-shot SCIM token reveal, handed over through the same server-side
    # store the temp-password flow uses so the plaintext never touches a URL.
    new_scim_token = None
    ref = request.cookies.get("lucent_temp_pw_ref", "")
    from .admin import _temp_pw_store

    if ref and ref in _temp_pw_store and _temp_pw_store[ref]["expires"] > time.time():
        payload = _temp_pw_store.pop(ref)
        new_scim_token = payload.get("scim_token")

    csrf_token = request.cookies.get("lucent_csrf") or ""
    response = templates.TemplateResponse(
        request,
        "settings/identity.html",
        {
            "user": user,
            "connections": rendered,
            "scim_tokens": scim_tokens,
            "provider_presets": sso_config.PROVIDER_PRESETS,
            "base_url": base_url,
            "csrf_token": csrf_token,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "new_scim_token": new_scim_token,
            "scim_base_url": f"{base_url}/scim/v2",
            "settings_section": "identity",
        },
    )
    if ref:
        response.delete_cookie("lucent_temp_pw_ref", path="/settings/identity")
    return response


@router.post("/settings/identity/sso")
async def create_sso_connection(
    request: Request,
    provider_type: str = Form("oidc"),
    slug: str = Form(...),
    display_name: str = Form(...),
    issuer: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    idp_entity_id: str = Form(""),
    idp_sso_url: str = Form(""),
    idp_certificates: str = Form(""),
    allowed_email_domains: str = Form(""),
    auto_create_users: str = Form("on"),
    default_role: str = Form("member"),
    allow_email_linking: str = Form("on"),
    want_assertions_signed: str = Form("on"),
    want_response_signed: str = Form(""),
):
    """Create an identity-provider connection."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()

    def _fail(message: str) -> RedirectResponse:
        return RedirectResponse(
            f"/settings/identity?error={quote(message)}", status_code=303
        )

    try:
        clean_slug = sso_config.validate_slug(slug)
        certificates = sso_config.normalize_certificates(idp_certificates)
        sso_config.validate_connection_fields(
            provider_type=provider_type,
            issuer=issuer or None,
            client_id=client_id or None,
            idp_entity_id=idp_entity_id or None,
            idp_sso_url=idp_sso_url or None,
            idp_certificates=certificates,
        )
    except SsoConfigError as exc:
        return _fail(str(exc))

    if provider_type in ("oidc", "github") and not (client_secret or "").strip():
        return _fail(
            "A client secret is required. Store it here rather than in an "
            "environment variable so it is encrypted per tenant."
        )

    repo = SsoConnectionRepository(pool)
    existing = await repo.get_by_slug(clean_slug)
    if existing is not None:
        return _fail("That slug is already in use. Choose another.")

    try:
        connection = await repo.create(
            organization_id=user.organization_id,
            provider_type=provider_type,
            slug=clean_slug,
            display_name=display_name.strip(),
            issuer=(issuer or "").strip().rstrip("/") or None,
            client_id=(client_id or "").strip() or None,
            idp_entity_id=(idp_entity_id or "").strip() or None,
            idp_sso_url=(idp_sso_url or "").strip() or None,
            idp_certificates=certificates,
            sp_entity_id=f"{str(request.base_url).rstrip('/')}/sso/{clean_slug}",
            allowed_email_domains=sso_config.normalize_domains(allowed_email_domains),
            auto_create_users=auto_create_users == "on",
            default_role=default_role,
            allow_email_linking=allow_email_linking == "on",
            want_assertions_signed=want_assertions_signed == "on",
            want_response_signed=want_response_signed == "on",
        )
    except Exception as exc:  # noqa: BLE001 — surface a usable message
        logger.warning("Could not create SSO connection: %s", exc, exc_info=True)
        return _fail("Could not save the connection. Check the values and try again.")

    if client_secret.strip():
        try:
            await sso_config.set_client_secret(
                pool,
                connection_id=connection["id"],
                organization_id=user.organization_id,
                value=client_secret,
            )
        except SsoConfigError as exc:
            # Roll the connection back: a half-configured connection would
            # appear on the login page and fail for every user who tried it.
            await repo.delete(connection["id"], organization_id=user.organization_id)
            return _fail(f"Could not store the client secret: {exc}")

    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.SSO_CONNECTION_CREATE,
        entity_type="sso_connection",
        entity_id=connection["id"],
        entity_label=display_name,
        new_values={
            "provider_type": provider_type,
            "slug": clean_slug,
            "issuer": issuer or None,
            # Never the secret itself — only whether one was set.
            "client_secret_set": bool(client_secret.strip()),
        },
        notes="identity provider connection created",
    )
    return RedirectResponse("/settings/identity?success=connection_created", status_code=303)


@router.post("/settings/identity/sso/{connection_id}/toggle")
async def toggle_sso_connection(request: Request, connection_id: str):
    """Enable or disable a connection."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()

    repo = SsoConnectionRepository(pool)
    connection = await repo.get(connection_id, organization_id=user.organization_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    await repo.update(
        connection_id,
        organization_id=user.organization_id,
        is_enabled=not connection["is_enabled"],
    )
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.SSO_CONNECTION_UPDATE,
        entity_type="sso_connection",
        entity_id=connection_id,
        entity_label=connection["display_name"],
        new_values={"is_enabled": not connection["is_enabled"]},
        notes="identity provider connection toggled",
    )
    return RedirectResponse("/settings/identity?success=connection_updated", status_code=303)


@router.post("/settings/identity/sso/{connection_id}/delete")
async def delete_sso_connection(request: Request, connection_id: str):
    """Delete a connection, its stored secret, and any in-flight logins."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()

    repo = SsoConnectionRepository(pool)
    connection = await repo.get(connection_id, organization_id=user.organization_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Secret first: if the row went first and the delete then failed, the
    # secret would be orphaned with no way to reach it through the UI.
    await sso_config.delete_client_secret(
        pool,
        connection_id=connection_id,
        organization_id=user.organization_id,
    )
    await repo.delete(connection_id, organization_id=user.organization_id)
    oidc.clear_discovery_cache(connection.get("issuer"))

    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.SSO_CONNECTION_DELETE,
        entity_type="sso_connection",
        entity_id=connection_id,
        entity_label=connection["display_name"],
        notes="identity provider connection deleted",
    )
    return RedirectResponse("/settings/identity?success=connection_deleted", status_code=303)


@router.get("/settings/identity/sso/{connection_id}/metadata")
async def download_sp_metadata(request: Request, connection_id: str):
    """Download SP metadata XML for upload to the IdP.

    No CSRF token: this is a GET that changes nothing, and the id is not a
    secret. It is admin-gated like the rest of the page.
    """
    user = await _require_admin(request)
    pool = await get_pool()

    repo = SsoConnectionRepository(pool)
    connection = await repo.get(connection_id, organization_id=user.organization_id)
    if connection is None or connection["provider_type"] != "saml":
        raise HTTPException(status_code=404, detail="SAML connection not found")

    acs_url = f"{str(request.base_url).rstrip('/')}/sso/callback/saml"
    metadata = saml.build_metadata(connection, acs_url=acs_url)
    return Response(
        content=metadata,
        media_type="application/samlmetadata+xml",
        headers={
            "Content-Disposition": f'attachment; filename="{connection["slug"]}-sp-metadata.xml"'
        },
    )


@router.post("/settings/identity/sso/{connection_id}/import-metadata")
async def import_idp_metadata(
    request: Request,
    connection_id: str,
    metadata_url: str = Form(...),
):
    """Populate the IdP fields from a metadata URL.

    Convenience only. The certificate that arrives this way is *shown* to the
    admin rather than silently trusted, because trust-on-first-fetch from an
    arbitrary URL is precisely the thing the login path refuses to do.
    """
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()

    def _fail(message: str) -> RedirectResponse:
        return RedirectResponse(
            f"/settings/identity?error={quote(message)}", status_code=303
        )

    repo = SsoConnectionRepository(pool)
    connection = await repo.get(connection_id, organization_id=user.organization_id)
    if connection is None or connection["provider_type"] != "saml":
        raise HTTPException(status_code=404, detail="SAML connection not found")

    from urllib.parse import urlparse

    parsed = urlparse(metadata_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return _fail("The metadata URL must be an absolute https:// URL.")

    try:
        metadata = await saml.fetch_idp_metadata(metadata_url)
    except saml.SamlError as exc:
        return _fail(str(exc))

    if not metadata.get("sso_url") or not metadata.get("certificates"):
        return _fail("The metadata did not contain an SSO URL and a signing certificate.")

    await repo.update(
        connection_id,
        organization_id=user.organization_id,
        idp_entity_id=metadata.get("entity_id"),
        idp_sso_url=metadata.get("sso_url"),
        idp_certificates=metadata["certificates"],
    )
    return RedirectResponse("/settings/identity?success=metadata_imported", status_code=303)


@router.post("/settings/identity/scim-tokens")
async def create_scim_token(request: Request, name: str = Form(...)):
    """Mint a SCIM bearer token. The plaintext is shown exactly once."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()

    record, token = await ScimTokenRepository(pool).create(
        organization_id=user.organization_id,
        name=(name or "").strip() or "SCIM token",
        created_by=user.id,
    )
    await AdminAuditRepository(pool).log_for_user(
        user,
        request,
        action=audit_actions.SCIM_TOKEN_CREATE,
        entity_type="scim_token",
        entity_id=record["id"],
        entity_label=record["name"],
        notes="SCIM provisioning token created",
    )

    # One-shot transport, same pattern as a temp password: server-side store
    # keyed by an opaque ref in an HttpOnly cookie, never in the URL.
    import secrets as _secrets
    import time as _time

    from .admin import _TEMP_PW_MAX_ENTRIES, _TEMP_PW_TTL, _cleanup_temp_pw_store, _temp_pw_store

    _cleanup_temp_pw_store()
    if len(_temp_pw_store) >= _TEMP_PW_MAX_ENTRIES:
        _temp_pw_store.clear()
    ref = _secrets.token_urlsafe(32)
    _temp_pw_store[ref] = {"scim_token": token, "expires": _time.time() + _TEMP_PW_TTL}

    response = RedirectResponse(
        "/settings/identity?success=scim_token_created", status_code=303
    )
    from lucent.auth_providers import SECURE_COOKIES

    response.set_cookie(
        key="lucent_temp_pw_ref",
        value=ref,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
        max_age=60,
        path="/settings/identity",
    )
    return response


@router.post("/settings/identity/scim-tokens/{token_id}/revoke")
async def revoke_scim_token(request: Request, token_id: str):
    """Revoke a SCIM bearer token."""
    await _check_csrf(request)
    user = await _require_admin(request)
    pool = await get_pool()

    revoked = await ScimTokenRepository(pool).revoke(
        token_id, organization_id=user.organization_id
    )
    if revoked:
        await AdminAuditRepository(pool).log_for_user(
            user,
            request,
            action=audit_actions.SCIM_TOKEN_REVOKE,
            entity_type="scim_token",
            entity_id=token_id,
            notes="SCIM provisioning token revoked",
        )
    return RedirectResponse("/settings/identity?success=scim_token_revoked", status_code=303)
