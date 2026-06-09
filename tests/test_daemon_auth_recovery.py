from datetime import datetime, timedelta, timezone

import pytest

import daemon.daemon as daemon_module
from daemon.daemon import AuthFailureDetectedError, LucentDaemon


@pytest.mark.asyncio
async def test_proactive_rotation_triggers_under_60_minutes(monkeypatch):
    daemon_module.MCP_API_KEY = "hs_test_key"
    daemon_module._current_key_expires_at = datetime.now(timezone.utc) + timedelta(minutes=45)

    called: dict[str, object] = {}

    async def _verify(_api_key: str) -> bool:
        return True

    async def _recover(instance_id: str, *, force_rotate: bool = False) -> bool:
        called["instance_id"] = instance_id
        called["force_rotate"] = force_rotate
        return True

    monkeypatch.setattr(daemon_module, "_verify_api_key", _verify)
    monkeypatch.setattr(daemon_module, "_handle_auth_failure", _recover)

    ok = await daemon_module._verify_and_provision_key("inst-1")

    assert ok is True
    assert called["instance_id"] == "inst-1"
    assert called["force_rotate"] is True


def test_detects_mcp_auth_failure_from_tool_error_response():
    daemon = LucentDaemon()
    payload = (
        '{"jsonrpc":"2.0","error":{"code":-32001,'
        '"message":"Unauthorized: Invalid or expired credentials"},"id":null}'
    )

    assert daemon._is_mcp_auth_failure_message(payload) is True
    assert daemon._is_mcp_auth_failure_message("tool failed with timeout") is False


def test_recoverable_mcp_auth_failure_requires_memory_server_tool():
    daemon = LucentDaemon()
    payload = "Error: Unauthorized: Invalid or expired credentials"

    assert daemon._is_recoverable_mcp_auth_failure("bash", payload) is False
    assert daemon._is_recoverable_mcp_auth_failure("view", payload) is False
    assert daemon._is_recoverable_mcp_auth_failure(
        "memory-server-create_memory",
        payload,
    ) is True
    assert daemon._is_recoverable_mcp_auth_failure("create_memory", payload) is True


def test_scoped_mcp_headers_can_defer_authorization_until_jit_mint():
    headers = daemon_module._scoped_mcp_headers(
        memory_scope="user",
        memory_scope_user_id="user-123",
        org_id="org-123",
        task_id="task-123",
    )

    assert "Authorization" not in headers
    assert headers["X-Lucent-Memory-Scope"] == "user"
    assert headers["X-Lucent-Memory-Scope-User-Id"] == "user-123"
    assert headers["X-Lucent-Org-Id"] == "org-123"


@pytest.mark.asyncio
async def test_run_session_recovers_once_after_auth_failure(monkeypatch):
    daemon = LucentDaemon()
    daemon.instance_id = "instance-retry-once"
    daemon_module.MCP_CONFIG = {
        "memory-server": {
            "type": "http",
            "url": "http://mcp",
            "headers": {"Authorization": "Bearer old-key"},
            "tools": ["*"],
        }
    }

    call_count = {"n": 0}
    seen_headers: list[str] = []

    async def _inner(_name, _system, _prompt, model=None, mcp_config_override=None, **_kwargs):
        call_count["n"] += 1
        if mcp_config_override and mcp_config_override.get("memory-server"):
            seen_headers.append(
                mcp_config_override["memory-server"]["headers"]["Authorization"]
            )
        if call_count["n"] == 1:
            raise AuthFailureDetectedError("Unauthorized: Invalid or expired credentials")
        return "ok"

    recover_calls = {"n": 0}

    async def _recover(_instance_id: str, *, force_rotate: bool = False) -> bool:
        recover_calls["n"] += 1
        daemon_module.MCP_CONFIG = {
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {"Authorization": "Bearer new-key"},
                "tools": ["*"],
            }
        }
        return True

    monkeypatch.setattr(daemon, "_run_session_inner", _inner)
    monkeypatch.setattr(daemon_module, "_handle_auth_failure", _recover)

    result = await daemon.run_session(
        "auth-recovery-test",
        "system",
        "prompt",
        mcp_config_override={
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {"Authorization": "Bearer old-key"},
                "tools": ["*"],
            },
            "other": {"type": "http", "url": "http://other"},
        },
    )

    assert result == "ok"
    assert call_count["n"] == 2
    assert recover_calls["n"] == 1
    assert seen_headers == ["Bearer old-key", "Bearer new-key"]


@pytest.mark.asyncio
async def test_run_session_remints_task_scoped_key_without_daemon_fallback(monkeypatch):
    daemon = LucentDaemon()
    daemon.instance_id = "instance-task-scope"
    daemon_module.MCP_CONFIG = {
        "memory-server": {
            "type": "http",
            "url": "http://mcp",
            "headers": {"Authorization": "Bearer daemon-key"},
            "tools": ["*"],
        }
    }

    call_count = {"n": 0}
    seen_headers: list[str] = []
    daemon_recovery_calls = {"n": 0}
    minted: list[tuple[str, str | None, str, int | None]] = []

    async def _inner(_name, _system, _prompt, model=None, mcp_config_override=None, **_kwargs):
        call_count["n"] += 1
        seen_headers.append(
            mcp_config_override["memory-server"]["headers"]["Authorization"]
        )
        if call_count["n"] == 1:
            raise AuthFailureDetectedError("Unauthorized: Invalid or expired credentials")
        return "ok"

    async def _recover(_instance_id: str, *, force_rotate: bool = False) -> bool:
        daemon_recovery_calls["n"] += 1
        return True

    async def _mint_scoped_api_key(
        *, memory_scope, memory_scope_user_id=None, org_id, ttl_minutes=None
    ):
        minted.append((memory_scope, memory_scope_user_id, org_id, ttl_minutes))
        return "hs_refreshed-scoped-key"

    monkeypatch.setattr(daemon, "_run_session_inner", _inner)
    monkeypatch.setattr(daemon_module, "_handle_auth_failure", _recover)
    monkeypatch.setattr(daemon_module, "_mint_scoped_api_key", _mint_scoped_api_key)

    result = await daemon.run_session(
        "task-scoped-auth-recovery-test",
        "system",
        "prompt",
        mcp_config_override={
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {
                    "Authorization": "Bearer scoped-key",
                    "X-Lucent-Task-Id": "task-123",
                    "X-Lucent-Memory-Scope": "user",
                    "X-Lucent-Memory-Scope-User-Id": "user-123",
                    "X-Lucent-Org-Id": "org-123",
                },
                "tools": ["*"],
            },
        },
    )

    assert result == "ok"
    assert seen_headers == ["Bearer scoped-key", "Bearer hs_refreshed-scoped-key"]
    assert daemon_recovery_calls["n"] == 0
    assert minted == [("user", "user-123", "org-123", daemon_module._scoped_key_ttl_minutes())]


@pytest.mark.asyncio
async def test_run_session_mints_task_scoped_key_just_in_time(monkeypatch):
    daemon = LucentDaemon()
    daemon.instance_id = "instance-jit"

    seen_headers: list[dict] = []
    minted: list[tuple[str, str | None, str, int | None]] = []

    async def _inner(_name, _system, _prompt, model=None, mcp_config_override=None, **_kwargs):
        seen_headers.append(dict(mcp_config_override["memory-server"]["headers"]))
        return "ok"

    async def _mint_scoped_api_key(
        *, memory_scope, memory_scope_user_id=None, org_id, ttl_minutes=None
    ):
        minted.append((memory_scope, memory_scope_user_id, org_id, ttl_minutes))
        return "hs_jit-scoped-key"

    monkeypatch.setattr(daemon, "_run_session_inner", _inner)
    monkeypatch.setattr(daemon_module, "_mint_scoped_api_key", _mint_scoped_api_key)

    result = await daemon.run_session(
        "task-scoped-jit-test",
        "system",
        "prompt",
        mcp_config_override={
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {
                    "X-Lucent-Task-Id": "task-123",
                    "X-Lucent-Memory-Scope": "user",
                    "X-Lucent-Memory-Scope-User-Id": "user-123",
                    "X-Lucent-Org-Id": "org-123",
                },
                "tools": ["*"],
            },
        },
    )

    assert result == "ok"
    assert seen_headers[0]["Authorization"] == "Bearer hs_jit-scoped-key"
    assert minted == [("user", "user-123", "org-123", daemon_module._scoped_key_ttl_minutes())]


@pytest.mark.asyncio
async def test_run_session_auth_retry_guard_prevents_infinite_loop(monkeypatch):
    daemon = LucentDaemon()
    daemon.instance_id = "instance-guard"

    async def _inner(_name, _system, _prompt, model=None, mcp_config_override=None, **_kwargs):
        raise AuthFailureDetectedError("Unauthorized: Invalid or expired credentials")

    recover_calls = {"n": 0}

    async def _recover(_instance_id: str, *, force_rotate: bool = False) -> bool:
        recover_calls["n"] += 1
        return True

    monkeypatch.setattr(daemon, "_run_session_inner", _inner)
    monkeypatch.setattr(daemon_module, "_handle_auth_failure", _recover)

    result = await daemon.run_session("auth-guard-test", "system", "prompt")

    assert result is None
    assert recover_calls["n"] == 1


@pytest.mark.asyncio
async def test_cognitive_cycle_keeps_preloop_verification(monkeypatch):
    daemon = LucentDaemon()
    daemon.instance_id = "instance-preloop"
    daemon.cycle_count = 0

    verify_calls = {"n": 0}
    run_calls = {"n": 0}

    async def _verify_and_provision(_instance_id: str) -> bool:
        verify_calls["n"] += 1
        return True

    async def _check_adapt() -> None:
        return None

    async def _build_prompt() -> str:
        return "system"

    async def _run_session(*args, **kwargs):
        run_calls["n"] += 1
        return "done"

    monkeypatch.setattr(daemon_module, "_verify_and_provision_key", _verify_and_provision)
    monkeypatch.setattr(daemon, "_check_environment_adaptation", _check_adapt)
    monkeypatch.setattr(daemon_module, "build_cognitive_prompt", _build_prompt)
    monkeypatch.setattr(daemon, "run_session", _run_session)

    await daemon.run_cognitive_cycle()

    assert verify_calls["n"] == 1
    assert run_calls["n"] == 1
