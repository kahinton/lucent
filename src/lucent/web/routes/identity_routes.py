"""Public identity routes — self-serve signup, invitation redemption, and SSO.

Everything in this module is reachable **without** a session, which makes it the
most exposed surface in the application. The rules that keep it safe:

* Every state-changing POST is CSRF-checked through the same double-submit
  helper the rest of the web UI uses, and every POST is rate limited per IP so a
  token can't be brute forced through the form.
* Tokens are looked up from the request each time; the rendered page's copy is
  never trusted. State is single-use in the database, so a replayed form fails
  even if it carries a syntactically valid token.
* Nothing here reveals whether an email address has an account. A failed
  invitation says "invalid, expired, or already used" for all three cases.
* A new session is minted only after the identity check succeeds, and the
  session token is a fresh random value — never derived from the invitation or
  the IdP response.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.auth_providers import (
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_HOURS,
    create_session,
    generate_csrf_token,
    get_cookie_params,
    set_user_password,
    validate_password_complexity,
)
from lucent.db import (
    AdminAuditRepository,
    OrganizationRepository,
    SsoConnectionRepository,
    UserRepository,
    get_pool,
)
from lucent.db import admin_audit as audit_actions
from lucent.db.identity import SsoLoginStateRepository
from lucent.identity import oidc, saml, sso_config
from lucent.identity.provisioning import FederatedIdentityError
from lucent.services import identity as identity_service

from ._shared import _check_csrf, _set_csrf_cookie, templates

logger = logging.getLogger(__name__)

router = APIRouter()

#: Query parameter carrying a message to the login page. Kept as a fixed set of
#: codes rather than free text so the page cannot be used to render an
#: attacker-authored sentence in a trusted context.
LOGIN_MESSAGES = {
    "signed_out": "You have been signed out.",
    "invited": "Your account is ready. Sign in to continue.",
    "sso_failed": "Single sign-on could not be completed. Please try again.",
    "sso_denied": "Single sign-on did not authorize access to this workspace.",
    "expired": "That link has expired. Ask an administrator for a new one.",
}


def _login_redirect(message: str | None = None) -> RedirectResponse:
    if message:
        return RedirectResponse(f"/login?notice={quote(message)}", status_code=303)
    return RedirectResponse("/login", status_code=303)


async def _issue_session_cookie(request: Request, response, user_id) -> None:
    """Create a session row and attach the cookie. Shared by every login path."""
    pool = await get_pool()
    token = await create_session(pool, user_id)
    params = get_cookie_params()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        **params,
    )
    _set_csrf_cookie(response, generate_csrf_token())


# =============================================================================
# Invitation redemption
# =============================================================================


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invitation_page(request: Request, token: str):
    """Render the acceptance form for an invitation link."""
    pool = await get_pool()
    invitation = await identity_service.resolve_invitation(pool, token)

    if invitation is None:
        csrf_token = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "invite_invalid.html",
            {"csrf_token": csrf_token, "csrf_field_name": CSRF_FIELD_NAME},
            status_code=410,
        )
        _set_csrf_cookie(response, csrf_token)
        return response

    org = await OrganizationRepository(pool).get_by_id(invitation["organization_id"])
    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "invite_accept.html",
        {
            "csrf_token": csrf_token,
            "csrf_field_name": CSRF_FIELD_NAME,
            "token": token,
            "invitation": invitation,
            "organization": org,
            "error": request.query_params.get("error"),
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/invite/{token}")
async def invitation_submit(
    request: Request,
    token: str,
    display_name: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    """Create the account the invitation names and sign the person in."""
    from lucent.rate_limit import get_client_ip, get_login_limiter

    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))

    pool = await get_pool()
    client_ip = get_client_ip(request)
    allowed, retry_after = get_login_limiter().check(f"invite:{client_ip}")
    if not allowed:
        logger.warning("Invitation redemption rate limit exceeded for IP %s", client_ip)
        return RedirectResponse(
            f"/invite/{token}?error="
            + quote(f"Too many attempts. Try again in {retry_after} seconds."),
            status_code=303,
        )

    invitation = await identity_service.resolve_invitation(pool, token)
    if invitation is None:
        return RedirectResponse(
            "/login?notice=" + quote(LOGIN_MESSAGES["expired"]), status_code=303
        )

    if password and password != confirm_password:
        return RedirectResponse(
            f"/invite/{token}?error=" + quote("Passwords do not match."), status_code=303
        )
    if password:
        complexity_error = validate_password_complexity(password)
        if complexity_error:
            return RedirectResponse(
                f"/invite/{token}?error=" + quote(complexity_error), status_code=303
            )

    try:
        user = await identity_service.redeem_invitation(
            pool,
            token=token,
            display_name=(display_name or "").strip() or None,
            password=password or None,
        )
    except identity_service.InvitationError as exc:
        return RedirectResponse(f"/invite/{token}?error=" + quote(str(exc)), status_code=303)

    await AdminAuditRepository(pool).log(
        organization_id=invitation["organization_id"],
        action=audit_actions.INVITATION_ACCEPT,
        entity_type="user",
        entity_id=user["id"],
        entity_label=user.get("email") or user.get("display_name"),
        actor_user_id=user["id"],
        context={"invitation_id": str(invitation["id"]), "ip": client_ip},
        notes="invitation accepted; account created" if password else "invitation accepted",
    )

    # If the account already existed (and had no password), a password typed on
    # this form is the person's own choice and should be adopted so they are not
    # locked to SSO-only access by accident.
    if password and not await UserRepository(pool).get_password_hash(user["id"]):
        await set_user_password(pool, user["id"], password)

    response = RedirectResponse("/", status_code=303)
    await _issue_session_cookie(request, response, user["id"])
    return response


# =============================================================================
# Self-serve signup (creates a new tenant)
# =============================================================================


def signup_enabled() -> bool:
    """Whether self-serve tenant creation is offered on this instance.

    Default off. An instance whose operator distributes access through the
    hyperadmin console should not advertise a public signup form, and turning
    it on is an explicit decision with billing and abuse consequences.
    """
    import os

    return os.environ.get("LUCENT_SELF_SIGNUP_ENABLED", "false").strip().lower() in (
        "true",
        "1",
        "yes",
    )


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Render the self-serve signup form."""
    if not signup_enabled():
        return RedirectResponse("/login", status_code=303)

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        "signup.html",
        {
            "csrf_token": csrf_token,
            "csrf_field_name": CSRF_FIELD_NAME,
            "error": request.query_params.get("error"),
            "organization_name": request.query_params.get("organization_name", ""),
            "display_name": request.query_params.get("display_name", ""),
            "email": request.query_params.get("email", ""),
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/signup")
async def signup_submit(
    request: Request,
    organization_name: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
):
    """Create a tenant and its owner in one step, then sign them in.

    The account is created with the *user's own* password, so there is no
    temporary-credential handoff and no forced reset. This is the flow the
    hyperadmin provisioning endpoint cannot offer because the operator, not the
    user, chooses the credential.
    """
    from lucent.rate_limit import get_client_ip, get_login_limiter

    if not signup_enabled():
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    await _check_csrf(request, form_token=str(form.get(CSRF_FIELD_NAME, "")))

    pool = await get_pool()
    client_ip = get_client_ip(request)
    allowed, retry_after = get_login_limiter().check(f"signup:{client_ip}")
    if not allowed:
        logger.warning("Signup rate limit exceeded for IP %s", client_ip)
        return RedirectResponse(
            "/signup?error="
            + quote(f"Too many attempts. Try again in {retry_after} seconds."),
            status_code=303,
        )

    def _fail(message: str) -> RedirectResponse:
        params = (
            f"?error={quote(message)}"
            f"&organization_name={quote(organization_name or '')}"
            f"&display_name={quote(display_name or '')}"
            f"&email={quote(email or '')}"
        )
        return RedirectResponse("/signup" + params, status_code=303)

    cleaned_org = (organization_name or "").strip()
    normalized_email = (email or "").strip().lower()

    if not cleaned_org or len(cleaned_org) > 255:
        return _fail("A workspace name is required (max 255 characters).")
    if "@" not in normalized_email:
        return _fail("A valid email address is required.")
    if password != confirm_password:
        return _fail("Passwords do not match.")
    complexity_error = validate_password_complexity(password)
    if complexity_error:
        return _fail(complexity_error)

    org_repo = OrganizationRepository(pool)
    existing_name = await org_repo.get_by_name(cleaned_org)
    if existing_name is not None:
        return _fail("That workspace name is already taken. Choose another.")

    from lucent.services import org_provisioning

    try:
        result = await org_provisioning.provision_organization(
            pool,
            name=cleaned_org,
            admin_email=normalized_email,
            admin_display_name=(display_name or "").strip() or normalized_email,
            # The user's own password: no temporary credential is generated,
            # so `temporary_password` comes back None and no reset is forced.
            admin_password=password,
            admin_role="owner",
            # Distinguishes a customer signing themselves up from an operator
            # creating an account for them, and refuses to claim the address is
            # verified: the person typed it, nothing was delivered to it.
            admin_provisioned_by="signup",
            admin_email_verified=False,
            # No API key by default. Signing up is not the same as asking for
            # programmatic access, and a key handed out at signup is a
            # long-lived credential nobody asked for.
            api_key_name=None,
        )
    except org_provisioning.OrganizationNameTakenError:
        return _fail("That workspace name is already taken. Choose another.")
    except org_provisioning.InvalidProvisioningRequestError as exc:
        return _fail(str(exc))
    except org_provisioning.ProvisioningError:
        logger.exception("Signup failed for org %r", cleaned_org)
        return _fail("Could not create the workspace. Please try again.")

    user = result.user
    # A self-service signup proves control of the mailbox only insofar as the
    # person typed it; nothing was delivered to it. Mark unverified so SSO
    # email-linking cannot later adopt this account without going through the
    # verified-link path.
    await AdminAuditRepository(pool).log(
        organization_id=result.organization["id"],
        action=audit_actions.SIGNUP_COMPLETE,
        entity_type="user",
        entity_id=user["id"],
        entity_label=normalized_email,
        actor_user_id=user["id"],
        context={"ip": client_ip},
        notes="self-serve signup: tenant and owner created with a user-chosen password",
    )

    logger.info(
        "Self-serve signup completed: org=%s user=%s", result.organization["id"], user["id"]
    )
    response = RedirectResponse("/", status_code=303)
    await _issue_session_cookie(request, response, user["id"])
    return response


# =============================================================================
# SSO — start
# =============================================================================


async def _resolve_connection(pool, slug: str) -> dict | None:
    repo = SsoConnectionRepository(pool)
    connection = await repo.get_by_slug(slug)
    if connection is None:
        return None
    org = await OrganizationRepository(pool).get_by_id(connection["organization_id"])
    if org is None or org.get("status") != "active":
        return None
    return connection


@router.get("/sso/{slug}/start")
async def sso_start(request: Request, slug: str, next: str | None = None):
    """Begin a login: mint state, store it, and redirect to the IdP."""
    pool = await get_pool()
    connection = await _resolve_connection(pool, slug)
    if connection is None:
        logger.info("SSO start for unknown or disabled connection slug %r", slug)
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    base_url = str(request.base_url)
    return_to = sso_config.safe_return_to(next)
    state_repo = SsoLoginStateRepository(pool)

    if connection["provider_type"] == "saml":
        acs_url = f"{base_url.rstrip('/')}/sso/callback/saml"
        try:
            request_data = saml.build_authn_request(connection, acs_url=acs_url)
        except saml.SamlError as exc:
            logger.error("Could not build SAML AuthnRequest for %s: %s", slug, exc)
            return _login_redirect(LOGIN_MESSAGES["sso_failed"])

        await state_repo.create(
            state_hash=_hash_state(request_data.relay_state),
            connection_id=connection["id"],
            organization_id=connection["organization_id"],
            expires_at=_state_expiry(),
            saml_request_id=request_data.request_id,
            return_to=return_to,
        )
        return RedirectResponse(request_data.url, status_code=303)

    try:
        auth_request = await oidc.build_authorization_request(
            connection, base_url=base_url, return_to=return_to
        )
    except oidc.OidcError as exc:
        logger.error("Could not start OIDC login for %s: %s", slug, exc)
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    await state_repo.create(
        state_hash=_hash_state(auth_request.state),
        connection_id=connection["id"],
        organization_id=connection["organization_id"],
        expires_at=_state_expiry(),
        code_verifier=auth_request.code_verifier,
        nonce=auth_request.nonce,
        return_to=return_to,
    )
    return RedirectResponse(auth_request.url, status_code=303)


def _hash_state(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_expiry():
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(seconds=oidc.LOGIN_STATE_TTL_SECONDS)


# =============================================================================
# SSO — callbacks
# =============================================================================


async def _complete_sso_login(
    request: Request,
    *,
    connection: dict,
    identity,
    return_to: str | None,
):
    """Shared tail of both callbacks: admit the identity and mint a session."""
    from lucent.identity.provisioning import resolve_federated_user

    pool = await get_pool()

    if not sso_config.email_domain_allowed(
        identity.email, connection.get("allowed_email_domains") or []
    ):
        logger.warning(
            "SSO login rejected: email domain not allowed for connection %s",
            connection["id"],
        )
        return _login_redirect(LOGIN_MESSAGES["sso_denied"])

    try:
        user = await resolve_federated_user(
            pool, connection=connection, identity=identity
        )
    except FederatedIdentityError as exc:
        logger.warning(
            "SSO login rejected for connection %s: %s", connection["id"], exc.reason
        )
        return _login_redirect(LOGIN_MESSAGES["sso_denied"])

    await UserRepository(pool).update_last_login(user["id"])
    await AdminAuditRepository(pool).log(
        organization_id=connection["organization_id"],
        action=audit_actions.SSO_LOGIN,
        entity_type="user",
        entity_id=user["id"],
        entity_label=user.get("email") or user.get("display_name"),
        actor_user_id=user["id"],
        context={
            "connection_id": str(connection["id"]),
            "provider_type": connection["provider_type"],
            "subject": identity.subject,
        },
        notes="signed in through an external identity provider",
    )

    response = RedirectResponse(sso_config.safe_return_to(return_to), status_code=303)
    await _issue_session_cookie(request, response, user["id"])
    return response


@router.get("/sso/callback/oidc")
async def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Complete an OIDC (or GitHub) login."""
    if error:
        logger.info("IdP returned an error on OIDC callback: %s", error)
        return _login_redirect(LOGIN_MESSAGES["sso_denied"])
    if not code or not state:
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    pool = await get_pool()
    state_repo = SsoLoginStateRepository(pool)
    stored = await state_repo.consume(_hash_state(state))
    if stored is None:
        logger.warning("OIDC callback with unknown, expired, or replayed state")
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    connection = await SsoConnectionRepository(pool).get(
        stored["connection_id"], organization_id=stored["organization_id"]
    )
    if connection is None or not connection.get("is_enabled"):
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    client_secret = await sso_config.get_client_secret(
        pool,
        connection_id=connection["id"],
        organization_id=connection["organization_id"],
    )

    try:
        identity = await oidc.complete_login(
            connection,
            client_secret=client_secret,
            code=code,
            code_verifier=stored["code_verifier"] or "",
            nonce=stored["nonce"],
            base_url=str(request.base_url),
        )
    except oidc.OidcError as exc:
        logger.warning("OIDC login failed for connection %s: %s", connection["id"], exc)
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    return await _complete_sso_login(
        request,
        connection=connection,
        identity=identity,
        return_to=stored.get("return_to"),
    )


@router.post("/sso/callback/saml")
async def saml_callback(
    request: Request,
    saml_response: str = Form("", alias="SAMLResponse"),
    relay_state: str = Form("", alias="RelayState"),
):
    """Complete a SAML login from the IdP's POST.

    The wire field names (``SAMLResponse``, ``RelayState``) are fixed by the
    SAML POST binding, so they are bound through aliases rather than being used
    as Python identifiers.
    """
    from lucent.db.identity import SamlReplayRepository

    if not saml_response or not relay_state:
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    pool = await get_pool()
    state_repo = SsoLoginStateRepository(pool)
    stored = await state_repo.consume(_hash_state(relay_state))
    if stored is None:
        logger.warning("SAML callback with unknown, expired, or replayed RelayState")
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    connection = await SsoConnectionRepository(pool).get(
        stored["connection_id"], organization_id=stored["organization_id"]
    )
    if connection is None or not connection.get("is_enabled"):
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    acs_url = f"{str(request.base_url).rstrip('/')}/sso/callback/saml"
    replay_repo = SamlReplayRepository(pool)

    # `parse_response` is synchronous and does CPU-bound XML work including
    # signature verification, so it runs in a worker thread — a large assertion
    # must not stall the event loop.
    import asyncio

    def _validate():
        return saml.parse_response(
            saml_response_b64=saml_response,
            connection=connection,
            acs_url=acs_url,
            expected_request_id=stored["saml_request_id"],
        )

    try:
        validated = await asyncio.to_thread(_validate)
    except saml.SamlError as exc:
        logger.warning("SAML login failed for connection %s: %s", connection["id"], exc)
        return _login_redirect(LOGIN_MESSAGES["sso_failed"])

    # The replay claim happens only after the assertion is fully verified, so an
    # attacker cannot burn (or poison) assertion IDs with forged documents.
    claimed = await replay_repo.claim(
        assertion_id=validated.assertion_id,
        connection_id=connection["id"],
        expires_at=validated.expires_at,
    )
    if not claimed:
        logger.warning(
            "SAML assertion replay rejected for connection %s", connection["id"]
        )
        return _login_redirect(LOGIN_MESSAGES["sso_denied"])

    return await _complete_sso_login(
        request,
        connection=connection,
        identity=validated.identity,
        return_to=stored.get("return_to"),
    )
