"""Sandbox management routes."""

from math import ceil

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from lucent.auth_providers import CSRF_COOKIE_NAME
from lucent.db import get_pool
from lucent.logging import get_logger
from lucent.secrets import SecretRegistry, resolve_env_vars

from ._shared import _check_csrf, _parse_env_vars, get_user_context, templates

logger = get_logger("web.routes.sandboxes")

router = APIRouter()

ALLOWED_PER_PAGE = {10, 25, 50, 100}


def _require_admin_or_owner(user) -> None:
    """Raise 403 if user is not admin or owner."""
    role_value = user.role if isinstance(user.role, str) else user.role.value
    if role_value not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Permission denied")


def _require_org_membership(user) -> None:
    """Raise 403 if user has no organization membership."""
    if not user.organization_id:
        raise HTTPException(status_code=403, detail="Organization membership required")


# =============================================================================
# Sandboxes
# =============================================================================


@router.get("/sandboxes", response_class=HTMLResponse)
async def sandboxes_page(
    request: Request,
    tab: str | None = Query(default=None),
    show: str | None = Query(default=None),
    page: int = 1,
    per_page: int = 25,
):
    """Sandbox templates and instances."""
    user = await get_user_context(request)
    org_id = str(user.organization_id) if user.organization_id else None

    page = max(1, page)
    per_page = per_page if per_page in ALLOWED_PER_PAGE else 25
    offset = (page - 1) * per_page
    active_tab = tab or "templates"

    # Always load templates (needed for both tabs and launch modal)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    tpl_repo = SandboxTemplateRepository(pool)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    total_count = 0
    try:
        if org_id:
            if active_tab == "templates":
                tpl_result = await tpl_repo.list_accessible_by(
                    str(user.id),
                    org_id,
                    limit=per_page,
                    offset=offset,
                    user_role=role_value,
                )
                template_list = tpl_result["items"]
                total_count = tpl_result["total_count"]
            else:
                # For instances tab, load all templates (for launch modal + name enrichment)
                tpl_result = await tpl_repo.list_accessible_by(
                    str(user.id),
                    org_id,
                    limit=1000,
                    offset=0,
                    user_role=role_value,
                )
                template_list = tpl_result["items"]
        else:
            template_list = []
    except Exception:
        logger.debug("Failed to load sandbox templates", exc_info=True)
        template_list = []

    # Load instances only when on instances tab
    sandbox_list = []
    if active_tab == "instances":
        from lucent.sandbox.manager import get_sandbox_manager

        manager = get_sandbox_manager()
        try:
            if show == "active":
                sb_result = await manager.list_active(org_id, limit=per_page, offset=offset)
            else:
                sb_result = await manager.list_all(org_id, limit=per_page, offset=offset)
            sandbox_list = sb_result["items"]
            total_count = sb_result["total_count"]
        except Exception:
            logger.debug("Failed to load sandbox list", exc_info=True)
            sandbox_list = []

        # Enrich with template name
        tpl_names = {str(t["id"]): t["name"] for t in template_list}
        for sb in sandbox_list:
            sb["template_name"] = tpl_names.get(str(sb.get("template_id", "")))

    total_pages = ceil(total_count / per_page) if total_count > 0 else 1
    page = min(page, total_pages)

    return templates.TemplateResponse(
        request,
        "sandboxes.html",
        {
            "tab": active_tab,
            "templates": template_list,
            "sandboxes": sandbox_list,
            "show_filter": show or "all",
            "user": user,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "total_count": total_count,
        },
    )


@router.post("/sandboxes/templates/create")
async def create_template_web(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    image: str = Form(default="python:3.12-slim"),
    repo_url: str = Form(default=""),
    branch: str = Form(default="main"),
    setup_commands: str = Form(default=""),
    env_vars: str = Form(default=""),
    memory_limit: str = Form(default="2g"),
    cpu_limit: float = Form(default=2.0),
    disk_limit: str = Form(default="10g"),
    network_mode: str = Form(default="none"),
    timeout_seconds: int = Form(default=1800),
    csrf_token: str = Form(default=""),
):
    """Create a new sandbox template."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request, form_token=csrf_token)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)

    await repo.create(
        name=name.strip(),
        organization_id=str(user.organization_id),
        description=description.strip(),
        image=image,
        repo_url=repo_url.strip() or None,
        branch=branch.strip() or None,
        setup_commands=[c.strip() for c in setup_commands.splitlines() if c.strip()],
        env_vars=_parse_env_vars(env_vars),
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        disk_limit=disk_limit,
        network_mode=network_mode,
        timeout_seconds=timeout_seconds,
        created_by=str(user.id),
    )
    return RedirectResponse("/sandboxes", status_code=303)


@router.get("/sandboxes/templates/{template_id}/edit", response_class=HTMLResponse)
async def edit_template_page(request: Request, template_id: str):
    """Edit a sandbox template."""
    user = await get_user_context(request)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    tpl = await repo.get_accessible(
        template_id,
        str(user.organization_id),
        str(user.id),
        user_role=role_value,
    )
    if not tpl:
        raise HTTPException(404, "Template not found")

    return templates.TemplateResponse(
        request,
        "sandbox_template_edit.html",
        {
            "template": tpl,
            "user": user,
            "csrf_token": request.cookies.get(CSRF_COOKIE_NAME, ""),
        },
    )


@router.post("/sandboxes/templates/{template_id}/edit")
async def update_template_web(
    request: Request,
    template_id: str,
    name: str = Form(...),
    description: str = Form(default=""),
    image: str = Form(default="python:3.12-slim"),
    repo_url: str = Form(default=""),
    branch: str = Form(default="main"),
    setup_commands: str = Form(default=""),
    env_vars: str = Form(default=""),
    memory_limit: str = Form(default="2g"),
    cpu_limit: float = Form(default=2.0),
    disk_limit: str = Form(default="10g"),
    network_mode: str = Form(default="none"),
    timeout_seconds: int = Form(default=1800),
    csrf_token: str = Form(default=""),
):
    """Update a sandbox template."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request, form_token=csrf_token)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)

    await repo.update(
        template_id,
        str(user.organization_id),
        name=name.strip(),
        description=description.strip(),
        image=image,
        repo_url=repo_url.strip() or None,
        branch=branch.strip() or None,
        setup_commands=[c.strip() for c in setup_commands.splitlines() if c.strip()],
        env_vars=_parse_env_vars(env_vars),
        memory_limit=memory_limit,
        cpu_limit=cpu_limit,
        disk_limit=disk_limit,
        network_mode=network_mode,
        timeout_seconds=timeout_seconds,
    )
    return RedirectResponse("/sandboxes", status_code=303)


@router.post("/sandboxes/templates/{template_id}/delete")
async def delete_template_web(request: Request, template_id: str):
    """Delete a sandbox template."""
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    await _check_csrf(request)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = SandboxTemplateRepository(pool)
    await repo.delete(template_id, str(user.organization_id))
    return RedirectResponse("/sandboxes", status_code=303)


@router.post("/sandboxes/launch")
async def launch_sandbox_web(
    request: Request,
    template_id: str = Form(...),
    name: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Launch a sandbox instance from a template."""
    user = await get_user_context(request)
    _require_org_membership(user)
    await _check_csrf(request, form_token=csrf_token)
    pool = await get_pool()
    from lucent.db.sandbox_template import SandboxTemplateRepository
    from lucent.sandbox.manager import get_sandbox_manager
    from lucent.sandbox.models import SandboxConfig

    tpl_repo = SandboxTemplateRepository(pool)
    role_value = user.role if isinstance(user.role, str) else user.role.value
    tpl = await tpl_repo.get_accessible(
        template_id,
        str(user.organization_id),
        str(user.id),
        user_role=role_value,
    )
    if not tpl:
        raise HTTPException(404, "Template not found")

    provider = SecretRegistry.get()
    resolved_env_vars = await resolve_env_vars(tpl.get("env_vars") or {}, provider)
    config = SandboxConfig(
        name=name.strip() or f"{tpl['name']}-instance",
        image=tpl["image"],
        repo_url=tpl.get("repo_url"),
        branch=tpl.get("branch"),
        setup_commands=tpl.get("setup_commands") or [],
        env_vars=resolved_env_vars,
        working_dir=tpl.get("working_dir", "/workspace"),
        memory_limit=tpl.get("memory_limit", "2g"),
        cpu_limit=float(tpl.get("cpu_limit", 2.0)),
        network_mode=tpl.get("network_mode", "none"),
        allowed_hosts=tpl.get("allowed_hosts") or [],
        timeout_seconds=tpl.get("timeout_seconds", 1800),
        organization_id=str(user.organization_id),
    )
    manager = get_sandbox_manager()
    await manager.create(config)
    return RedirectResponse("/sandboxes?tab=instances", status_code=303)


@router.post("/sandboxes/{sandbox_id}/stop")
async def stop_sandbox_web(request: Request, sandbox_id: str):
    """Stop a sandbox from the web UI."""
    user = await get_user_context(request)
    _require_org_membership(user)
    await _check_csrf(request)
    from lucent.sandbox.manager import get_sandbox_manager

    manager = get_sandbox_manager()
    sandbox = await manager.get(sandbox_id)
    if not sandbox or str(sandbox.get("organization_id", "")) != str(user.organization_id):
        raise HTTPException(404, "Sandbox not found")
    await manager.stop(sandbox_id)
    return RedirectResponse("/sandboxes?tab=instances", status_code=303)


@router.post("/sandboxes/{sandbox_id}/destroy")
async def destroy_sandbox_web(request: Request, sandbox_id: str):
    """Destroy a sandbox from the web UI."""
    user = await get_user_context(request)
    _require_org_membership(user)
    await _check_csrf(request)
    from lucent.sandbox.manager import get_sandbox_manager

    manager = get_sandbox_manager()
    sandbox = await manager.get(sandbox_id)
    if not sandbox or str(sandbox.get("organization_id", "")) != str(user.organization_id):
        raise HTTPException(404, "Sandbox not found")
    await manager.destroy(sandbox_id)
    return RedirectResponse("/sandboxes?tab=instances", status_code=303)


@router.post("/sandboxes/{sandbox_id}/exec")
async def exec_sandbox_web(request: Request, sandbox_id: str):
    """Execute a command in a sandbox from the web UI.

    Uses session cookie auth + CSRF validation so the frontend
    never needs to handle bearer tokens.
    """
    user = await get_user_context(request)
    _require_admin_or_owner(user)
    csrf_token = request.headers.get("X-CSRF-Token", "")
    await _check_csrf(request, form_token=csrf_token)

    from lucent.sandbox.manager import get_sandbox_manager

    manager = get_sandbox_manager()
    sandbox = await manager.get(sandbox_id)
    if not sandbox or str(sandbox.get("organization_id", "")) != str(user.organization_id):
        raise HTTPException(404, "Sandbox not found")

    body = await request.json()
    command = body.get("command", "")
    timeout = min(body.get("timeout", 30), 300)

    if not command:
        return JSONResponse({"error": "No command provided"}, status_code=400)

    result = await manager.exec(sandbox_id, command, timeout=timeout)
    return JSONResponse({
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })
