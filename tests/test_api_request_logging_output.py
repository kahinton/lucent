"""Tests for request/user context fields in emitted API log lines."""

import asyncio
import io
import json
import logging
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import AuthenticatedUser, CurrentUser
from lucent.log_context import clear_log_context, get_request_id, get_user_id
from lucent.logging import JSONFormatter, get_logger


@pytest.fixture
def captured_json_logger():
    """Attach a JSON formatter handler to an isolated test logger."""
    logger = get_logger("test.request_logging")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())

    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate

    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        yield logger, stream
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        clear_log_context()


def _json_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_json_logs_include_request_id_and_honor_supplied_x_request_id(captured_json_logger):
    logger, stream = captured_json_logger
    app = create_app()

    @app.get("/api/_test/log-request-id")
    async def log_request_id_probe():
        logger.info("request-id probe")
        return {"request_id": get_request_id()}

    supplied_request_id = "req-from-client-abc"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/_test/log-request-id",
            headers={"X-Request-ID": supplied_request_id},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == supplied_request_id
    assert response.json()["request_id"] == supplied_request_id

    lines = _json_lines(stream)
    assert len(lines) == 1
    assert lines[0]["request_id"] == supplied_request_id


@pytest.mark.asyncio
async def test_authenticated_request_logs_include_user_id(monkeypatch, captured_json_logger):
    logger, stream = captured_json_logger
    app = create_app()

    fake_user_id = uuid4()
    fake_org_id = uuid4()

    async def fake_authenticate_with_api_key(_authorization: str):
        return CurrentUser(
            id=fake_user_id,
            organization_id=fake_org_id,
            role="member",
            email="test@example.com",
            display_name="Test User",
            auth_method="api_key",
            api_key_scopes=["read", "write"],
        )

    monkeypatch.setattr(
        "lucent.api.deps._authenticate_with_api_key",
        fake_authenticate_with_api_key,
    )

    @app.get("/api/_test/log-auth")
    async def log_auth_probe(_user: AuthenticatedUser):
        logger.info("auth probe")
        return {"request_id": get_request_id(), "user_id": get_user_id()}

    supplied_request_id = "req-auth-123"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/_test/log-auth",
            headers={
                "Authorization": "Bearer hs_fake",
                "X-Request-ID": supplied_request_id,
            },
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == supplied_request_id
    assert response.json()["request_id"] == supplied_request_id
    assert response.json()["user_id"] == str(fake_user_id)

    lines = _json_lines(stream)
    assert len(lines) == 1
    assert lines[0]["request_id"] == supplied_request_id
    assert lines[0]["user_id"] == str(fake_user_id)


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_bleed_request_context(captured_json_logger):
    logger, stream = captured_json_logger
    app = create_app()

    @app.get("/api/_test/log-concurrent")
    async def log_concurrent_probe(slot: str):
        logger.info("concurrent probe", extra={"slot": slot})
        return {"slot": slot, "request_id": get_request_id(), "user_id": get_user_id()}

    req_a = "req-concurrent-a"
    req_b = "req-concurrent-b"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.get(
                "/api/_test/log-concurrent",
                params={"slot": "a"},
                headers={"X-Request-ID": req_a},
            ),
            client.get(
                "/api/_test/log-concurrent",
                params={"slot": "b"},
                headers={"X-Request-ID": req_b},
            ),
        )

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.json()["request_id"] == req_a
    assert resp_b.json()["request_id"] == req_b

    lines = _json_lines(stream)
    assert len(lines) == 2
    by_slot = {str(line["slot"]): str(line["request_id"]) for line in lines}
    assert by_slot == {"a": req_a, "b": req_b}


@pytest.mark.asyncio
async def test_request_context_cleared_after_request_completion(captured_json_logger):
    logger, stream = captured_json_logger
    app = create_app()

    @app.get("/api/_test/log-clear")
    async def log_clear_probe():
        logger.info("in-request log")
        return {"request_id": get_request_id(), "user_id": get_user_id()}

    supplied_request_id = "req-clear-001"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/_test/log-clear",
            headers={"X-Request-ID": supplied_request_id},
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == supplied_request_id

    # Once request handling has completed, contextvars should be clear.
    assert get_request_id() is None
    assert get_user_id() is None

    logger.info("post-request log")
    lines = _json_lines(stream)
    assert len(lines) == 2

    in_request_line = lines[0]
    post_request_line = lines[1]

    assert in_request_line["request_id"] == supplied_request_id
    assert "user_id" not in in_request_line

    assert "request_id" not in post_request_line
    assert "user_id" not in post_request_line
