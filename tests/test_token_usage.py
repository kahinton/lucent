"""Integration tests for provider-reported LLM token usage reporting."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.api.routers import chat
from lucent.db import UserRepository
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.token_usage import TokenUsageRepository
from lucent.llm.engine import SessionEvent, SessionEventType


def _current(user):
    return CurrentUser(
        id=user["id"],
        organization_id=user["organization_id"],
        role=user["role"],
        email=user.get("email"),
        display_name=user.get("display_name"),
        auth_method="api_key",
    )


@pytest.mark.asyncio
async def test_token_usage_report_aggregates_provider_calls_and_deduplicates(
    db_pool, test_user, test_organization
):
    repo = TokenUsageRepository(db_pool)
    session = await LLMSessionRepository(db_pool).create_session(
        org_id=test_organization["id"], user_id=test_user["id"], model="model-a"
    )
    common = {
        "organization_id": test_organization["id"],
        "user_id": test_user["id"],
        "session_id": session["id"],
        "turn_id": None,
        "message_id": None,
        "model": "model-a",
        "engine": "copilot",
    }
    usage = {
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_read_tokens": 10,
        "cache_write_tokens": 2,
        "reasoning_tokens": 5,
        "provider_call_id": "provider-call-1",
    }
    try:
        recorded = await repo.record(**common, usage=usage)
        duplicate = await repo.record(**common, usage=usage)
        report = await repo.get_report(test_organization["id"], user_id=test_user["id"])
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM llm_token_usage WHERE organization_id = $1", test_organization["id"]
            )

    assert recorded["input_tokens"] == 100
    assert duplicate == {}
    assert report["summary"] == {
        "call_count": 1,
        "input_tokens": 100,
        "uncached_input_tokens": 90,
        "output_tokens": 25,
        "cache_read_tokens": 10,
        "cache_write_tokens": 2,
        "reasoning_tokens": 5,
    }
    assert report["by_model"][0]["model"] == "model-a"


@pytest.mark.asyncio
async def test_token_usage_keeps_null_provider_ids_and_honors_time_bounds(
    db_pool, test_user, test_organization
):
    repo = TokenUsageRepository(db_pool)
    now = datetime.now(timezone.utc)
    record_args = {
        "organization_id": test_organization["id"],
        "user_id": test_user["id"],
        "session_id": None,
        "turn_id": None,
        "message_id": None,
        "model": "model-a",
        "engine": "langchain",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    try:
        in_range_record = await repo.record(**record_args)
        await repo.record(**record_args)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE llm_token_usage SET created_at = $1 "
                "WHERE organization_id = $2 AND model = 'model-a'",
                now - timedelta(days=2),
                test_organization["id"],
            )
            await conn.execute(
                "UPDATE llm_token_usage SET created_at = $1 "
                "WHERE id = $2",
                now,
                in_range_record["id"],
            )
        report = await repo.get_report(
            test_organization["id"],
            user_id=test_user["id"],
            starts_at=now,
            ends_at=now + timedelta(seconds=1),
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM llm_token_usage WHERE organization_id = $1", test_organization["id"]
            )

    assert report["summary"]["call_count"] == 1
    assert report["summary"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_token_usage_api_scopes_member_and_admin_reports(
    db_pool, test_user, test_organization, clean_test_data
):
    other_user = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}usage-other",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}usage-other@test.com",
        display_name="Usage Other",
    )
    admin = await UserRepository(db_pool).create(
        external_id=f"{clean_test_data}usage-admin",
        provider="local",
        organization_id=test_organization["id"],
        email=f"{clean_test_data}usage-admin@test.com",
        display_name="Usage Admin",
        role="admin",
    )
    repo = TokenUsageRepository(db_pool)
    now = datetime.now(timezone.utc)
    try:
        for user, model, input_tokens in (
            (test_user, "model-a", 100),
            (other_user, "model-b", 200),
        ):
            await repo.record(
                organization_id=test_organization["id"],
                user_id=user["id"],
                session_id=None,
                turn_id=None,
                message_id=None,
                model=model,
                engine="langchain",
                usage={"input_tokens": input_tokens, "output_tokens": 10},
            )
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: _current(test_user)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            own = await client.get(
                "/api/usage/tokens",
                params={"starts_at": (now - timedelta(days=1)).isoformat()},
            )
            denied = await client.get("/api/usage/tokens?organization_wide=true")
            app.dependency_overrides[get_current_user] = lambda: _current(admin)
            organization = await client.get("/api/usage/tokens?organization_wide=true")
    finally:
        app.dependency_overrides.clear()
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM llm_token_usage WHERE organization_id = $1", test_organization["id"]
            )

    assert own.status_code == 200
    assert own.json()["scope"] == "user"
    assert own.json()["summary"]["input_tokens"] == 100
    assert denied.status_code == 403
    assert organization.status_code == 200
    assert organization.json()["summary"]["input_tokens"] == 300
    assert {item["model"] for item in organization.json()["by_model"]} == {"model-a", "model-b"}
    assert {item["user_name"] for item in organization.json()["by_user_model"]} == {
        test_user["display_name"],
        "Usage Other",
    }


@pytest.mark.asyncio
async def test_chat_stream_usage_event_is_persisted(
    db_pool, test_user, test_organization, monkeypatch
):
    class UsageEngine:
        name = "langchain"

        async def run_session_streaming(self, **kwargs):
            kwargs["on_event"](
                SessionEvent(
                    type=SessionEventType.USAGE,
                    usage={"input_tokens": 75, "output_tokens": 15},
                )
            )
            return None

    async def fake_user(_request):
        return test_user, db_pool

    async def allow_model(*_args):
        return True

    async def system_prompt(*_args):
        return "system"

    async def skip_experience(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat, "_get_session_user", fake_user)
    monkeypatch.setattr(chat, "_can_user_access_model", allow_model)
    monkeypatch.setattr(chat, "_build_system_prompt", system_prompt)
    monkeypatch.setattr(chat, "_maybe_capture_session_experience", skip_experience)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", lambda _model: UsageEngine())
    try:
        response = await chat.chat_stream_v2(
            type("Request", (), {"cookies": {}})(),
            chat.ChatStreamRequest(
                messages=[chat.ChatMessage(role="user", content="hello")],
                model="model-a",
            ),
        )
        async for _chunk in response.body_iterator:
            pass
        report = await TokenUsageRepository(db_pool).get_report(
            test_organization["id"], user_id=test_user["id"]
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM llm_token_usage WHERE organization_id = $1", test_organization["id"]
            )

    assert report["summary"]["input_tokens"] == 75
    assert report["summary"]["output_tokens"] == 15
    assert report["by_model"][0]["model"] == "model-a"


@pytest.mark.asyncio
async def test_legacy_chat_stream_usage_event_is_persisted(
    db_pool, test_user, test_organization, monkeypatch
):
    class UsageEngine:
        name = "langchain"

        async def run_session_streaming(self, **kwargs):
            kwargs["on_event"](
                SessionEvent(
                    type=SessionEventType.USAGE,
                    usage={"input_tokens": 60, "output_tokens": 12},
                )
            )
            return "done"

    async def fake_user(_request):
        return test_user, db_pool

    async def allow_model(*_args):
        return True

    async def system_prompt(*_args):
        return "system"

    async def skip_experience(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat, "_get_session_user", fake_user)
    monkeypatch.setattr(chat, "_can_user_access_model", allow_model)
    monkeypatch.setattr(chat, "_build_system_prompt", system_prompt)
    monkeypatch.setattr(chat, "_maybe_capture_session_experience", skip_experience)
    monkeypatch.setattr("lucent.model_registry.validate_model", lambda _model: None)
    monkeypatch.setattr("lucent.llm.get_engine_for_model", lambda _model: UsageEngine())
    try:
        await chat.chat_stream(
            type("Request", (), {"cookies": {}})(),
            chat.ChatRequest(
                messages=[chat.ChatMessage(role="user", content="hello")],
                model="model-a",
            ),
        )
        report = await TokenUsageRepository(db_pool).get_report(
            test_organization["id"], user_id=test_user["id"]
        )
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM llm_token_usage WHERE organization_id = $1", test_organization["id"]
            )

    assert report["summary"]["input_tokens"] == 60
    assert report["summary"]["output_tokens"] == 12
