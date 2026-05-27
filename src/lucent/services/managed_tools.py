"""Sandbox-backed execution for managed tool definitions."""

from __future__ import annotations

import json
import textwrap
import time
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError, validate

from lucent.auth import set_current_user
from lucent.db.definitions import DefinitionRepository
from lucent.logging import get_logger
from lucent.sandbox.manager import get_sandbox_manager
from lucent.sandbox.models import SandboxConfig, SandboxStatus
from lucent.secrets import SecretRegistry, resolve_env_vars

logger = get_logger("services.managed_tools")


class ManagedToolError(Exception):
    """Base error for managed tool execution failures."""


class ManagedToolBlockedError(ManagedToolError):
    """Raised when auth or policy blocks execution."""


@dataclass
class ManagedToolExecutionResult:
    """Structured result returned by the managed tool executor."""

    ok: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    sandbox_id: str | None = None
    duration_ms: int | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ok": self.ok,
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "sandbox_id": self.sandbox_id,
            "duration_ms": self.duration_ms,
            "run_id": self.run_id,
        }
        if self.error:
            data["error"] = self.error
        return data


class ManagedToolExecutor:
    """Execute approved managed tool definitions inside sandbox containers.

    The executor intentionally owns the security-sensitive path instead of
    letting generated tool code run as host subprocesses. Policy is enforced in
    this order:
    1. caller must be in the same org and able to access the tool definition;
    2. tool must be active;
    3. when invoked from an agent context, the tool must be granted to that agent;
    4. input/output schemas are validated outside the container;
    5. network/resource/secrets policy are applied before code execution.
    """

    def __init__(self, repo: DefinitionRepository):
        self.repo = repo

    async def execute(
        self,
        *,
        tool: dict[str, Any],
        arguments: dict[str, Any] | None,
        org_id: str,
        user_id: str,
        user_role: str | None = None,
        agent_id: str | None = None,
        enforce_agent_grant: bool = True,
    ) -> ManagedToolExecutionResult:
        if tool.get("status") != "active":
            raise ManagedToolBlockedError("Managed tool is not active")

        auth_policy = tool.get("auth_policy") or {}
        allowed_roles = auth_policy.get("roles")
        if allowed_roles and user_role not in set(allowed_roles):
            raise ManagedToolBlockedError("Caller role is not allowed to use this managed tool")

        if agent_id and enforce_agent_grant:
            granted = await self.repo.is_managed_tool_granted_to_agent(agent_id, str(tool["id"]))
            if not granted:
                raise ManagedToolBlockedError("Managed tool is not granted to this agent")

        payload = arguments or {}
        self._validate_input(tool, payload)

        run = await self.repo.create_managed_tool_run(
            tool_id=str(tool["id"]),
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            input_payload=payload,
        )
        run_id = str(run["id"])
        started = time.monotonic()
        sandbox_id: str | None = None

        try:
            manager = get_sandbox_manager()
            config = await self._build_sandbox_config(tool, org_id=org_id, user_id=user_id)
            info = await manager.create(config)
            sandbox_id = info.id
            if info.status != SandboxStatus.READY:
                raise ManagedToolError(info.error or "Sandbox failed to become ready")

            await self._stage_runtime(manager, sandbox_id, tool, payload)
            await self._install_requirements(manager, sandbox_id, tool)
            timeout = int(tool.get("timeout_seconds") or 300)
            exec_result = await manager.exec(
                sandbox_id,
                "python /workspace/run_managed_tool.py",
                cwd="/workspace",
                timeout=timeout,
            )
            if exec_result.timed_out:
                raise ManagedToolError(f"Tool timed out after {timeout}s")

            output = await self._read_output(manager, sandbox_id)
            duration_ms = int((time.monotonic() - started) * 1000)
            if not output.get("ok"):
                error = str(output.get("error") or exec_result.stderr or "Tool execution failed")
                await self.repo.complete_managed_tool_run(
                    run_id,
                    status="failed",
                    output_payload=output,
                    error=error,
                    sandbox_id=sandbox_id,
                    duration_ms=duration_ms,
                )
                return ManagedToolExecutionResult(
                    ok=False,
                    stdout=str(output.get("stdout") or exec_result.stdout or "")[:12000],
                    stderr=str(output.get("stderr") or exec_result.stderr or "")[:12000],
                    error=error,
                    sandbox_id=sandbox_id,
                    duration_ms=duration_ms,
                    run_id=run_id,
                )

            result_value = output.get("result")
            self._validate_output(tool, result_value)
            await self.repo.complete_managed_tool_run(
                run_id,
                status="completed",
                output_payload=output,
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
            )
            return ManagedToolExecutionResult(
                ok=True,
                result=result_value,
                stdout=str(output.get("stdout") or "")[:12000],
                stderr=str(output.get("stderr") or "")[:12000],
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
                run_id=run_id,
            )
        except ManagedToolBlockedError:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self.repo.complete_managed_tool_run(
                run_id,
                status="blocked",
                error="Execution blocked by policy",
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
            )
            raise
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self.repo.complete_managed_tool_run(
                run_id,
                status="failed",
                error=str(exc),
                sandbox_id=sandbox_id,
                duration_ms=duration_ms,
            )
            raise
        finally:
            if sandbox_id:
                try:
                    await get_sandbox_manager().destroy(sandbox_id)
                except Exception:
                    logger.warning("Failed to destroy managed-tool sandbox %s", sandbox_id,
                                   exc_info=True)

    def _validate_input(self, tool: dict[str, Any], payload: dict[str, Any]) -> None:
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        try:
            validate(instance=payload, schema=schema)
        except ValidationError as exc:
            raise ManagedToolBlockedError(
                f"Input does not match tool schema: {exc.message}"
            ) from exc

    def _validate_output(self, tool: dict[str, Any], value: Any) -> None:
        schema = tool.get("output_schema")
        if not schema:
            return
        try:
            validate(instance=value, schema=schema)
        except ValidationError as exc:
            raise ManagedToolError(f"Output does not match tool schema: {exc.message}") from exc

    async def _build_sandbox_config(
        self,
        tool: dict[str, Any],
        *,
        org_id: str,
        user_id: str,
    ) -> SandboxConfig:
        runtime_config = tool.get("runtime_config") or {}
        network_policy = tool.get("network_policy") or {}
        resource_limits = tool.get("resource_limits") or {}
        env_vars = tool.get("env_vars") or {}

        provider = SecretRegistry.get()
        # Credential/secret references are user-scoped. Set current user only
        # while resolving them, then clear immediately so execution remains
        # explicit and auditable.
        set_current_user({"id": user_id, "organization_id": org_id, "role": "member"})
        try:
            resolved_env = await resolve_env_vars(env_vars, provider)
        finally:
            set_current_user(None)

        timeout_seconds = int(tool.get("timeout_seconds") or 300)
        return SandboxConfig(
            name=f"managed-tool-{tool.get('name', 'tool')}",
            image=runtime_config.get("image") or "lucent-sandbox:base",
            setup_commands=[],
            env_vars=resolved_env,
            working_dir="/workspace",
            memory_limit=str(resource_limits.get("memory_limit") or "512m"),
            cpu_limit=float(resource_limits.get("cpu_limit") or 1.0),
            disk_limit=str(resource_limits.get("disk_limit") or "1g"),
            network_mode=str(network_policy.get("network_mode") or "none"),
            allowed_hosts=list(network_policy.get("allowed_hosts") or []),
            timeout_seconds=max(timeout_seconds + 60, 120),
            idle_timeout_seconds=60,
            organization_id=org_id,
            requesting_user_id=user_id,
        )

    async def _install_requirements(self, manager, sandbox_id: str, tool: dict[str, Any]) -> None:
        requirements = [
            str(req).strip()
            for req in (tool.get("requirements") or [])
            if str(req).strip()
        ]
        if not requirements:
            return
        result = await manager.exec(
            sandbox_id,
            "python -m pip install --disable-pip-version-check -q -r /workspace/requirements.txt",
            cwd="/workspace",
            timeout=300,
        )
        if result.exit_code != 0:
            raise ManagedToolError(f"Failed to install tool requirements: {result.stderr[:500]}")

    async def _stage_runtime(
        self,
        manager,
        sandbox_id: str,
        tool: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        await manager.write_file(
            sandbox_id,
            "/workspace/managed_tool_impl.py",
            str(tool.get("source_code") or "").encode("utf-8"),
        )
        await manager.write_file(
            sandbox_id,
            "/workspace/input.json",
            json.dumps(payload).encode("utf-8"),
        )
        requirements = "\n".join(
            str(req).strip() for req in (tool.get("requirements") or []) if str(req).strip()
        )
        await manager.write_file(
            sandbox_id,
            "/workspace/requirements.txt",
            requirements.encode("utf-8"),
        )
        await manager.write_file(
            sandbox_id,
            "/workspace/run_managed_tool.py",
            self._runner_script(str(tool.get("entrypoint") or "handler")).encode("utf-8"),
        )

    async def _read_output(self, manager, sandbox_id: str) -> dict[str, Any]:
        try:
            raw = await manager.read_file(sandbox_id, "/workspace/output.json")
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:
            raise ManagedToolError(f"Tool did not produce a valid output.json: {exc}") from exc
        raise ManagedToolError("Tool did not produce a JSON object")

    @staticmethod
    def _runner_script(entrypoint: str) -> str:
        return textwrap.dedent(f"""
            import asyncio
            import contextlib
            import importlib.util
            import inspect
            import io
            import json
            import traceback

            ENTRYPOINT = {entrypoint!r}

            async def _main():
                stdout = io.StringIO()
                stderr = io.StringIO()
                try:
                    with open('/workspace/input.json', 'r', encoding='utf-8') as fh:
                        payload = json.load(fh)
                    spec = importlib.util.spec_from_file_location(
                        'managed_tool_impl', '/workspace/managed_tool_impl.py'
                    )
                    if spec is None or spec.loader is None:
                        raise RuntimeError('Could not load managed tool module')
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    func = getattr(module, ENTRYPOINT, None)
                    if func is None or not callable(func):
                        raise RuntimeError(f'Entrypoint {{ENTRYPOINT}} was not found or callable')
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        result = func(payload)
                        if inspect.isawaitable(result):
                            result = await result
                    output = {{
                        'ok': True,
                        'result': result,
                        'stdout': stdout.getvalue(),
                        'stderr': stderr.getvalue(),
                    }}
                    exit_code = 0
                except Exception as exc:
                    output = {{
                        'ok': False,
                        'error': str(exc),
                        'traceback': traceback.format_exc(limit=20),
                        'stdout': stdout.getvalue(),
                        'stderr': stderr.getvalue(),
                    }}
                    exit_code = 1
                with open('/workspace/output.json', 'w', encoding='utf-8') as fh:
                    json.dump(output, fh, ensure_ascii=False)
                raise SystemExit(exit_code)

            if __name__ == '__main__':
                asyncio.run(_main())
            """).strip() + "\n"
