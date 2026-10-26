"""Standalone local ops console — a small web app for administering an instance.

Run it with ``lucent-ops-console`` (or ``python -m ops_console.app``). It is a
*client* of the Lucent hyperadmin API: no database driver, no Lucent imports, no
shared schema. Everything it can do, the eventual hosted signup site can do too,
which is exactly why it exists as a separate service.

Design decisions worth preserving:

* **Two secrets, two purposes.** ``LUCENT_OPS_API_KEY`` is the console's identity
  towards Lucent and never leaves the server side of this process.
  ``LUCENT_OPS_CONSOLE_TOKEN`` is the human passphrase for the console UI. The
  API key is never rendered into a template or a cookie.
* **One-time secrets.** Generated passwords and API keys are held in a small
  in-memory store with a short TTL and handed to the browser exactly once, by
  opaque reference. They are never written to disk or to a log.
* **Loopback by default.** The console binds to 127.0.0.1 and refuses to start
  without both secrets, so an accidental ``docker compose up`` elsewhere cannot
  expose an unauthenticated operator tool.
* **No hidden state.** Every page reflects what the API returned. If Lucent
  says no (403 for a non-operator key, 404 for a missing tenant, 400 for the
  last owner), the console shows that message verbatim.
"""

from __future__ import annotations

import logging
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ops_console.client import LucentAPIError, LucentClient
from ops_console.config import ConfigError, ConsoleConfig, load_config
from ops_console.session import (
    CSRF_COOKIE,
    CSRF_FIELD,
    SESSION_COOKIE,
    ConsoleAuth,
    cookie_params,
    issue_session,
    new_csrf_token,
)

logger = logging.getLogger("lucent.ops_console")

TEMPLATES_DIR = Path(__file__).parent / "templates"

#: One-time credentials waiting to be displayed. Keyed by an opaque reference
#: stored in a short-lived cookie, mirroring how the server hands back a
#: temporary password after an admin reset.
_SECRET_TTL_SECONDS = 120
_secret_store: dict[str, dict[str, Any]] = {}
_SECRET_COOKIE = "lucent_ops_secret_ref"


def _cleanup_secrets() -> None:
    now = time.time()
    for key in [k for k, v in _secret_store.items() if v["expires"] < now]:
        _secret_store.pop(key, None)


def _stash_secret(payload: dict[str, Any]) -> str:
    _cleanup_secrets()
    if len(_secret_store) >= 50:
        _secret_store.clear()
    ref = secrets.token_urlsafe(24)
    _secret_store[ref] = {"payload": payload, "expires": time.time() + _SECRET_TTL_SECONDS}
    return ref


def _take_secret(ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    entry = _secret_store.pop(ref, None)
    if entry is None or entry["expires"] < time.time():
        return None
    return entry["payload"]


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _shortdate(value: Any) -> str:
    """Render an ISO timestamp compactly; ``—`` when absent."""
    if not value:
        return "—"
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text[:16].replace("T", " ")
    return parsed.strftime("%Y-%m-%d %H:%M")


def _static_dir() -> Path | None:
    """Locate Lucent's static assets (logo, Tailwind build) for the console UI.

    The console shares Lucent's design assets but not its code. In a source
    checkout they sit next to this package; in a wheel they are inside the
    installed ``lucent`` package. Both are checked without importing Lucent.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "src" / "lucent" / "web" / "static",
        Path(__file__).resolve().parent.parent / "lucent" / "web" / "static",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    try:
        import importlib.util

        spec = importlib.util.find_spec("lucent")
        if spec and spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                candidate = Path(location) / "web" / "static"
                if candidate.is_dir():
                    return candidate
    except Exception:  # noqa: BLE001 — cosmetic assets are never worth failing over
        return None
    return None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: ConsoleConfig | None = None) -> FastAPI:
    """Build the console app. Exposed for tests and for ``main()``."""
    config = config or load_config()
    auth = ConsoleAuth(config)

    app = FastAPI(
        title="Lucent ops console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["shortdate"] = _shortdate

    static_dir = _static_dir()
    if static_dir is not None:
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def client() -> LucentClient:
        return LucentClient(config.api_root, config.api_key)

    def render(
        request: Request,
        template: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        """Render a page, ensuring a CSRF token exists for its forms."""
        token = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
        body = {
            "request": request,
            "csrf_token": token,
            CSRF_FIELD: token,
            "instance_label": config.instance_label,
            "base_url": config.api_root,
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
            **context,
        }
        response = templates.TemplateResponse(request, template, body, status_code=status_code)
        if request.cookies.get(CSRF_COOKIE) != token:
            params = cookie_params(secure=config.secure_cookies)
            params.pop("httponly")  # must be readable by inline copy helpers
            response.set_cookie(CSRF_COOKIE, token, **params)
        return response

    def back(
        path: str, *, error: str | None = None, success: str | None = None
    ) -> RedirectResponse:
        query = {}
        if error:
            query["error"] = error
        if success:
            query["success"] = success
        target = path + (f"?{urlencode(query)}" if query else "")
        return RedirectResponse(target, status_code=303)

    def fail(exc: LucentAPIError, path: str) -> RedirectResponse:
        """Surface an API refusal as a flash message on the originating page."""
        logger.info("API refused %s: %s %s", path, exc.status_code, exc.detail)
        return back(path, error=exc.detail)

    # -- auth -------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if auth.is_authenticated(request):
            return RedirectResponse("/", status_code=303)
        token = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "error": request.query_params.get("error"),
                "csrf_token": token,
            },
        )
        if request.cookies.get(CSRF_COOKIE) != token:
            params = cookie_params(secure=config.secure_cookies)
            params.pop("httponly")
            response.set_cookie(CSRF_COOKIE, token, **params)
        return response

    @app.post("/login")
    async def login_submit(
        request: Request, token: str = Form(...), csrf_token: str = Form(default="")
    ):
        auth.require_csrf(request, csrf_token)
        if not secrets.compare_digest(token.strip(), config.console_token):
            logger.warning("Console login rejected: bad passphrase from %s", _peer(request))
            return back("/login", error="Incorrect passphrase.")

        session_value = issue_session(config.signing_secret, hours=config.session_hours)
        response = RedirectResponse("/", status_code=303)
        params = cookie_params(secure=config.secure_cookies)
        response.set_cookie(
            SESSION_COOKIE,
            session_value,
            max_age=config.session_hours * 3600,
            **params,
        )
        logger.info("Console login from %s", _peer(request))
        return response

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form(default="")):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- organization list ------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def organizations_index(
        request: Request,
        page: int = Query(default=1, ge=1),
        search: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ):
        auth.require(request)
        per_page = 25
        try:
            payload = await client().list_organizations(
                offset=(page - 1) * per_page,
                limit=per_page,
                status=status or None,
                search=search or None,
            )
        except LucentAPIError as exc:
            return render(
                request,
                "organizations.html",
                {
                    "organizations": [],
                    "total_count": 0,
                    "page": 1,
                    "total_pages": 1,
                    "search": search,
                    "status": status,
                    "error": exc.detail,
                    "page_url": lambda p: "/",
                },
            )

        total = int(payload.get("total_count") or 0)
        total_pages = max(1, (total + per_page - 1) // per_page)

        def page_url(target: int) -> str:
            query: dict[str, Any] = {"page": target}
            if search:
                query["search"] = search
            if status:
                query["status"] = status
            return "/?" + urlencode(query)

        return render(
            request,
            "organizations.html",
            {
                "organizations": payload.get("organizations") or [],
                "total_count": total,
                "page": page,
                "total_pages": total_pages,
                "search": search,
                "status": status,
                "page_url": page_url,
            },
        )

    # -- create -----------------------------------------------------------

    @app.get("/organizations/new", response_class=HTMLResponse)
    async def organization_new_page(request: Request):
        auth.require(request)
        models: list[dict[str, Any]] = []
        model_warning = None
        try:
            models = await client().available_models()
        except LucentAPIError as exc:
            model_warning = f"Model list unavailable: {exc.detail}"
        return render(
            request,
            "organization_new.html",
            {"models": models, "form": {}, "model_warning": model_warning},
        )

    @app.post("/organizations/new")
    async def organization_new_submit(
        request: Request,
        name: str = Form(...),
        admin_email: str = Form(default=""),
        admin_display_name: str = Form(default=""),
        admin_role: str = Form(default="owner"),
        admin_password: str = Form(default=""),
        api_key_name: str = Form(default="Default API key"),
        model_id: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)

        cleaned_name = name.strip()
        form_state = {
            "name": cleaned_name,
            "admin_email": admin_email,
            "admin_display_name": admin_display_name,
            "admin_role": admin_role,
            "api_key_name": api_key_name,
            "model_id": model_id,
        }

        if not cleaned_name:
            return render(
                request,
                "organization_new.html",
                {"models": [], "form": form_state, "error": "Organization name is required."},
                status_code=400,
            )

        try:
            result = await client().provision_organization(
                name=cleaned_name,
                admin_email=admin_email.strip() or None,
                admin_display_name=admin_display_name.strip() or None,
                admin_password=admin_password or None,
                admin_role=admin_role if admin_role in ("owner", "admin") else "owner",
                api_key_name=api_key_name.strip() or None,
                model_id=model_id or None,
            )
        except LucentAPIError as exc:
            models: list[dict[str, Any]] = []
            try:
                models = await client().available_models()
            except LucentAPIError:
                pass
            return render(
                request,
                "organization_new.html",
                {"models": models, "form": form_state, "error": exc.detail},
                status_code=400,
            )

        # Credentials are shown once, on the next page load, then dropped.
        ref = _stash_secret(result)
        query: dict[str, Any] = {"created": "1"}
        if ref:
            query["ref"] = ref
        response = RedirectResponse(
            f"/organizations/{result['organization']['id']}/created?{urlencode(query)}",
            status_code=303,
        )
        if ref:
            params = cookie_params(secure=config.secure_cookies)
            response.set_cookie(_SECRET_COOKIE, ref, max_age=_SECRET_TTL_SECONDS, **params)
        return response

    # -- created (one-time credentials) ------------------------------------

    @app.get("/organizations/{organization_id}/created", response_class=HTMLResponse)
    async def organization_created(
        request: Request,
        organization_id: str,
        ref: str | None = Query(default=None),
    ):
        auth.require(request)
        payload = _take_secret(ref or request.cookies.get(_SECRET_COOKIE))
        if payload is None:
            return back(
                f"/organizations/{organization_id}",
                error=(
                    "Those credentials are no longer available — they are shown only once. "
                    "Reset the member's password or mint a new key from the organization page."
                ),
            )
        return render(request, "organization_created.html", {"result": payload})

    # -- detail -----------------------------------------------------------

    @app.get("/organizations/{organization_id}", response_class=HTMLResponse)
    async def organization_detail(request: Request, organization_id: str):
        auth.require(request)
        api = client()

        try:
            organization = await api.get_organization(organization_id, include_stats=True)
        except LucentAPIError as exc:
            return render(
                request,
                "organizations.html",
                {
                    "organizations": [],
                    "total_count": 0,
                    "page": 1,
                    "total_pages": 1,
                    "error": exc.detail,
                    "page_url": lambda p: "/",
                },
                status_code=exc.status_code or 500,
            )

        users: list[dict[str, Any]] = []
        settings_sections: dict[str, list[dict[str, Any]]] = {}
        audit_entries: list[dict[str, Any]] = []
        warnings: list[str] = []

        try:
            users = await api.list_users(organization_id)
        except LucentAPIError as exc:
            warnings.append(f"Members unavailable: {exc.detail}")
        try:
            settings_payload = await api.get_settings(organization_id)
            settings_sections = settings_payload.get("sections") or {}
        except LucentAPIError as exc:
            warnings.append(f"Settings unavailable: {exc.detail}")
        try:
            audit_payload = await api.organization_audit(organization_id, limit=15)
            audit_entries = audit_payload.get("entries") or []
        except LucentAPIError as exc:
            warnings.append(f"Audit unavailable: {exc.detail}")

        return render(
            request,
            "organization_detail.html",
            {
                "organization": organization,
                "users": users,
                "settings_sections": settings_sections,
                "audit": audit_entries,
                "warnings": warnings,
            },
        )

    # -- lifecycle --------------------------------------------------------

    @app.post("/organizations/{organization_id}/suspend")
    async def organization_suspend(
        request: Request, organization_id: str, csrf_token: str = Form(default="")
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            await client().suspend_organization(organization_id)
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        return back(
            f"/organizations/{organization_id}",
            success="Organization suspended. Members are signed out and API keys are rejected.",
        )

    @app.post("/organizations/{organization_id}/resume")
    async def organization_resume(
        request: Request, organization_id: str, csrf_token: str = Form(default="")
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            await client().resume_organization(organization_id)
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        return back(f"/organizations/{organization_id}", success="Organization resumed.")

    @app.post("/organizations/{organization_id}/rename")
    async def organization_rename(
        request: Request,
        organization_id: str,
        name: str = Form(...),
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            await client().rename_organization(organization_id, name.strip())
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        return back(f"/organizations/{organization_id}", success="Organization renamed.")

    @app.post("/organizations/{organization_id}/delete")
    async def organization_delete(
        request: Request,
        organization_id: str,
        confirm_name: str = Form(...),
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            result = await client().delete_organization(organization_id, confirm_name.strip())
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        message = (result or {}).get("message") or "Organization deleted."
        return back("/", success=message)

    # -- members ----------------------------------------------------------

    @app.post("/organizations/{organization_id}/users")
    async def organization_user_create(
        request: Request,
        organization_id: str,
        role: str = Form(default="owner"),
        email: str = Form(default=""),
        display_name: str = Form(default=""),
        mint_api_key: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            created = await client().create_user(
                organization_id,
                email=email.strip() or None,
                display_name=display_name.strip() or None,
                role=role if role in ("member", "admin", "owner") else "owner",
                mint_api_key=bool(mint_api_key),
            )
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")

        bits = []
        if created.get("temporary_password"):
            bits.append(f"temporary password {created['temporary_password']}")
        if created.get("api_key"):
            bits.append(f"API key {created['api_key']}")
        who = (created.get("user") or {}).get("email") or "member"
        if bits:
            return back(
                f"/organizations/{organization_id}",
                success=f"Added {who}. Shown once — " + "; ".join(bits),
            )
        return back(f"/organizations/{organization_id}", success=f"Added {who}.")

    @app.post("/organizations/{organization_id}/users/{user_id}/role")
    async def organization_user_role(
        request: Request,
        organization_id: str,
        user_id: str,
        role: str = Form(...),
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            await client().set_user_role(organization_id, user_id, role)
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        return back(
            f"/organizations/{organization_id}", success=f"Role updated to {role}."
        )

    @app.post("/organizations/{organization_id}/users/{user_id}/reset-password")
    async def organization_user_reset_password(
        request: Request,
        organization_id: str,
        user_id: str,
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            result = await client().reset_user_password(organization_id, user_id)
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        message = (result or {}).get("message") or "Password reset."
        return back(f"/organizations/{organization_id}", success=message)

    @app.post("/organizations/{organization_id}/users/{user_id}/api-key")
    async def organization_user_api_key(
        request: Request,
        organization_id: str,
        user_id: str,
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            result = await client().mint_api_key(organization_id, user_id)
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        key = (result or {}).get("api_key")
        return back(
            f"/organizations/{organization_id}",
            success=(
                f"New API key (shown once): {key}"
                if key
                else "API key minted, but the server did not return it."
            ),
        )

    @app.post("/organizations/{organization_id}/users/{user_id}/delete")
    async def organization_user_delete(
        request: Request,
        organization_id: str,
        user_id: str,
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)
        try:
            await client().remove_user(organization_id, user_id)
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        return back(f"/organizations/{organization_id}", success="Member removed.")

    # -- settings ---------------------------------------------------------

    @app.post("/organizations/{organization_id}/settings/{setting_key:path}")
    async def organization_setting_update(
        request: Request,
        organization_id: str,
        setting_key: str,
        value: str = Form(default=""),
        reset: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        auth.require(request)
        auth.require_csrf(request, csrf_token)

        api = client()
        try:
            if reset:
                result = await api.reset_setting(organization_id, setting_key)
                message = f"{result.get('title') or setting_key} reset to the instance value."
            else:
                result = await api.set_setting(organization_id, setting_key, value)
                message = f"{result.get('title') or setting_key} updated."
        except LucentAPIError as exc:
            return fail(exc, f"/organizations/{organization_id}")
        return back(f"/organizations/{organization_id}", success=message)

    # -- instance audit ---------------------------------------------------

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_index(
        request: Request,
        page: int = Query(default=1, ge=1),
        organization_id: str | None = Query(default=None),
        action: str | None = Query(default=None),
    ):
        auth.require(request)
        per_page = 50
        try:
            payload = await client().instance_audit(
                organization_id=organization_id or None,
                action=action or None,
                offset=(page - 1) * per_page,
                limit=per_page,
            )
        except LucentAPIError as exc:
            return render(
                request,
                "audit.html",
                {
                    "entries": [],
                    "total_count": 0,
                    "page": 1,
                    "total_pages": 1,
                    "organization_id": organization_id,
                    "action": action,
                    "error": exc.detail,
                    "page_url": lambda p: "/audit",
                },
            )

        total = int(payload.get("total_count") or 0)
        total_pages = max(1, (total + per_page - 1) // per_page)

        def page_url(target: int) -> str:
            query: dict[str, Any] = {"page": target}
            if organization_id:
                query["organization_id"] = organization_id
            if action:
                query["action"] = action
            return "/audit?" + urlencode(query)

        return render(
            request,
            "audit.html",
            {
                "entries": payload.get("entries") or [],
                "total_count": total,
                "page": page,
                "total_pages": total_pages,
                "organization_id": organization_id,
                "action": action,
                "page_url": page_url,
            },
        )

    # -- status -----------------------------------------------------------

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        auth.require(request)
        api = client()

        health_ok = False
        key_ok = False
        key_error = None
        identity = None
        org_api_ok = False

        try:
            await api.health()
            health_ok = True
        except LucentAPIError as exc:
            key_error = f"Server unreachable: {exc.detail}"

        if health_ok:
            try:
                await api.verify_key()
                key_ok = True
            except LucentAPIError as exc:
                key_error = exc.detail
            try:
                identity = await api.me()
            except LucentAPIError:
                identity = None
            try:
                # The organization API is always mounted; reaching it depends on
                # the key's role. A 404 here means the server predates that (an
                # older build that gated the router), so distinguish "not
                # registered" from "refused" rather than assuming a mode.
                await api.list_organizations(limit=1, include_stats=False)
                org_api_ok = True
            except LucentAPIError as exc:
                org_api_ok = exc.status_code != 404

        warning = None
        if health_ok and not org_api_ok:
            warning = (
                "The organization API is not registered on this server. Upgrade Lucent: "
                "the router is no longer gated by LUCENT_MODE and is always mounted."
            )
        elif identity and identity.get("role") != "hyperadmin":
            warning = (
                f"This API key belongs to a '{identity.get('role')}' account, not a "
                "hyperadmin. Instance endpoints will refuse it with 403."
            )

        return render(
            request,
            "status.html",
            {
                "health_ok": health_ok,
                "key_ok": key_ok,
                "key_error": key_error,
                "identity": identity,
                "org_api_ok": org_api_ok,
                "mode_warning": warning,
            },
        )

    return app


def _peer(request: Request) -> str:
    """Client address for the login log line."""
    client = getattr(request, "client", None)
    return getattr(client, "host", "unknown") or "unknown"


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``lucent-ops-console``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ops console cannot start: {exc}", file=sys.stderr)
        return 1

    app = create_app(config)
    print(
        f"Lucent ops console listening on http://{config.host}:{config.port} "
        f"(API: {config.api_root})",
        file=sys.stdout,
    )
    if config.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "Binding to %s exposes the ops console beyond loopback. It is meant for "
            "local administration only.",
            config.host,
        )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
