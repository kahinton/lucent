"""Chat API endpoint — streaming LLM conversations with page context.

Uses the configured LLM engine (Copilot SDK or LangChain) for
responses. Each message includes page context and relevant
memories for grounded answers. The chat agent has full access to the
Lucent MCP server for memory search, creation, and management.
"""

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lucent.auth_providers import SESSION_COOKIE_NAME, validate_session
from lucent.db import get_pool
from lucent.logging import get_logger

logger = get_logger("chat")

router = APIRouter(prefix="/chat", tags=["chat"])

CHAT_MODEL = os.environ.get("LUCENT_CHAT_MODEL", "claude-opus-4.6")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
# MCP server URL — localhost inside the container, configurable for external
MCP_URL = os.environ.get("LUCENT_CHAT_MCP_URL", "http://localhost:8766/mcp")
# Session timeout for chat (shorter than daemon — chat should be snappy)
CHAT_SESSION_TIMEOUT = int(os.environ.get("LUCENT_CHAT_TIMEOUT", "300"))


class ChatMessage(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    page_context: dict | None = None  # {url, title, type, data}
    model: str | None = None  # override the default chat model


async def _get_session_user(request: Request):
    """Authenticate via session cookie (same as web routes)."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(401, "Not authenticated")
    logger.debug("Session token for chat: len=%d prefix=%s", len(session_token), session_token[:8])
    pool = await get_pool()
    user = await validate_session(pool, session_token)
    if not user:
        raise HTTPException(401, "Session expired")
    return user, pool


def _build_mcp_config(session_token: str) -> dict:
    """Build MCP server config using the user's session token.

    The MCPAuthMiddleware accepts session tokens via Bearer auth,
    so the user's own identity flows through to MCP operations.
    """
    return {
        "memory-server": {
            "type": "http",
            "url": MCP_URL,
            "headers": {"Authorization": f"Bearer {session_token}"},
            "tools": ["*"],
        },
    }


async def _build_system_prompt(user: dict, pool, page_context: dict | None) -> str:
    """Build a system prompt with page context and relevant memories."""
    display_name = user.get("display_name", "a user")
    org_id = str(user["organization_id"]) if user.get("organization_id") else None
    parts = [
        "You are Lucent, an AI assistant embedded in the Lucent Memory System web interface.",
        f"You're talking with {display_name}.",
        "Be helpful, concise, and knowledgeable about the system they're using.",
        "You have access to context about what the user is currently viewing.",
        "You also have access to the Lucent MCP server (memory-server) — "
        "use it to search, create, update, and manage memories when the user asks.",
        "When the user asks about their memories, use the MCP tools to search and "
        "retrieve accurate information rather than relying only on the page context.",
        "Answer questions about their data, explain what they're seeing, "
        "and help them use the system.",
        "Format responses in markdown when helpful. Keep answers focused and practical.",
        f"Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
    ]

    # Add page context
    if page_context:
        url = page_context.get("url", "")
        title = page_context.get("title", "")
        page_type = page_context.get("type", "unknown")
        page_data = page_context.get("data", {})

        parts.append("\n## Current Page Context")
        parts.append(f"The user is viewing: **{title}** ({url})")
        parts.append(f"Page type: {page_type}")

        if page_data:
            parts.append(
                f"Page data:\n```json\n{json.dumps(page_data, default=str, indent=2)[:3000]}\n```"
            )

        # Load deep context for detail pages
        try:
            if page_type == "memory_detail" and page_data.get("memory_id") and org_id:
                from lucent.db import MemoryRepository

                repo = MemoryRepository(pool)
                mem = await repo.get_memory(page_data["memory_id"], org_id)
                if mem:
                    parts.append("\n## Memory Being Viewed")
                    parts.append(f"- Type: {mem.get('type', '?')}")
                    parts.append(f"- Tags: {', '.join(mem.get('tags', []))}")
                    parts.append(f"- Importance: {mem.get('importance', '?')}/10")
                    parts.append(f"- Content:\n{str(mem.get('content', ''))[:2000]}")

            elif page_type == "schedule_detail" and page_data.get("schedule_id") and org_id:
                from lucent.db.schedules import ScheduleRepository

                repo = ScheduleRepository(pool)
                sched = await repo.get_schedule_with_runs(page_data["schedule_id"], org_id)
                if sched:
                    parts.append("\n## Schedule Being Viewed")
                    parts.append(f"- Title: {sched.get('title')}")
                    parts.append(f"- Type: {sched.get('schedule_type')}")
                    parts.append(f"- Status: {sched.get('status')}")
                    parts.append(f"- Agent: {sched.get('agent_type', '?')}")
                    parts.append(f"- Description: {sched.get('description', '')[:500]}")
                    runs = sched.get("runs", [])
                    if runs:
                        parts.append(f"- Total runs: {len(runs)}")
                        latest = runs[0]
                        parts.append(
                            f"- Latest run: {latest.get('status')} — "
                            f"{str(latest.get('result_summary', ''))[:300]}"
                        )

            elif page_type == "request_detail" and page_data.get("request_id") and org_id:
                from lucent.db.requests import RequestRepository

                repo = RequestRepository(pool)
                req = await repo.get_request(page_data["request_id"], org_id)
                if req:
                    parts.append("\n## Request Being Viewed")
                    parts.append(f"- Title: {req.get('title')}")
                    parts.append(f"- Status: {req.get('status')}")
                    parts.append(f"- Priority: {req.get('priority')}")
                    parts.append(f"- Source: {req.get('source')}")
                    tasks = await repo.list_tasks(page_data["request_id"])
                    if tasks:
                        parts.append(f"- Tasks: {len(tasks)}")
                        for t in tasks[:5]:
                            parts.append(f"  - [{t.get('status')}] {t.get('title', '?')}")
        except Exception:
            pass  # Don't fail chat for context loading errors

    # Pull relevant memories for context
    try:
        from lucent.db import MemoryRepository

        memo_repo = MemoryRepository(pool)
        if org_id:
            recent = await memo_repo.list_memories(
                org_id=org_id,
                limit=10,
            )
            if recent:
                parts.append("\n## Recent Memories (for context)")
                for m in recent[:10]:
                    tags = ", ".join(m.get("tags", [])[:5])
                    content_preview = str(m.get("content", ""))[:200]
                    parts.append(f"- [{tags}] {content_preview}")
    except Exception:
        pass  # Don't fail chat if memory lookup fails

    return "\n".join(parts)


@router.post("/stream")
async def chat_stream(
    request: Request,
    body: ChatRequest,
):
    """Stream a chat response. Model defaults to CHAT_MODEL but can be overridden per-request."""
    user, pool = await _get_session_user(request)

    from lucent.llm import get_engine

    engine = get_engine()

    selected_model = body.model or CHAT_MODEL

    system_prompt = await _build_system_prompt(user, pool, body.page_context)

    # Build the conversation as a single prompt with history
    history_parts = []
    for m in body.messages[:-1]:  # All but the last message
        prefix = "User" if m.role == "user" else "Assistant"
        history_parts.append(f"{prefix}: {m.content}")

    last_message = body.messages[-1].content if body.messages else ""

    if history_parts:
        prompt = "Previous conversation:\n" + "\n".join(history_parts) + f"\n\nUser: {last_message}"
    else:
        prompt = last_message

    # Build MCP config using the user's session token
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    mcp_config = _build_mcp_config(session_token) if session_token else {}
    logger.info(
        "Chat session: engine=%s, model=%s, mcp=%s",
        engine.name,
        selected_model,
        "present" if session_token else "MISSING",
    )

    # Run the LLM session via the engine abstraction
    try:
        result_text = await engine.run_session(
            model=selected_model,
            system_message=system_prompt,
            prompt=prompt,
            mcp_config=mcp_config,
            timeout=CHAT_SESSION_TIMEOUT,
        )
        error = None if result_text else "No response received"
    except Exception as e:
        logger.error("Chat session failed: %s", e)
        result_text = None
        error = str(e)

    # Return SSE response
    async def generate():
        if error:
            yield f"data: {json.dumps({'type': 'error', 'error': error})}\n\n"
        elif result_text:
            yield f"data: {json.dumps({'type': 'text', 'text': result_text})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'error': 'No response received'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/models")
async def chat_models(request: Request):
    """List available models for the chat model picker."""
    # Require a valid session to prevent unauthenticated probing
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from lucent.model_registry import list_models as registry_list_models

    models = registry_list_models()
    return {
        "default": CHAT_MODEL,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "provider": m.provider,
                "category": m.category,
            }
            for m in models
        ],
    }


@router.get("/status")
async def chat_status(request: Request):
    """Check if chat is configured and available."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from lucent.llm import get_engine_name

    return {
        "available": True,
        "model": CHAT_MODEL,
        "engine": get_engine_name(),
    }
