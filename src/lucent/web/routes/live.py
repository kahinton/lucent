"""Lightweight live-state data for authenticated web pages."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from lucent.db import get_pool
from lucent.web.live_events import live_event_broker
from lucent.web.routes._shared import get_user_context

router = APIRouter()


@router.get("/ui/events")
async def live_events(request: Request) -> StreamingResponse:
    """Stream scoped refresh signals to the current browser session."""
    user = await get_user_context(request)
    organization_id = str(user.organization_id)
    user_id = str(user.id)

    async def stream() -> object:
        async with live_event_broker.subscribe(organization_id, user_id) as queue:
            yield "event: connected\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    await asyncio.wait_for(queue.get(), timeout=25)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: refresh\ndata: {json.dumps({'type': 'refresh'})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/ui/live-status")
async def live_status(request: Request) -> JSONResponse:
    """Return the current user's navigation attention counts without caching."""
    user = await get_user_context(request)
    pool = await get_pool()
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    org_id = str(user.organization_id)
    user_id = str(user.id)

    from lucent.db.definitions import DefinitionRepository
    from lucent.db.files import UserFileRepository
    from lucent.db.requests import RequestRepository
    from lucent.db.user_interactions import UserInteractionRepository

    request_repo = RequestRepository(pool)
    counts = {
        "activity": await request_repo.count_pending_approvals(
            org_id=org_id,
            requester_user_id=user_id,
            include_system=role in {"admin", "owner"},
        ),
        "definitions": await DefinitionRepository(pool).count_pending_proposals(
            org_id=org_id,
            requester_user_id=user_id,
            requester_role=role,
        ),
        "files": await UserFileRepository(pool).count_unseen_current_revisions(
            org_id, user_id
        ),
        "handoffs": await UserInteractionRepository(pool).count_attention_needed(
            org_id=org_id,
            user_id=user_id,
        ),
    }
    return JSONResponse({"badges": counts}, headers={"Cache-Control": "no-store"})