"""Sandbox-backed ``python`` runtime.

This is the original managed-tool executor, moved behind the
:class:`~lucent.services.tool_runtimes.base.ToolRuntime` contract. Behaviour is
deliberately unchanged: the same sandbox configuration, the same staged runner
script, the same pip install step, the same output file contract, and the same
credential resolution boundary.

The one structural difference is ownership of the sandbox lifecycle. The runtime
creates *and* destroys its own container in a ``finally`` block, so a non-sandbox
runtime has no sandbox to leak and the executor no longer needs to know that
sandboxes exist.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from lucent.auth import set_current_user
from lucent.logging import get_logger
from lucent.sandbox.manager import get_sandbox_manager
from lucent.sandbox.models import SandboxConfig, SandboxStatus
from lucent.secrets import SecretRegistry, resolve_env_vars
from lucent.services.tool_runtimes.base import (
    RuntimeExecutionContext,
    RuntimeExecutionResult,
    ToolRuntimeError,
)

logger = get_logger("services.tool_runtimes.python")

# Sandbox containers cap a run at ``timeout_seconds``; the container itself is
# given a little slack so the guest can report a timeout instead of being killed
# mid-write and losing output.json.
_SANDBOX_TIMEOUT_GRACE = 60
_MIN_SANDBOX_TIMEOUT = 120


class PythonRuntime:
    """Execute a managed tool's Python entrypoint inside a sandbox container."""

    runtime_type = "python"
    requires_sandbox = True

    async def execute(self, context: RuntimeExecutionContext) -> RuntimeExecutionResult:
        tool = context.runtime_config.get("_tool") or {}
        if not tool:
            raise ToolRuntimeError(
                "Python runtime requires the resolved tool definition in runtime_config"
            )
        manager = get_sandbox_manager()
        sandbox_id: str | None = None
        stdout = ""
        stderr = ""
        try:
            config = await self._build_sandbox_config(
                tool, org_id=context.org_id, user_id=context.user_id
            )
            info = await manager.create(config)
            sandbox_id = info.id
            if info.status != SandboxStatus.READY:
                raise ToolRuntimeError(info.error or "Sandbox failed to become ready")

            await self._stage_runtime(manager, sandbox_id, tool, context.arguments)
            await self._install_requirements(manager, sandbox_id, tool)

            timeout = int(tool.get("timeout_seconds") or 300)
            exec_result = await manager.exec(
                sandbox_id,
                "python /workspace/run_managed_tool.py",
                cwd="/workspace",
                timeout=timeout,
            )
            stdout = str(exec_result.stdout or "")[:12000]
            stderr = str(exec_result.stderr or "")[:12000]
            if exec_result.timed_out:
                raise ToolRuntimeError(f"Tool timed out after {timeout}s")

            output = await self._read_output(manager, sandbox_id)
            stdout = str(output.get("stdout") or exec_result.stdout or "")[:12000]
            stderr = str(output.get("stderr") or exec_result.stderr or "")[:12000]
            if not output.get("ok"):
                return RuntimeExecutionResult(
                    ok=False,
                    stdout=stdout,
                    stderr=stderr,
                    error=str(
                        output.get("error") or exec_result.stderr or "Tool execution failed"
                    ),
                    sandbox_id=sandbox_id,
                )
            return RuntimeExecutionResult(
                ok=True,
                result=output.get("result"),
                stdout=stdout,
                stderr=stderr,
                sandbox_id=sandbox_id,
            )
        finally:
            if sandbox_id:
                try:
                    await manager.destroy(sandbox_id)
                except Exception:
                    logger.warning(
                        "Failed to destroy managed-tool sandbox %s", sandbox_id, exc_info=True
                    )

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
            extra_hosts=dict(network_policy.get("extra_hosts") or {}),
            timeout_seconds=max(timeout_seconds + _SANDBOX_TIMEOUT_GRACE, _MIN_SANDBOX_TIMEOUT),
            idle_timeout_seconds=60,
            organization_id=org_id,
            requesting_user_id=user_id,
        )

    async def _install_requirements(
        self, manager, sandbox_id: str, tool: dict[str, Any]
    ) -> None:
        requirements = [
            str(req).strip() for req in (tool.get("requirements") or []) if str(req).strip()
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
            raise ToolRuntimeError(
                f"Failed to install tool requirements: {str(result.stderr)[:500]}"
            )

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
            raise ToolRuntimeError(f"Tool did not produce a valid output.json: {exc}") from exc
        raise ToolRuntimeError("Tool did not produce a JSON object")

    @staticmethod
    def _runner_script(entrypoint: str) -> str:
        return textwrap.dedent(f"""
            import asyncio
            import contextlib
            import importlib.util
            import inspect
            import io
            import json

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
                        'error': 'Managed tool execution failed',
                        'error_type': type(exc).__name__,
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
