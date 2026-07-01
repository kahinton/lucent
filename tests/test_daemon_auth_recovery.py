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


@pytest.mark.asyncio
async def test_run_session_remints_scoped_memory_key_after_auth_failure(monkeypatch):
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
    seen_scope_headers: list[dict[str, str]] = []

    async def _inner(_name, _system, _prompt, model=None, mcp_config_override=None, **_kwargs):
        call_count["n"] += 1
        if mcp_config_override and mcp_config_override.get("memory-server"):
            headers = mcp_config_override["memory-server"]["headers"]
            seen_headers.append(headers["Authorization"])
            seen_scope_headers.append(
                {
                    "scope": headers.get("X-Lucent-Memory-Scope"),
                    "scope_user": headers.get("X-Lucent-Memory-Scope-User-Id"),
                    "org_id": headers.get("X-Lucent-Org-Id"),
                }
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

    mint_calls: list[dict[str, object]] = []

    async def _mint(**kwargs):
        mint_calls.append(kwargs)
        return "hs_scoped_new"

    monkeypatch.setattr(daemon, "_run_session_inner", _inner)
    monkeypatch.setattr(daemon_module, "_handle_auth_failure", _recover)
    monkeypatch.setattr(daemon_module, "_mint_scoped_api_key", _mint)

    result = await daemon.run_session(
        "auth-recovery-test",
        "system",
        "prompt",
        mcp_config_override={
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {
                    "Authorization": "Bearer old-key",
                    "X-Lucent-Memory-Scope": "user",
                    "X-Lucent-Memory-Scope-User-Id": "user-1",
                    "X-Lucent-Org-Id": "org-1",
                },
                "tools": ["*"],
            },
            "other": {"type": "http", "url": "http://other"},
        },
    )

    assert result == "ok"
    assert call_count["n"] == 2
    assert recover_calls["n"] == 0
    assert seen_headers == ["Bearer old-key", "Bearer hs_scoped_new"]
    assert seen_scope_headers == [
        {"scope": "user", "scope_user": "user-1", "org_id": "org-1"},
        {"scope": "user", "scope_user": "user-1", "org_id": "org-1"},
    ]
    assert mint_calls == [
        {
            "memory_scope": "user",
            "memory_scope_user_id": "user-1",
            "org_id": "org-1",
            "ttl_minutes": 60,
        }
    ]


@pytest.mark.asyncio
async def test_run_session_refuses_unscoped_memory_override_after_auth_failure(
    monkeypatch,
):
    daemon = LucentDaemon()
    daemon.instance_id = "instance-refuse-unscoped"

    call_count = {"n": 0}

    async def _inner(_name, _system, _prompt, model=None, mcp_config_override=None, **_kwargs):
        call_count["n"] += 1
        raise AuthFailureDetectedError("Unauthorized: Invalid or expired credentials")

    recover_calls = {"n": 0}

    async def _recover(_instance_id: str, *, force_rotate: bool = False) -> bool:
        recover_calls["n"] += 1
        return True

    monkeypatch.setattr(daemon, "_run_session_inner", _inner)
    monkeypatch.setattr(daemon_module, "_handle_auth_failure", _recover)

    result = await daemon.run_session(
        "auth-unscoped-override-test",
        "system",
        "prompt",
        mcp_config_override={
            "memory-server": {
                "type": "http",
                "url": "http://mcp",
                "headers": {"Authorization": "Bearer old-key"},
                "tools": ["*"],
            }
        },
    )

    assert result is None
    assert call_count["n"] == 1
    assert recover_calls["n"] == 0


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
