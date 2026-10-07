"""Sandbox API endpoints for managing isolated execution environments."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from lucent.api.deps import AuthenticatedUser
from lucent.db.pool import get_pool
from lucent.logging import get_logger
from lucent.sandbox.manager import get_sandbox_manager
from lucent.sandbox.models import SandboxConfig, SandboxStatus, validate_extra_hosts
from lucent.secrets import SecretRegistry, resolve_env_vars
from lucent.settings import custom_tooling_enabled, sandboxes_enabled

logger = get_logger("api.sandboxes")

router = APIRouter()

# Allowed values for input validation
_MEMORY_RE = re.compile(r"^[1-9]\d{0,4}[mgt]$")
_IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:-]{0,254}$")


def _require_sandboxes_enabled(user: AuthenticatedUser) -> None:
    if not sandboxes_enabled(organization_id=user.organization_id):
        raise HTTPException(
            409,
            "Sandboxes are disabled for this organization by the "
            "features.sandboxes_enabled setting",
        )


# --- Request/Response Models ---


class SandboxCreateRequest(BaseModel):
    """Request body for creating a sandbox."""

    name: str | None = Field(default=None, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    image: str = Field(default="python:3.12-slim", max_length=256)
    repo_url: str | None = Field(default=None, max_length=512)
    branch: str | None = Field(default=None, max_length=128)
    setup_commands: list[str] = Field(default_factory=list, max_length=50)
    env_vars: dict[str, str] = Field(default_factory=dict)
    working_dir: str = "/workspace"
    memory_limit: str = Field(default="2g", pattern=r"^[1-9]\d{0,4}[mgt]$")
    cpu_limit: float = Field(default=2.0, gt=0, le=16)
    disk_limit: str = Field(default="10g", pattern=r"^[1-9]\d{0,4}[mgt]$")
    network_mode: Literal["none", "bridge", "allowlist"] = "none"
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    task_id: str | None = None
    request_id: str | None = None
    reuse_within_request: bool = False
    reuse_key: str | None = Field(default=None, max_length=128)


class SandboxExecRequest(BaseModel):
    """Request body for executing a command."""

    command: str = Field(..., max_length=4096)
    cwd: str | None = Field(default=None, max_length=256)
    env: dict[str, str] | None = None
    timeout: int = Field(default=300, ge=1, le=3600)


class SandboxWriteFileRequest(BaseModel):
    """Request body for writing a file."""

    path: str = Field(..., max_length=512)
    content: str  # UTF-8 text content


class SandboxResponse(BaseModel):
    """Sandbox info response."""

    id: str
    name: str
    status: str
    container_id: str | None = None
    created_at: str | None = None
    ready_at: str | None = None
    error: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    reuse_within_request: bool = False


class ExecResponse(BaseModel):
    """Command execution result."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = 0
    timed_out: bool = False


class FileListResponse(BaseModel):
    """File listing response."""

    files: list[dict[str, Any]]


# --- Helpers ---


async def _get_sandbox_for_user(sandbox_id: str, user: AuthenticatedUser) -> dict:
    """Fetch a sandbox and verify the caller may use it. Raises 404/403.

    "Only yours" (2026-10-01): members may touch only the sandboxes created
    for them; admins/owners manage every org sandbox.

    Note: sandboxes created before owner persistence/backfill may have no
    owner — a member gets 404 on those and can ask an admin/owner.
    """
    manager = get_sandbox_manager()
    info = await manager.get(sandbox_id, str(user.organization_id))
    if not info:
        raise HTTPException(status_code=404, detail="Sandbox not found")
    # Verify organization ownership
    org_id = (
        info.get("organization_id")
        if isinstance(info, dict)
        else getattr(info, "organization_id", None)
    )
    if org_id and str(org_id) != str(user.organization_id):
        raise HTTPException(status_code=404, detail="Sandbox not found")
    created_by = info.get("created_by") if isinstance(info, dict) else None
    if user.role.value not in ("admin", "owner") and (
        not created_by or str(created_by) != str(user.id)
    ):
        raise HTTPException(status_code=404, detail="Sandbox not found")
    return info


def _validate_sandbox_path(path: str) -> str:
    """Validate and normalize a sandbox file path to prevent traversal."""
    import os
    import posixpath

    normalized = posixpath.normpath(path)
    workspace_root = "/workspace"
    candidate = (
        normalized
        if normalized.startswith("/")
        else posixpath.join(workspace_root, normalized)
    )
    resolved_candidate = os.path.realpath(candidate)
    resolved_root = os.path.realpath(workspace_root)
    root_prefix = resolved_root.rstrip("/") + "/"
    if resolved_candidate != resolved_root and not resolved_candidate.startswith(root_prefix):
        raise HTTPException(status_code=400, detail="Invalid path")
    return resolved_candidate


def _to_response(info) -> SandboxResponse:
    """Convert SandboxInfo or dict to SandboxResponse."""
    if isinstance(info, dict):
        return SandboxResponse(
            id=str(info["id"]),
            name=info.get("name", ""),
            status=info.get("status", "unknown"),
            container_id=info.get("container_id"),
            created_at=info["created_at"].isoformat() if info.get("created_at") else None,
            ready_at=info["ready_at"].isoformat() if info.get("ready_at") else None,
            error=info.get("error"),
            task_id=str(info["task_id"]) if info.get("task_id") else None,
            request_id=str(info["request_id"]) if info.get("request_id") else None,
            reuse_within_request=bool((info.get("config") or {}).get("reuse_within_request")),
        )
    return SandboxResponse(
        id=info.id,
        name=info.name,
        status=info.status.value if hasattr(info.status, "value") else str(info.status),
        container_id=info.container_id,
        created_at=info.created_at.isoformat() if info.created_at else None,
        ready_at=info.ready_at.isoformat() if info.ready_at else None,
        error=info.error,
        task_id=info.config.task_id,
        request_id=info.config.request_id,
        reuse_within_request=info.config.reuse_within_request,
    )


# --- Endpoints ---


@router.post(
    "",
    response_model=SandboxResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sandbox(
    body: SandboxCreateRequest,
    user: AuthenticatedUser,
) -> SandboxResponse:
    """Create a new sandbox environment."""
    _require_sandboxes_enabled(user)
    provider = SecretRegistry.get()
    resolved_env_vars = await resolve_env_vars(body.env_vars, provider)
    config = SandboxConfig(
        name=body.name,
        image=body.image,
        repo_url=body.repo_url,
        branch=body.branch,
        setup_commands=body.setup_commands,
        env_vars=resolved_env_vars,
        working_dir=body.working_dir,
        memory_limit=body.memory_limit,
        cpu_limit=body.cpu_limit,
        network_mode=body.network_mode,
        allowed_hosts=body.allowed_hosts,
        timeout_seconds=body.timeout_seconds,
        task_id=body.task_id,
        request_id=body.request_id,
        reuse_within_request=body.reuse_within_request,
        reuse_key=body.reuse_key,
        organization_id=str(user.organization_id),
        requesting_user_id=str(user.id),
    )
    manager = get_sandbox_manager()
    info = await manager.create(config)

    if info.status == SandboxStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sandbox creation failed",
        )

    return _to_response(info)


@router.get("")
async def list_sandboxes(user: AuthenticatedUser):
    """List sandboxes for the caller's organization.

    Admins/owners get every org sandbox; members get only the ones cleared
    for their use — their own, plus any granted to their group ("only
    yours", 2026-10-01).
    """
    manager = get_sandbox_manager()
    if user.role.value in ("admin", "owner"):
        result = await manager.list_all(str(user.organization_id))
    else:
        from lucent.db.sandbox import (
            SandboxRepository,
            get_authorized_sandboxes_pool,
        )

        pool = await get_pool()
        user_pool = await get_authorized_sandboxes_pool(
            pool, {"id": str(user.id), "organization_id": str(user.organization_id)}
        )
        repo = SandboxRepository(user_pool)
        result = await repo.list_sandboxes_accessible_by(
            str(user.id), str(user.organization_id)
        )
    result["items"] = [_to_response(s) for s in result["items"]]
    result["sandboxes_enabled"] = sandboxes_enabled(organization_id=user.organization_id)
    result["custom_tooling_enabled"] = custom_tooling_enabled(
        organization_id=user.organization_id
    )
    return result


@router.get("/{sandbox_id}", response_model=SandboxResponse)
async def get_sandbox(
    sandbox_id: str,
    user: AuthenticatedUser,
) -> SandboxResponse:
    """Get sandbox status and details."""
    info = await _get_sandbox_for_user(sandbox_id, user)
    return _to_response(info)


@router.post("/{sandbox_id}/exec", response_model=ExecResponse)
async def exec_in_sandbox(
    sandbox_id: str,
    body: SandboxExecRequest,
    user: AuthenticatedUser,
) -> ExecResponse:
    """Execute a command in a sandbox."""
    await _get_sandbox_for_user(sandbox_id, user)

    manager = get_sandbox_manager()
    result = await manager.exec(
        sandbox_id,
        body.command,
        cwd=body.cwd,
        env=body.env,
        timeout=body.timeout,
    )
    return ExecResponse(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
    )


@router.get("/{sandbox_id}/files", response_model=FileListResponse)
async def list_sandbox_files(
    sandbox_id: str,
    path: str = "/workspace",
    user: AuthenticatedUser = ...,
) -> FileListResponse:
    """List files in a sandbox directory."""
    await _get_sandbox_for_user(sandbox_id, user)
    safe_path = _validate_sandbox_path(path)
    manager = get_sandbox_manager()
    files = await manager.list_files(sandbox_id, safe_path)
    return FileListResponse(files=files)


@router.get("/{sandbox_id}/files/{file_path:path}")
async def read_sandbox_file(
    sandbox_id: str,
    file_path: str,
    user: AuthenticatedUser,
) -> dict:
    """Read a file from a sandbox."""
    await _get_sandbox_for_user(sandbox_id, user)
    safe_path = _validate_sandbox_path(f"/{file_path}")
    manager = get_sandbox_manager()
    try:
        content = await manager.read_file(sandbox_id, safe_path)
        return {"path": file_path, "content": content.decode("utf-8", errors="replace")}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")


@router.put("/{sandbox_id}/files/{file_path:path}")
async def write_sandbox_file(
    sandbox_id: str,
    file_path: str,
    body: SandboxWriteFileRequest,
    user: AuthenticatedUser,
) -> dict:
    """Write a file to a sandbox."""
    await _get_sandbox_for_user(sandbox_id, user)
    safe_path = _validate_sandbox_path(f"/{file_path}")
    manager = get_sandbox_manager()
    await manager.write_file(sandbox_id, safe_path, body.content.encode("utf-8"))
    return {"path": file_path, "written": True}


@router.post("/{sandbox_id}/stop")
async def stop_sandbox(sandbox_id: str, user: AuthenticatedUser) -> dict:
    """Stop a sandbox (preserves state)."""
    await _get_sandbox_for_user(sandbox_id, user)
    manager = get_sandbox_manager()
    await manager.stop(sandbox_id)
    return {"id": sandbox_id, "status": "stopped"}


@router.delete("/{sandbox_id}")
async def destroy_sandbox(sandbox_id: str, user: AuthenticatedUser) -> dict:
    """Permanently destroy a sandbox."""
    await _get_sandbox_for_user(sandbox_id, user)
    manager = get_sandbox_manager()
    await manager.destroy(sandbox_id)
    return {"id": sandbox_id, "status": "destroyed"}


# --- Sandbox Templates (reusable environment definitions) ---


class TemplateCreateRequest(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
    )
    description: str = Field(default="", max_length=512)
    image: str = Field(default="python:3.12-slim", max_length=256)
    repo_url: str | None = Field(default=None, max_length=512)
    branch: str | None = Field(default=None, max_length=128)
    setup_commands: list[str] = Field(default_factory=list, max_length=50)
    env_vars: dict[str, str] = Field(default_factory=dict)
    working_dir: str = "/workspace"
    docker_bind_mounts: list[dict[str, str | bool]] = Field(default_factory=list, max_length=20)
    extra_hosts: dict[str, str] = Field(default_factory=dict, max_length=20)
    memory_limit: str = Field(default="2g", pattern=r"^[1-9]\d{0,4}[mgt]$")
    cpu_limit: float = Field(default=2.0, gt=0, le=16)
    disk_limit: str = Field(default="10g", pattern=r"^[1-9]\d{0,4}[mgt]$")
    network_mode: Literal["none", "bridge", "allowlist"] = "none"
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    timeout_seconds: int = Field(default=1800, ge=60, le=86400)

    _validate_extra_hosts = field_validator("extra_hosts")(validate_extra_hosts)


class TemplateUpdateRequest(BaseModel):
    name: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
    )
    description: str | None = Field(default=None, max_length=512)
    image: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    setup_commands: list[str] | None = None
    env_vars: dict[str, str] | None = None
    working_dir: str | None = None
    docker_bind_mounts: list[dict[str, str | bool]] | None = None
    extra_hosts: dict[str, str] | None = Field(default=None, max_length=20)
    memory_limit: str | None = None
    cpu_limit: float | None = None
    disk_limit: str | None = None
    network_mode: str | None = None
    allowed_hosts: list[str] | None = None
    timeout_seconds: int | None = None

    _validate_extra_hosts = field_validator("extra_hosts")(validate_extra_hosts)


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateCreateRequest, user: AuthenticatedUser):
    """Create a reusable sandbox template."""
    from lucent.db.sandbox_template import SandboxTemplateRepository

    _require_sandboxes_enabled(user)
    if body.docker_bind_mounts and user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Only administrators can configure Docker bind mounts")
    if body.extra_hosts and user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Only administrators can configure Docker host mappings")
    pool = await get_pool()
    repo = SandboxTemplateRepository(pool)
    return await repo.create(
        name=body.name,
        organization_id=str(user.organization_id),
        description=body.description,
        image=body.image,
        repo_url=body.repo_url,
        branch=body.branch,
        setup_commands=body.setup_commands,
        env_vars=body.env_vars,
        working_dir=body.working_dir,
        docker_bind_mounts=body.docker_bind_mounts,
        extra_hosts=body.extra_hosts,
        memory_limit=body.memory_limit,
        cpu_limit=body.cpu_limit,
        disk_limit=body.disk_limit,
        network_mode=body.network_mode,
        allowed_hosts=body.allowed_hosts,
        timeout_seconds=body.timeout_seconds,
        created_by=str(user.id),
    )


@router.get("/templates")
async def list_templates(user: AuthenticatedUser):
    """List sandbox templates cleared for this principal's use."""
    from lucent.db.sandbox_template import SandboxTemplateRepository, get_authorized_templates_pool

    pool = await get_pool()
    tpl_pool = await get_authorized_templates_pool(
        pool, {"id": str(user.id), "organization_id": str(user.organization_id)}
    )
    repo = SandboxTemplateRepository(tpl_pool)
    result = await repo.list_templates_accessible_by(
        str(user.id),
        str(user.organization_id),
    )
    result["sandboxes_enabled"] = sandboxes_enabled(organization_id=user.organization_id)
    result["custom_tooling_enabled"] = custom_tooling_enabled(
        organization_id=user.organization_id
    )
    return result


@router.get("/templates/{template_id}")
async def get_template(template_id: str, user: AuthenticatedUser):
    """Get a sandbox template by ID — clearance-driven and default-deny."""
    from lucent.db.sandbox_template import SandboxTemplateRepository, get_authorized_templates_pool

    pool = await get_pool()
    tpl_pool = await get_authorized_templates_pool(
        pool, {"id": str(user.id), "organization_id": str(user.organization_id)}
    )
    repo = SandboxTemplateRepository(tpl_pool)
    tpl = await repo.get_usable_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    return tpl


@router.patch("/templates/{template_id}")
async def update_template(template_id: str, body: TemplateUpdateRequest, user: AuthenticatedUser):
    """Update a sandbox template. Only owner or admin can modify."""
    from lucent.access_control import AccessControlService
    from lucent.db.sandbox_template import SandboxTemplateRepository

    if body.docker_bind_mounts and user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Only administrators can configure Docker bind mounts")
    if body.extra_hosts and user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Only administrators can configure Docker host mappings")
    pool = await get_pool()
    acl = AccessControlService(pool)
    if user.role.value not in ("admin", "owner") and not await acl.can_modify(
        str(user.id), "sandbox_template", template_id, str(user.organization_id)
    ):
        raise HTTPException(404, "Template not found")
    repo = SandboxTemplateRepository(pool)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, "No fields to update")
    result = await repo.update(template_id, str(user.organization_id), **updates)
    if not result:
        raise HTTPException(404, "Template not found")
    return result


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str, user: AuthenticatedUser):
    """Delete a sandbox template. Only owner or admin can delete."""
    from lucent.access_control import AccessControlService
    from lucent.db.sandbox_template import SandboxTemplateRepository

    pool = await get_pool()
    acl = AccessControlService(pool)
    if user.role.value not in ("admin", "owner") and not await acl.can_modify(
        str(user.id), "sandbox_template", template_id, str(user.organization_id)
    ):
        raise HTTPException(404, "Template not found")
    repo = SandboxTemplateRepository(pool)
    deleted = await repo.delete(template_id, str(user.organization_id))
    if not deleted:
        raise HTTPException(404, "Template not found")
    return {"id": template_id, "deleted": True}


@router.post("/templates/{template_id}/approve")
async def approve_template(template_id: str, user: AuthenticatedUser):
    """Approve a proposed sandbox template for use in tasks and launches."""
    from lucent.db.sandbox_template import SandboxTemplateRepository

    if user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Only administrators can approve sandbox templates")
    pool = await get_pool()
    repo = SandboxTemplateRepository(pool)
    existing = await repo.get(template_id, str(user.organization_id))
    if not existing:
        raise HTTPException(404, "Template not found")
    if existing.get("status") != "proposed":
        raise HTTPException(409, "Only proposed sandbox templates can be approved")
    template = await repo.set_status(
        template_id,
        str(user.organization_id),
        "approved",
        reviewed_by=str(user.id),
    )
    if not template:
        raise HTTPException(404, "Template not found")
    return template


@router.post("/templates/{template_id}/reject")
async def reject_template(template_id: str, user: AuthenticatedUser):
    """Reject a sandbox template so it cannot be used for work."""
    from lucent.db.sandbox_template import SandboxTemplateRepository

    if user.role.value not in ("admin", "owner"):
        raise HTTPException(403, "Only administrators can reject sandbox templates")
    pool = await get_pool()
    repo = SandboxTemplateRepository(pool)
    existing = await repo.get(template_id, str(user.organization_id))
    if not existing:
        raise HTTPException(404, "Template not found")
    if existing.get("status") != "proposed":
        raise HTTPException(409, "Only proposed sandbox templates can be rejected")
    template = await repo.set_status(
        template_id,
        str(user.organization_id),
        "rejected",
        reviewed_by=str(user.id),
    )
    if not template:
        raise HTTPException(404, "Template not found")
    return template


@router.post("/templates/{template_id}/launch")
async def launch_from_template(
    template_id: str,
    user: AuthenticatedUser,
    name: str | None = None,
) -> SandboxResponse:
    """Launch a sandbox instance from a template."""
    from lucent.db.sandbox_template import (
        SandboxTemplateRepository,
        get_authorized_templates_pool,
    )

    _require_sandboxes_enabled(user)
    pool = await get_pool()
    # Usage is clearance-driven and default-deny: launching needs a grant
    # (built-in org read, ownership, group, or explicit) for this principal.
    tpl_pool = await get_authorized_templates_pool(
        pool, {"id": str(user.id), "organization_id": str(user.organization_id)}
    )
    tpl_repo = SandboxTemplateRepository(tpl_pool)
    tpl = await tpl_repo.get_usable_template(template_id)
    if not tpl:
        raise HTTPException(404, "Template not found")
    if tpl.get("status") != "approved":
        raise HTTPException(409, "Sandbox template must be approved before launch")

    provider = SecretRegistry.get()
    resolved_env_vars = await resolve_env_vars(tpl.get("env_vars") or {}, provider)
    config = SandboxConfig(
        name=name or f"{tpl['name']}-instance",
        image=tpl["image"],
        repo_url=tpl.get("repo_url"),
        branch=tpl.get("branch"),
        setup_commands=tpl.get("setup_commands") or [],
        env_vars=resolved_env_vars,
        working_dir=tpl.get("working_dir", "/workspace"),
        docker_bind_mounts=tpl.get("docker_bind_mounts") or [],
        extra_hosts=tpl.get("extra_hosts") or {},
        memory_limit=tpl.get("memory_limit", "2g"),
        cpu_limit=float(tpl.get("cpu_limit", 2.0)),
        disk_limit=tpl.get("disk_limit", "10g"),
        network_mode=tpl.get("network_mode", "none"),
        allowed_hosts=tpl.get("allowed_hosts") or [],
        timeout_seconds=tpl.get("timeout_seconds", 1800),
        organization_id=str(user.organization_id),
        requesting_user_id=str(user.id),
    )
    manager = get_sandbox_manager()
    info = await manager.create(config)
    if info.status == SandboxStatus.FAILED:
        raise HTTPException(500, "Sandbox creation failed")
    return _to_response(info)
