"""Sandbox provisioning and lifecycle helpers for daemon tasks."""

from __future__ import annotations

import hashlib

from daemon.runtime.module_proxy import runtime


class SandboxLifecycleMixin:
    """Sandbox provisioning and reuse behavior composed into the daemon."""

    async def _create_task_sandbox(self, *args, **kwargs):
        return await _create_task_sandbox(self, *args, **kwargs)

    async def _request_has_later_reusable_sandbox_task(self, *args, **kwargs):
        return await _request_has_later_reusable_sandbox_task(self, *args, **kwargs)

    async def _destroy_task_sandbox(self, *args, **kwargs):
        return await _destroy_task_sandbox(self, *args, **kwargs)

    async def _get_technical_context_for_request(self, *args, **kwargs):
        return await _get_technical_context_for_request(self, *args, **kwargs)

    async def _resolve_sandbox_template(self, *args, **kwargs):
        return await _resolve_sandbox_template(self, *args, **kwargs)


async def _create_task_sandbox(
    self,
    task_id: str,
    sandbox_config: dict,
    *,
    request_id: str | None = None,
    requesting_user_id: str,
    org_id: str,
    sequence_order: int = 0,
    sandbox_template_id: str | None = None,
) -> tuple[str | None, "SandboxConfig | None", dict | None, bool]:
    """Create or reuse a sandbox for a task."""
    from lucent.sandbox.manager import get_sandbox_manager
    from lucent.sandbox.models import SandboxConfig

    provider = await runtime.get_secret_provider()
    runtime.set_current_user({"id": requesting_user_id, "organization_id": org_id})
    try:
        env_vars = await runtime.resolve_secret_env_vars(
            sandbox_config.get("env_vars", {}), provider
        )
    finally:
        runtime.set_current_user(None)

    git_credentials = sandbox_config.get("git_credentials")
    repo_url = sandbox_config.get("repo_url") or ""
    if not git_credentials and repo_url and "github.com" in repo_url:
        try:
            from lucent.db import get_pool
            from lucent.integrations.encryption import (
                BackendCredentialEncryptor,
                EncryptionError,
            )

            pool = await get_pool()
            async with pool.acquire() as conn:
                credential = await conn.fetchrow(
                    """
                    SELECT id, encrypted_secret_payload
                    FROM enterprise_credentials
                    WHERE integration_type = 'github'
                      AND scope_type = 'user'
                      AND owner_user_id = $1::uuid
                      AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    requesting_user_id,
                )
            if not credential:
                runtime.log(
                    f"No GitHub token on file for user {requesting_user_id[:8]}; "
                    "private repo clones will fail. User can connect GitHub in settings.",
                    "WARN",
                )
            else:
                try:
                    payload = BackendCredentialEncryptor().decrypt(
                        credential["encrypted_secret_payload"]
                    )
                    token = payload.get("access_token")
                    if token:
                        git_credentials = f"x-access-token:{token}"
                        runtime.log(
                            f"Resolved GitHub token for user {requesting_user_id[:8]} "
                            "for sandbox clone"
                        )
                    else:
                        runtime.log(
                            f"GitHub credential row for user {requesting_user_id[:8]} "
                            "decrypted but has no access_token field",
                            "WARN",
                        )
                except EncryptionError as error:
                    runtime.log(
                        f"GitHub credential for user {requesting_user_id[:8]} could not "
                        f"be decrypted ({error}). The user must reconnect GitHub in "
                        "Settings → Integrations.",
                        "WARN",
                    )
        except Exception as error:
            runtime.log(f"GitHub token lookup failed (non-fatal): {error}", "WARN")

    default_network_mode = "none"
    default_allowed_hosts: list[str] = []
    if sandbox_config.get("repo_url") and "network_mode" not in sandbox_config:
        default_network_mode = "allowlist"
        default_allowed_hosts = [
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "deb.debian.org",
            "security.debian.org",
            "archive.ubuntu.com",
            "security.ubuntu.com",
            "dl-cdn.alpinelinux.org",
        ]

    reuse_enabled = bool(sandbox_config.get("reuse_within_request", False))
    reuse_key = sandbox_config.get("reuse_key") or sandbox_template_id
    if reuse_enabled and not reuse_key:
        reuse_shape = {
            key: sandbox_config.get(key)
            for key in (
                "image",
                "repo_url",
                "branch",
                "setup_commands",
                "working_dir",
                "memory_limit",
                "cpu_limit",
                "network_mode",
                "allowed_hosts",
            )
        }
        reuse_key = "config-" + hashlib.sha256(
            runtime.json.dumps(reuse_shape, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]

    config = SandboxConfig(
        name=sandbox_config.get("name", f"task-{task_id[:12]}"),
        image=sandbox_config.get("image", "lucent-sandbox:base"),
        repo_url=sandbox_config.get("repo_url"),
        branch=sandbox_config.get("branch"),
        git_credentials=git_credentials,
        setup_commands=sandbox_config.get("setup_commands", []),
        env_vars=env_vars,
        working_dir=sandbox_config.get("working_dir", "/workspace"),
        memory_limit=sandbox_config.get("memory_limit", "2g"),
        cpu_limit=sandbox_config.get("cpu_limit", 2.0),
        network_mode=sandbox_config.get("network_mode", default_network_mode),
        allowed_hosts=sandbox_config.get("allowed_hosts", default_allowed_hosts),
        timeout_seconds=sandbox_config.get("timeout_seconds", 1800),
        reuse_within_request=reuse_enabled,
        reuse_key=reuse_key,
        reuse_sequence_order=sequence_order,
        output_mode=sandbox_config.get("output_mode"),
        commit_approved=bool(sandbox_config.get("commit_approved", False)),
        task_id=task_id,
        request_id=request_id or sandbox_config.get("request_id"),
        organization_id=org_id,
        requesting_user_id=requesting_user_id,
    )
    manager = get_sandbox_manager()
    info, reused = await manager.get_or_create_for_request(
        config, sequence_order=sequence_order
    )
    if info.status.value in {"ready", "running"}:
        action = "reused" if reused else "created"
        runtime.log(f"Sandbox {info.id[:12]} {action} for task {task_id[:8]}")
        return info.id, config, None, reused

    error = info.error or "Sandbox creation failed without a specific error"
    runtime.log(
        f"Sandbox creation failed for task {task_id[:8]}: {error} "
        f"(image={config.image!r}, repo_url={config.repo_url!r}, "
        f"network_mode={config.network_mode!r})",
        "WARN",
    )
    return None, None, {
        "stage": "sandbox_create",
        "detail": error,
        "image": config.image,
        "repo_url": config.repo_url,
        "branch": config.branch,
        "network_mode": config.network_mode,
        "allowed_hosts": list(config.allowed_hosts or []),
        "working_dir": config.working_dir,
    }, False


async def _request_has_later_reusable_sandbox_task(
    self, request_id: str, task_id: str, sequence_order: int
) -> bool:
    """Return whether a later unfinished task will reuse this request sandbox."""
    from lucent.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM tasks
                    WHERE request_id = $1::uuid
                      AND id != $2::uuid
                      AND sequence_order > $3
                      AND status IN ('pending', 'planned', 'running', 'needs_review')
                      AND COALESCE(
                          (sandbox_config->>'reuse_within_request')::boolean,
                          false
                      )
                )
                """,
                request_id,
                task_id,
                sequence_order,
            )
        )


async def _destroy_task_sandbox(self, sandbox_id: str) -> None:
    """Destroy a task's sandbox."""
    from lucent.sandbox.manager import get_sandbox_manager

    await get_sandbox_manager().destroy(sandbox_id)
    runtime.log(f"Sandbox {sandbox_id[:12]} destroyed")


async def _get_technical_context_for_request(self, request_id: str) -> str | None:
    """Build request context from technical memories matching its target repo."""
    try:
        request = await runtime.RequestAPI.get_request(request_id)
        if not request:
            return None
        target_repo = request.get("target_repo")
        target_paths = request.get("target_paths") or []
        if not target_repo:
            return None
        async with runtime.httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{runtime.API_BASE}/search",
                headers=runtime.API_HEADERS,
                params={"q": target_repo, "type": "technical", "limit": "20"},
            )
            if response.status_code != 200:
                return None
            memories = response.json().get("memories", [])
        relevant = [
            memory
            for memory in memories
            if (memory.get("metadata") or {}).get("repo", "") == target_repo
        ]
        if not relevant:
            return None
        if target_paths:
            prioritized: list[dict] = []
            general: list[dict] = []
            for memory in relevant:
                metadata = memory.get("metadata") or {}
                directory = metadata.get("directory", "")
                filename = metadata.get("filename", "")
                targeted = any(
                    (directory and directory.startswith(path))
                    or (filename and filename.startswith(path))
                    or (not directory and not filename)
                    for path in target_paths
                )
                (prioritized if targeted else general).append(memory)
            relevant = prioritized + general[:3]
        parts = [
            f"--- TECHNICAL CONTEXT ({target_repo}) ---",
            "The following technical memories describe the codebase conventions and "
            "patterns for this repository.",
            "Follow these conventions in your work. These represent the established "
            "standards.\n",
        ]
        for memory in relevant:
            metadata = memory.get("metadata") or {}
            scope = metadata.get("filename") or metadata.get("directory") or "(repo-level)"
            parts.extend((f"### {scope}", memory.get("content", ""), ""))
        return "\n".join(parts)
    except Exception as error:
        runtime.log(f"Failed to fetch technical context: {error}", "DEBUG")
        return None


async def _resolve_sandbox_template(
    self,
    template_id: str,
    *,
    org_id: str,
    requesting_user_id: str,
) -> dict | None:
    """Resolve an approved sandbox template accessible to the requesting user."""
    try:
        from lucent.db import get_pool

        pool = await get_pool()
    except Exception:
        runtime.log("Sandbox ACL check skipped (no DB pool in daemon)", "DEBUG")
        return None
    try:
        from lucent.access_control import AccessControlService
        from lucent.db.sandbox_template import SandboxTemplateRepository

        access_control = AccessControlService(pool)
        if not await access_control.can_access(
            requesting_user_id, "sandbox_template", template_id, org_id
        ):
            runtime.log(
                f"Sandbox template {template_id[:8]} not accessible to requesting "
                f"user {requesting_user_id[:8]}",
                "WARN",
            )
            return None
        repository = SandboxTemplateRepository(pool)
        template = await repository.get(template_id, org_id)
        if not template:
            runtime.log(f"Sandbox template {template_id[:8]} not found", "WARN")
            return None
        if template.get("status") and template["status"] != "approved":
            runtime.log(
                f"Sandbox template '{template.get('name', template_id[:8])}' has "
                f"status={template['status']!r} — refusing dispatch. Only approved "
                "templates can be used by tasks.",
                "WARN",
            )
            return None
        config = repository.to_sandbox_config(template)
        await repository.mark_used(template_id)
        runtime.log(
            f"Resolved sandbox template '{template.get('name', template_id[:8])}' "
            "for dispatch"
        )
        return config
    except Exception as error:
        runtime.log(
            f"Failed to resolve sandbox template {template_id[:8]}: {error}", "WARN"
        )
        return None
