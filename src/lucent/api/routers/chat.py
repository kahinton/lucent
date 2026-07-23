"""Chat API endpoint — streaming LLM conversations with page context.

Uses the configured LLM engine (Copilot SDK or LangChain) for
responses. Each message includes page context and relevant
memories for grounded answers. The chat agent has full access to the
Lucent MCP server for memory search, creation, and management.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lucent.auth_providers import SESSION_COOKIE_NAME, validate_session
from lucent.db import UserRepository, get_pool
from lucent.logging import get_logger
from lucent.mcp_config import build_internal_mcp_server
from lucent.prompts.memory_usage import render_active_user_context
from lucent.settings import (
    chat_mcp_url,
    chat_model_id,
    chat_timeout_seconds,
    session_experience_model_id,
    session_experience_summary_enabled,
    session_experience_timeout_seconds,
)
from lucent.tool_policy import (
    CHAT_ALLOWED_TOOLS,
    chat_allowed_tools_for_agent,
)

logger = get_logger("chat")

router = APIRouter(prefix="/chat", tags=["chat"])

# Compatibility constants for tests and older imports. Settings helpers remain
# the source of the initial values, but callers can monkeypatch these module
# attributes in unit tests.
SESSION_EXPERIENCE_SUMMARY_ENABLED = session_experience_summary_enabled()
SESSION_EXPERIENCE_MODEL = session_experience_model_id()
SESSION_EXPERIENCE_TIMEOUT = session_experience_timeout_seconds()


async def _can_user_access_model(user, pool, model_id: str) -> bool:
    from lucent.access_control import AccessControlService

    return await AccessControlService(pool).can_access(
        str(user["id"]), "model", model_id, str(user["organization_id"])
    )


def _resolve_chat_model(override: str | None = None) -> str:
    from lucent.model_registry import get_default_model_id

    if override:
        return override
    return get_default_model_id(preferred_model=chat_model_id())


def _chat_allowed_tools_for_agent(
    agent_name: str | None = None,
    skill_names: list[str] | None = None,
) -> list[str]:
    """Return the MCP tool allow-list for a chat session.

    General chat stays intentionally narrow. Specialized composer tools are
    added only when the selected chat agent is an approved composer role (or an
    equivalent agent explicitly granted the corresponding skill).
    """
    return chat_allowed_tools_for_agent(agent_name, skill_names)


class ChatAttachment(BaseModel):
    """A multimodal attachment (image or document) on a user message.

    ``data`` may be raw base64 or a ``data:`` URL; both are normalized and
    validated server-side in ``lucent.llm.attachments``.
    """

    mime_type: str | None = Field(default=None, max_length=128)
    data: str
    name: str | None = Field(default=None, max_length=256)
    kind: str | None = Field(default=None, pattern=r"^(image|document)$")
    size: int | None = None


class ChatMessage(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant)$")
    content: str
    attachments: list[ChatAttachment] | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    page_context: dict | None = None  # {url, title, type, data}
    model: str | None = None  # override the default chat model
    reasoning_effort: str | None = Field(default=None, max_length=64)
    session_id: str | None = None
    surface: str = Field(default="embedded_chat", pattern=r"^(chat|embedded_chat)$")


class ChatStreamRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None
    reasoning_effort: str | None = Field(default=None, max_length=64)
    agent_id: str | None = None  # optional agent definition to use
    session_id: str | None = None
    surface: str = Field(default="chat", pattern=r"^(chat|embedded_chat)$")


class CreateChatSession(BaseModel):
    title: str | None = Field(default=None, max_length=256)
    kind: str = Field(default="chat", pattern=r"^(chat|embedded_chat)$")
    model: str | None = None
    reasoning_effort: str | None = Field(default=None, max_length=64)
    agent_id: str | None = None
    initial_message: str | None = Field(default=None, max_length=1000)


class UpdateChatSession(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    status: str | None = Field(default=None, pattern=r"^(active|idle|archived|deleted)$")


@dataclass
class PersistentChatSession:
    session_id: str | None
    provider_session_id: str | None
    provider_initialized: bool
    turn_id: str
    user_message_id: str | None
    previous_messages: list[dict[str, Any]]
    repo: Any | None
    session_metadata: dict[str, Any] = field(default_factory=dict)


async def _get_session_user(request: Request):
    """Authenticate via session cookie (same as web routes)."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(401, "Not authenticated")
    logger.debug("Session token present for chat request")
    pool = await get_pool()
    user = await validate_session(pool, session_token)
    if not user:
        raise HTTPException(401, "Session expired")
    try:
        from lucent.auth import set_current_user

        set_current_user(user)
    except Exception:
        pass
    return user, pool


def _build_mcp_config(
    session_token: str,
    *,
    llm_session_id: str | None = None,
    llm_turn_id: str | None = None,
    llm_message_id: str | None = None,
    agent_definition_id: str | None = None,
    tools: list[str] | None = None,
) -> dict:
    """Build MCP server config using the user's session token.

    The MCPAuthMiddleware accepts session tokens via Bearer auth,
    so the user's own identity flows through to MCP operations.
    """
    headers = {}
    if llm_session_id:
        headers["X-Lucent-LLM-Session-Id"] = llm_session_id
    if llm_turn_id:
        headers["X-Lucent-LLM-Turn-Id"] = llm_turn_id
    if llm_message_id:
        headers["X-Lucent-LLM-Message-Id"] = llm_message_id
    if agent_definition_id:
        headers["X-Lucent-Agent-Definition-Id"] = agent_definition_id

    return {
        "memory-server": build_internal_mcp_server(
            url=chat_mcp_url(),
            bearer_token=session_token,
            tools=tools or CHAT_ALLOWED_TOOLS,
            extra_headers=headers,
        ),
    }



def _title_from_message(content: str | None) -> str | None:
    if not content:
        return None
    first_line = " ".join(content.strip().splitlines()[0:1]).strip()
    return first_line[:80] if first_line else None


def _history_prompt(history: list[dict[str, Any]], last_message: str) -> str:
    history_parts = []
    for m in history:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        prefix = "User" if role == "user" else "Assistant"
        history_parts.append(f"{prefix}: {m.get('content') or ''}")
    if history_parts:
        return "Previous conversation:\n" + "\n".join(history_parts) + f"\n\nUser: {last_message}"
    return last_message


def _message_history_for_engine(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"role": str(m.get("role")), "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]


def _chat_message_to_dict(message: ChatMessage) -> dict[str, Any]:
    """Pydantic v1/v2 compatible ChatMessage serialization."""
    result: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.attachments:
        result["attachments"] = [a.model_dump(exclude_none=True) for a in message.attachments]
    return result


def _normalize_last_attachments(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Validate and normalize attachments on the final user message.

    Returns an empty list when there are none. Raises HTTP 400 on invalid
    payloads so the client gets a clear error instead of a silent failure.
    """
    if not messages or not messages[-1].attachments:
        return []
    from lucent.llm.attachments import AttachmentError, normalize_attachments

    raw = [a.model_dump(exclude_none=True) for a in messages[-1].attachments]
    try:
        return normalize_attachments(raw)
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



def _event_raw(event) -> dict[str, Any]:
    """Build a small JSON-safe raw event summary for persistence."""
    return {
        "type": event.type.value if hasattr(event.type, "value") else str(event.type),
        "content": event.content,
        "tool_name": event.tool_name,
        "tool_input": event.tool_input,
        "tool_output": event.tool_output,
    }


@lru_cache(maxsize=1)
def _session_experience_skill_content() -> str:
    skill_path = (
        Path(__file__).resolve().parents[4]
        / ".github"
        / "skills"
        / "session-experience-capture"
        / "SKILL.md"
    )
    try:
        return skill_path.read_text(encoding="utf-8")
    except OSError:
        return (
            "# Session Experience Capture\n\n"
            "Write a concise experience memory with Session Summary, What Happened, "
            "Why It Matters, and Follow-up. Return NO_EXPERIENCE_NEEDED for trivial chats."
        )


def _format_session_experience_context(session: dict, evaluation: dict[str, Any]) -> str:
    messages = session.get("messages") or []
    events = session.get("events") or []
    requests = session.get("requests") or []
    lines = [
        "## Session Metadata",
        f"- Session ID: {session.get('id')}",
        f"- Title: {session.get('title') or 'Untitled'}",
        f"- Kind: {session.get('kind')}",
        f"- Capture score: {evaluation.get('score')}",
        f"- Capture reasons: {', '.join(evaluation.get('reasons') or []) or 'none'}",
        "",
        "## Linked Requests",
    ]
    if requests:
        for req in requests[:20]:
            lines.append(
                f"- {req.get('request_title') or req.get('request_id')} "
                f"(id={req.get('request_id')}, status={req.get('request_status')}, "
                f"relation={req.get('relation')})"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Tool Events"])
    visible_events = [e for e in events if e.get("tool_name") or e.get("event_type")]
    if visible_events:
        for event in visible_events[-60:]:
            tool = event.get("tool_name") or event.get("event_type")
            detail = event.get("detail") or ""
            tool_input = event.get("tool_input")
            tool_output = event.get("tool_output")
            lines.append(f"- {tool}: {detail}".strip())
            if tool_input is not None:
                lines.append(f"  input: {str(tool_input)[:500]}")
            if tool_output is not None:
                lines.append(f"  output: {str(tool_output)[:500]}")
    else:
        lines.append("- None")

    lines.extend(["", "## Transcript"])
    for message in messages[-80:]:
        role = str(message.get("role") or "unknown").upper()
        content = str(message.get("content") or "")
        lines.append(f"\n### {role}\n{content}")
    return "\n".join(lines)


async def _summarize_session_experience(
    *,
    session: dict,
    evaluation: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Return model-written experience content, model id, and error if any."""
    if not SESSION_EXPERIENCE_SUMMARY_ENABLED:
        return None, None, "disabled"
    try:
        from lucent.llm import get_engine_for_model
        from lucent.model_registry import validate_model

        model = _resolve_chat_model(SESSION_EXPERIENCE_MODEL)
        validation_error = validate_model(model)
        if validation_error:
            return None, model, validation_error
        engine = get_engine_for_model(model)
        system_message = (
            "You summarize meaningful Lucent sessions into durable experience memories.\n\n"
            "Follow this skill exactly:\n\n"
            f"{_session_experience_skill_content()}"
        )
        prompt = (
            "Summarize this session into an experience memory. Use the skill's exact "
            "memory content structure. If it is not worth capturing, return "
            "NO_EXPERIENCE_NEEDED.\n\n"
            f"{_format_session_experience_context(session, evaluation)}"
        )
        result = await engine.run_session(
            model=model,
            system_message=system_message,
            prompt=prompt,
            mcp_config={},
            timeout=SESSION_EXPERIENCE_TIMEOUT,
            audit_context={
                "source": "chat.session_experience_summary",
                "organization_id": str(session.get("organization_id") or ""),
                "session_id": str(session.get("id") or ""),
                "model": model,
                "engine": engine.name,
            },
        )
        content = (result or "").strip()
        if not content or content == "NO_EXPERIENCE_NEEDED":
            return None, model, "no_experience_needed"
        return content, model, None
    except Exception as exc:
        logger.warning("Session experience model summary failed", exc_info=True)
        return None, SESSION_EXPERIENCE_MODEL, str(exc)[:500]


async def _prepare_persistent_chat_session(
    *,
    user: dict,
    pool,
    session_id: str | None,
    kind: str,
    engine_name: str,
    model: str,
    reasoning_effort: str | None,
    agent_id: str | None = None,
    last_message: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> PersistentChatSession:
    """Create/load a DB-backed chat session and persist the current user turn.

    If persistence is unavailable and no explicit session_id was supplied,
    the chat may continue ephemerally. An explicit session_id must resolve to
    a real session so users do not accidentally talk in the wrong thread.
    """
    turn_id = str(uuid4())
    try:
        from lucent.db.llm_sessions import LLMSessionRepository

        repo = LLMSessionRepository(pool)
        org_id = str(user["organization_id"])
        user_id = str(user["id"])
        if session_id:
            session = await repo.get_session(session_id, org_id, user_id=user_id)
            if not session:
                raise HTTPException(status_code=404, detail="Chat session not found")
            await repo.update_session(
                session_id,
                org_id,
                user_id=user_id,
                engine=engine_name,
                model=model,
                reasoning_effort=reasoning_effort or "",
                agent_definition_id=agent_id,
                status="active",
            )
        else:
            provider_session_id = str(uuid4()) if engine_name == "copilot" else None
            session = await repo.create_session(
                org_id=org_id,
                user_id=user_id,
                kind=kind,
                title=_title_from_message(last_message),
                engine=engine_name,
                model=model,
                reasoning_effort=reasoning_effort,
                agent_definition_id=agent_id,
                provider_session_id=provider_session_id,
            )
            session_id = str(session["id"])

        provider_session_id = session.get("provider_session_id")
        if engine_name == "copilot" and not provider_session_id:
            provider_session_id = str(uuid4())
            session = await repo.update_session(
                session_id,
                org_id,
                user_id=user_id,
                provider_session_id=provider_session_id,
            ) or session

        previous_messages = await repo.list_messages(
            session_id,
            org_id,
            roles={"user", "assistant"},
            limit=200,
        )

        user_message_id = None
        if last_message or attachments:
            user_metadata: dict[str, Any] = {
                "surface": kind,
                "model": model,
                "agent_id": agent_id,
            }
            if attachments:
                from lucent.llm.attachments import get_attachment_store

                user_metadata["attachments"] = await get_attachment_store().persist(attachments)
            user_message = await repo.add_message(
                session_id,
                role="user",
                content=last_message,
                org_id=org_id,
                turn_id=turn_id,
                metadata=user_metadata,
            )
            user_message_id = str(user_message["id"])

        provider_metadata = session.get("provider_metadata") or {}
        session_metadata = (
            session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        )
        return PersistentChatSession(
            session_id=session_id,
            provider_session_id=provider_session_id,
            provider_initialized=bool(provider_metadata.get("provider_initialized")),
            turn_id=turn_id,
            user_message_id=user_message_id,
            previous_messages=previous_messages,
            session_metadata=session_metadata,
            repo=repo,
        )
    except HTTPException:
        raise
    except Exception:
        logger.warning("Chat persistence unavailable; continuing ephemerally", exc_info=True)
        if session_id:
            raise HTTPException(status_code=500, detail="Chat session persistence failed")
        return PersistentChatSession(
            session_id=None,
            provider_session_id=None,
            provider_initialized=False,
            turn_id=turn_id,
            user_message_id=None,
            previous_messages=[],
            session_metadata={},
            repo=None,
        )


async def _chat_session_defaults(
    *,
    user: dict,
    pool,
    session_id: str | None,
) -> dict[str, Any]:
    """Return saved model/agent defaults for an existing chat session."""
    if not session_id:
        return {}
    try:
        from lucent.db.llm_sessions import LLMSessionRepository

        session = await LLMSessionRepository(pool).get_session(
            session_id,
            str(user["organization_id"]),
            user_id=str(user["id"]),
        )
        if not session:
            return {}
        agent_id = session.get("agent_definition_id")
        return {
            "model": session.get("model"),
            "reasoning_effort": session.get("reasoning_effort"),
            "agent_id": str(agent_id) if agent_id else None,
            "metadata": (
                session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
            ),
        }
    except Exception:
        logger.debug("Failed to load chat session defaults", exc_info=True)
        return {}


async def _mirror_handoff_user_turn(
    *,
    user: dict,
    pool,
    chat_session: PersistentChatSession,
    content: str,
) -> None:
    """Mirror a session user turn to the owning Handoff, when applicable.

    The LLM session is the authoritative conversation transcript. Handoffs use
    this mirror only for status/attention bookkeeping and compatibility with
    existing Handoff thread storage.
    """
    interaction_id = str(chat_session.session_metadata.get("interaction_id") or "").strip()
    if not interaction_id or not chat_session.session_id or not chat_session.user_message_id:
        return
    if not content.strip():
        return
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                """SELECT 1
                   FROM user_interaction_messages
                   WHERE interaction_id = $1::uuid
                     AND metadata->>'llm_message_id' = $2
                   LIMIT 1""",
                interaction_id,
                chat_session.user_message_id,
            )
        if exists:
            return

        from lucent.db.user_interactions import UserInteractionRepository

        repo = UserInteractionRepository(pool)
        interaction = await repo.get_interaction(
            interaction_id,
            str(user["organization_id"]),
            user_id=str(user["id"]),
        )
        if not interaction:
            return
        await repo.add_message(
            interaction_id=interaction_id,
            org_id=str(user["organization_id"]),
            sender_type="user",
            sender_user_id=str(user["id"]),
            body=content,
            metadata={
                "source": "llm_session",
                "surface": "handoff",
                "llm_session_id": chat_session.session_id,
                "llm_message_id": chat_session.user_message_id,
                "llm_turn_id": chat_session.turn_id,
            },
        )
    except Exception:
        logger.warning("Failed to mirror Handoff session user turn", exc_info=True)


async def _maybe_capture_session_experience(
    *,
    user: dict,
    chat_session: PersistentChatSession,
) -> None:
    """Best-effort automatic experience capture for meaningful chat sessions."""
    if not chat_session.repo or not chat_session.session_id:
        return
    try:
        capture_context = await chat_session.repo.evaluate_experience_capture(
            chat_session.session_id,
            str(user["organization_id"]),
            user_id=str(user["id"]),
        )
        session = capture_context.get("session")
        evaluation = capture_context.get("evaluation") or {}
        summary_content = None
        summary_model = None
        summary_error = None
        summary_mode = "deterministic"
        if session and evaluation.get("should_capture"):
            summary_content, summary_model, summary_error = await _summarize_session_experience(
                session=session,
                evaluation=evaluation,
            )
            if summary_error == "no_experience_needed":
                raw_metadata = session.get("metadata")
                metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                await chat_session.repo.update_session(
                    chat_session.session_id,
                    str(user["organization_id"]),
                    user_id=str(user["id"]),
                    metadata={
                        **metadata,
                        "experience_capture": {
                            "status": "skipped",
                            "reason": "model_no_experience_needed",
                            "evaluated_at": datetime.now(timezone.utc).isoformat(),
                            "score": evaluation.get("score"),
                            "reasons": evaluation.get("reasons") or [],
                            "summary_model": summary_model,
                        },
                    },
                )
                logger.debug("Session experience capture skipped by summary model")
                return
            if summary_content:
                summary_mode = "model"
            elif summary_error == "disabled":
                summary_mode = "disabled"
            else:
                summary_mode = "deterministic_fallback"
        result = await chat_session.repo.maybe_capture_experience(
            chat_session.session_id,
            str(user["organization_id"]),
            user_id=str(user["id"]),
            content_override=summary_content,
            summary_mode=summary_mode,
            summary_model=summary_model,
            summary_error=summary_error,
        )
        logger.debug("Session experience capture result: %s", result)
    except Exception:
        logger.warning("Session experience capture failed", exc_info=True)


def _chat_tool_grounding_instructions() -> str:
    """Runtime instructions that keep chat answers grounded in actual tool results."""
    return (
        "## Tool and memory grounding\n"
        "You have access to MCP tools. For questions about the user's memories, "
        "use `get_current_user_context`, `search_memories`, `search_memories_full`, "
        "or `get_memory` as needed before answering. If a tool returns memory content "
        "or page context includes memory content, you may quote or summarize it for "
        "this authenticated user. If tools are unavailable or return an access error, "
        "say that plainly. Do not invent Lucent security policies, hidden rules, or "
        "confidentiality restrictions to explain missing information.\n"
        "For requests to queue or track daemon work, use `create_request` directly. "
        "Do not create a goal, memory, or other handoff record as a substitute for a "
        "request unless the user explicitly asks for that. If `create_request` is "
        "unavailable, say so and stop rather than inventing a workaround. Use "
        "`list_available_models` for model constraints instead of shell or database "
        "inspection. In web chat, prefer MCP tools and page context; do not run shell, "
        "git, grep, view, or direct database commands to operate Lucent."
    )


async def _render_active_user_context(user: dict, pool) -> str:
    """Load and render trusted context without making chat depend on memory I/O."""
    individual_memory = None
    try:
        individual_memory = await UserRepository(pool).get_individual_memory_for_user(user["id"])
    except Exception:
        logger.warning("Failed to load active user memory for chat", exc_info=True)
    return render_active_user_context(user, individual_memory)


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
        "use it to search, create, update, and manage memories when the user asks, "
        "and to create tracked requests when the user asks to queue daemon work.",
        "When the user asks about their memories, use the MCP tools to search and "
        "retrieve accurate information rather than relying only on the page context.",
        _chat_tool_grounding_instructions(),
        "Answer questions about their data, explain what they're seeing, "
        "and help them use the system.",
        "Format responses in markdown when helpful. Keep answers focused and practical.",
        f"Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        await _render_active_user_context(user, pool),
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
                    tasks = (await repo.list_tasks(page_data["request_id"]))["items"]
                    if tasks:
                        parts.append(f"- Tasks: {len(tasks)}")
                        for t in tasks[:5]:
                            parts.append(f"  - [{t.get('status')}] {t.get('title', '?')}")

            elif (
                page_type == "user_interaction_detail"
                and page_data.get("interaction_id")
                and org_id
            ):
                from lucent.db.user_interactions import UserInteractionRepository

                repo = UserInteractionRepository(pool)
                interaction = await repo.get_interaction(
                    page_data["interaction_id"],
                    org_id,
                    user_id=str(user["id"]),
                )
                if interaction:
                    parts.append(
                        "\n## Handoff Conversation Instructions\n"
                        "This page is an active Lucent conversation that began from a proactive "
                        "handoff. Respond interactively on the page; do not tell the user "
                        "to reply elsewhere or wait for a daemon cycle. If the user asks for "
                        "tracked follow-up work, use `create_request` and summarize "
                        "what was queued."
                    )
                    parts.append("\n## Handoff Being Viewed")
                    parts.append(f"- Title: {interaction.get('title')}")
                    parts.append(f"- Type: {interaction.get('interaction_type')}")
                    parts.append(f"- Status: {interaction.get('status')}")
                    parts.append(f"- Requires response: {interaction.get('requires_response')}")
                    if interaction.get("response_prompt"):
                        parts.append(f"- Response prompt: {interaction.get('response_prompt')}")
                    refs = interaction.get("references") or []
                    if refs:
                        parts.append("- Attached context:")
                        for ref in refs[:10]:
                            label = ref.get("label") or ref.get("reference_id") or ref.get("url")
                            parts.append(
                                f"  - {ref.get('reference_type')}: {label}"
                            )
                    messages = interaction.get("messages") or []
                    if messages:
                        parts.append("- Thread:")
                        for msg in messages[-10:]:
                            sender = msg.get("sender_type", "unknown")
                            content = str(msg.get("body") or "")[:1000]
                            parts.append(f"  - {sender}: {content}")
        except Exception:
            logger.debug("Failed to load request context for chat", exc_info=True)

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
        logger.debug("Failed to load memories for chat context", exc_info=True)

    return "\n".join(parts)


async def _load_default_chat_agent(repo, org_id: str) -> dict | None:
    """Resolve the default chat persona to the built-in `lucent` identity agent.

    When a user does not pick an agent, the chat should still be composed from
    the same definition the daemon uses for its core identity — so the default
    persona has the lucent agent's skills, managed tools, and hooks. Returns the
    full agent dict, or None if no active `lucent` agent exists.
    """
    try:
        result = await repo.list_agents(org_id, status="active")
        items = result.get("items", result) if isinstance(result, dict) else result
        for candidate in items or []:
            if (candidate.get("name") or "").strip().lower() == "lucent":
                return await repo.get_agent(str(candidate["id"]), org_id)
    except Exception:
        logger.debug("Failed to resolve default lucent chat agent", exc_info=True)
    return None


async def _load_agent_hooks(pool, agent_id: str | None) -> list[dict[str, Any]]:
    """Load active hook definitions granted to an agent, if any."""
    if not agent_id:
        return []
    try:
        from lucent.db.definitions import DefinitionRepository

        repo = DefinitionRepository(pool)
        return await repo.get_agent_hooks(agent_id)
    except Exception:
        logger.debug("Failed to load agent hooks", exc_info=True)
        return []


def _hook_event_payload(event: Any) -> dict[str, Any]:
    raw = event.raw if isinstance(event.raw, dict) else {}
    return {
        "type": "hook_context",
        "hook": raw.get("hook") or "hook",
        "text": event.content or "",
        "metadata": {k: v for k, v in raw.items() if k != "hook"},
    }


@router.get("/sessions")
async def list_chat_sessions(
    request: Request,
    kind: str | None = None,
    limit: int = 25,
    offset: int = 0,
    include_archived: bool = False,
):
    """List persisted chat sessions for the authenticated web user."""
    user, pool = await _get_session_user(request)
    from lucent.db.llm_sessions import LLMSessionRepository

    repo = LLMSessionRepository(pool)
    return await repo.list_sessions(
        str(user["organization_id"]),
        user_id=str(user["id"]),
        kind=kind,
        include_archived=include_archived,
        limit=min(max(limit, 1), 100),
        offset=max(offset, 0),
    )


@router.post("/sessions")
async def create_chat_session(request: Request, body: CreateChatSession):
    """Create a persisted chat session shell."""
    user, pool = await _get_session_user(request)
    from lucent.db.llm_sessions import LLMSessionRepository

    repo = LLMSessionRepository(pool)
    session = await repo.create_session(
        org_id=str(user["organization_id"]),
        user_id=str(user["id"]),
        kind=body.kind,
        title=body.title or _title_from_message(body.initial_message),
        model=body.model,
        reasoning_effort=body.reasoning_effort,
        agent_definition_id=body.agent_id,
    )
    return session


@router.get("/sessions/{session_id}")
async def get_chat_session(request: Request, session_id: str):
    """Load a persisted chat session with transcript and request links."""
    user, pool = await _get_session_user(request)
    from lucent.db.llm_sessions import LLMSessionRepository

    repo = LLMSessionRepository(pool)
    session = await repo.get_session_detail(
        session_id,
        str(user["organization_id"]),
        user_id=str(user["id"]),
        include_events=True,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


@router.patch("/sessions/{session_id}")
async def update_chat_session(
    request: Request,
    session_id: str,
    body: UpdateChatSession,
):
    """Rename or archive a persisted chat session."""
    user, pool = await _get_session_user(request)
    from lucent.db.llm_sessions import LLMSessionRepository

    repo = LLMSessionRepository(pool)
    session = await repo.update_session(
        session_id,
        str(user["organization_id"]),
        user_id=str(user["id"]),
        title=body.title,
        status=body.status,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


@router.post("/stream")
async def chat_stream(
    request: Request,
    body: ChatRequest,
):
    """Stream a chat response. Model defaults to CHAT_MODEL but can be overridden per-request."""
    user, pool = await _get_session_user(request)

    from lucent.llm import get_engine_for_model
    from lucent.model_registry import validate_model, validate_reasoning_effort

    session_defaults = await _chat_session_defaults(
        user=user,
        pool=pool,
        session_id=body.session_id,
    )
    selected_model = _resolve_chat_model(body.model or session_defaults.get("model"))
    reasoning_effort = (
        body.reasoning_effort
        if body.reasoning_effort is not None
        else session_defaults.get("reasoning_effort")
    )
    validation_error = validate_model(selected_model)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)
    if not await _can_user_access_model(user, pool, selected_model):
        raise HTTPException(status_code=403, detail="Model is not available to this user")
    effort_error = validate_reasoning_effort(selected_model, reasoning_effort)
    if effort_error:
        raise HTTPException(status_code=400, detail=effort_error)

    engine = get_engine_for_model(selected_model)

    system_prompt = await _build_system_prompt(user, pool, body.page_context)
    agent_hooks = await _load_agent_hooks(pool, None)
    last_message = body.messages[-1].content if body.messages else ""
    attachments = _normalize_last_attachments(body.messages)
    chat_session = await _prepare_persistent_chat_session(
        user=user,
        pool=pool,
        session_id=body.session_id,
        kind=body.surface,
        engine_name=engine.name,
        model=selected_model,
        reasoning_effort=reasoning_effort,
        last_message=last_message,
        attachments=attachments,
    )
    await _mirror_handoff_user_turn(
        user=user,
        pool=pool,
        chat_session=chat_session,
        content=last_message,
    )

    fallback_history = [_chat_message_to_dict(m) for m in body.messages[:-1]]
    persisted_history = chat_session.previous_messages or fallback_history
    resume_provider = bool(
        engine.name == "copilot"
        and chat_session.provider_session_id
        and chat_session.provider_initialized
    )
    prompt = last_message if resume_provider else _history_prompt(persisted_history, last_message)
    message_history = (
        _message_history_for_engine(chat_session.previous_messages)
        if engine.name == "langchain" and chat_session.previous_messages
        else None
    )

    # Build MCP config using the user's session token
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    mcp_config = (
        _build_mcp_config(
            session_token,
            llm_session_id=chat_session.session_id,
            llm_turn_id=chat_session.turn_id,
            llm_message_id=chat_session.user_message_id,
            agent_definition_id=session_defaults.get("agent_id"),
        )
        if session_token
        else {}
    )
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
            timeout=chat_timeout_seconds(),
            reasoning_effort=reasoning_effort,
            provider_session_id=chat_session.provider_session_id,
            resume=resume_provider,
            message_history=message_history,
            hooks=agent_hooks,
            approve_permissions=False,
            attachments=attachments,
            audit_context={
                "source": "chat.stream",
                "organization_id": str(user["organization_id"]),
                "user_id": str(user["id"]),
                "session_id": chat_session.session_id,
                "turn_id": chat_session.turn_id,
                "message_id": chat_session.user_message_id,
                "model": selected_model,
                "reasoning_effort": reasoning_effort,
                "engine": engine.name,
            },
        )
        error = None if result_text else "No response received"
        if result_text and chat_session.repo and chat_session.session_id:
            await chat_session.repo.add_message(
                chat_session.session_id,
                role="assistant",
                content=result_text,
                org_id=str(user["organization_id"]),
                turn_id=chat_session.turn_id,
                metadata={"model": selected_model, "engine": engine.name},
            )
            if engine.name == "copilot":
                await chat_session.repo.mark_provider_initialized(
                    chat_session.session_id,
                    str(user["organization_id"]),
                    provider_session_id=chat_session.provider_session_id,
                )
            await _maybe_capture_session_experience(user=user, chat_session=chat_session)
    except Exception as e:
        logger.error("Chat session failed: %s", e)
        result_text = None
        error = str(e)

    # Return SSE response
    async def generate():
        if chat_session.session_id:
            session_event = {"type": "session", "session_id": chat_session.session_id}
            yield f"data: {json.dumps(session_event)}\n\n"
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
    user, pool = await _get_session_user(request)
    from lucent.db.models import ModelRepository

    role = user.get("role", "member")
    if hasattr(role, "value"):
        role = role.value
    models = (
        await ModelRepository(pool).list_models_accessible_by(
            str(user["id"]),
            str(user["organization_id"]),
            requester_role=str(role),
        )
    )["items"]
    default_model = _resolve_chat_model()
    accessible_ids = {model["id"] for model in models}
    return {
        "default": (
            default_model
            if default_model in accessible_ids
            else next(iter(accessible_ids), None)
        ),
        "models": [
            {
                "id": model["id"],
                "name": model["name"],
                "provider": model["provider"],
                "category": model["category"],
                "reasoning_efforts": model.get("reasoning_efforts") or [],
                "supports_vision": bool(model.get("supports_vision")),
            }
            for model in models
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
        "model": _resolve_chat_model(),
        "engine": get_engine_name(),
    }


@router.get("/agents")
async def chat_agents(request: Request):
    """List available agents for the chat agent picker (session-authenticated)."""
    user, pool = await _get_session_user(request)
    from lucent.db.definitions import DefinitionRepository

    repo = DefinitionRepository(pool)
    result = await repo.list_agents(
        str(user["organization_id"]),
        status="active",
        requester_user_id=str(user["id"]),
        requester_role=user.get("role", "member"),
    )
    return result.get("items", result) if isinstance(result, dict) else result


@router.post("/stream-v2")
async def chat_stream_v2(
    request: Request,
    body: ChatStreamRequest,
):
    """Enhanced streaming chat with granular SSE events.

    Streams individual events for text deltas, tool calls, tool results,
    and session state — enabling rich UI visibility into what the model
    is doing.

    SSE event types:
      - text_delta: Incremental text chunk
      - tool_call: Model invoked a tool (name + args)
      - tool_result: Tool returned a result
      - message: Complete message block
      - error: Something went wrong
      - done: Session finished
    """
    import asyncio

    user, pool = await _get_session_user(request)

    from lucent.llm import get_engine_for_model
    from lucent.llm.engine import SessionEvent, SessionEventType
    from lucent.model_registry import validate_model, validate_reasoning_effort

    session_defaults = await _chat_session_defaults(
        user=user,
        pool=pool,
        session_id=body.session_id,
    )
    selected_model = _resolve_chat_model(body.model or session_defaults.get("model"))
    reasoning_effort = (
        body.reasoning_effort
        if body.reasoning_effort is not None
        else session_defaults.get("reasoning_effort")
    )
    agent_id = body.agent_id or session_defaults.get("agent_id")
    validation_error = validate_model(selected_model)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)
    if not await _can_user_access_model(user, pool, selected_model):
        raise HTTPException(status_code=403, detail="Model is not available to this user")
    effort_error = validate_reasoning_effort(selected_model, reasoning_effort)
    if effort_error:
        raise HTTPException(status_code=400, detail=effort_error)

    engine = get_engine_for_model(selected_model)

    # Build system prompt — load the agent definition (skills, managed tools).
    # When no agent is explicitly selected, fall back to the built-in `lucent`
    # identity so the default chat persona is composed from the same definition
    # the daemon uses (agents are built the same way in chat and the daemon).
    system_prompt_parts = []
    agent_name = None
    agent_skill_names: list[str] = []
    agent_managed_tool_names: list[str] = []
    # The agent actually composed for this turn (may be the lucent default even
    # when the user did not pick one). Used for hooks, managed-tool grants, and
    # tool allow-listing so the default persona has full parity.
    effective_agent_id = agent_id

    try:
        from lucent.db.definitions import DefinitionRepository
        from lucent.llm.agent_composition import (
            render_managed_tools_section,
            render_skills_section,
        )

        repo = DefinitionRepository(pool)
        agent = None
        if agent_id:
            agent = await repo.get_agent(agent_id, str(user["organization_id"]))
        else:
            agent = await _load_default_chat_agent(repo, str(user["organization_id"]))
        if agent:
            effective_agent_id = str(agent["id"])
            system_prompt_parts.append(agent.get("content", ""))
            agent_name = agent.get("name", "Lucent")
            # Skills are listed (name/id/description); the agent loads full
            # instructions on demand via get_skill_definition.
            skills = await repo.get_agent_skills(effective_agent_id)
            agent_skill_names = [str(s["name"]) for s in skills if s.get("name")]
            skills_section = render_skills_section(skills)
            if skills_section:
                system_prompt_parts.append(skills_section)
            managed_tools = await repo.get_agent_managed_tools(effective_agent_id)
            agent_managed_tool_names = [
                str(t["name"]) for t in managed_tools if t.get("name")
            ]
            tools_section = render_managed_tools_section(managed_tools)
            if tools_section:
                system_prompt_parts.append(tools_section)
    except Exception:
        logger.debug("Failed to load agent definition for chat", exc_info=True)

    if not system_prompt_parts:
        # Default Lucent system prompt
        display_name = user.get("display_name", "a user")
        system_prompt_parts.append(
            "You are Lucent, an AI assistant embedded in the Lucent Memory System.\n"
            f"You're talking with {display_name}.\n"
            "Be helpful, concise, and knowledgeable. "
            "You have access to MCP tools for memory search, creation, and management.\n"
            f"{_chat_tool_grounding_instructions()}\n"
            "Format responses in markdown when helpful.\n"
            f"Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}."
        )

    if agent_id and agent_name:
        system_prompt_parts.append(_chat_tool_grounding_instructions())
        if agent_name == "definition-engineer":
            system_prompt_parts.append(
                "## Web Agent Composer context\n"
                "You are running inside the Agent Composer web page, helping a human "
                "create agents, skills, and managed tools conversationally. There is no "
                "daemon task_id "
                "in this session, so do not call or ask for `log_task_event`. When the "
                "user wants a new capability, ask concise clarifying questions only when "
                "needed, then use `create_skill_definition`, `create_tool_definition`, "
                "and `create_agent_definition` to create proposed definitions. For requests "
                "like 'connect to an API and do X', prefer proposing a managed tool with a "
                "tight JSON input schema, explicit credential references, no-network or "
                "allowlist network policy, and small resource limits. Do not approve, run, "
                "or grant definitions from "
                "chat; tell the user to review proposals in the Pending tab. Keep language "
                "plain and avoid assuming the user knows prompt-engineering terminology.\n"
                "Lucent supports three lightweight agent kinds inside the existing definition "
                "framework: `functional` agents primarily perform a job or workflow; `persona` "
                "agents primarily provide a consistent stance, voice, relationship, or judgment "
                "style; `hybrid` agents combine both. Persona agents are still real agent "
                "definitions: give them clear boundaries, escalation rules, and any skills/tools "
                "they need. When creating an agent, include `proposal_evidence` with "
                "at least `agent_kind` (`functional`, `persona`, or `hybrid`) and "
                "a short `agent_kind_reason`."
            )
        elif agent_name == "workflow-composer":
            system_prompt_parts.append(
                "## Web Workflow Wizard context\n"
                "You are running inside the Workflow Wizard web page, helping a human "
                "turn an automation idea into a Lucent workflow. There is no daemon "
                "task_id in this session, so do not call or ask for `log_task_event`. "
                "Use `list_workflows` to avoid duplicates, `list_available_models` only "
                "when model choice matters, and `list_agent_definitions(status=\"active\")` "
                "before naming action agents. Use `create_workflow` only after the user "
                "explicitly asks you to create the workflow or confirms your draft. Never "
                "invent agent types such as `general-purpose`; if an agent name is not in "
                "the active-agent list, do not include it in the draft or create call. "
                "Prefer drafting trigger, request template, ordered actions, and review "
                "criteria before creating. For actions, use `task` when work should run "
                "through the daemon queue, and `user_interaction` when the workflow's "
                "output should be a Handoff message or clarification for the "
                "user. Keep explanations plain-language and reflect "
                "the UI model: schedule/manual/webhook/integration-event trigger, "
                "request, actions, Handoffs, outputs, review. Do not invent server_function "
                "actions; those are source-defined built-in maintenance workflows."
            )

    system_prompt_parts.append(await _render_active_user_context(user, pool))
    system_prompt = "\n\n".join(system_prompt_parts)
    agent_hooks = await _load_agent_hooks(pool, effective_agent_id)

    last_message = body.messages[-1].content if body.messages else ""
    attachments = _normalize_last_attachments(body.messages)
    chat_session = await _prepare_persistent_chat_session(
        user=user,
        pool=pool,
        session_id=body.session_id,
        kind=body.surface,
        engine_name=engine.name,
        model=selected_model,
        reasoning_effort=reasoning_effort,
        agent_id=agent_id,
        last_message=last_message,
        attachments=attachments,
    )
    await _mirror_handoff_user_turn(
        user=user,
        pool=pool,
        chat_session=chat_session,
        content=last_message,
    )
    fallback_history = [_chat_message_to_dict(m) for m in body.messages[:-1]]
    persisted_history = chat_session.previous_messages or fallback_history
    resume_provider = bool(
        engine.name == "copilot"
        and chat_session.provider_session_id
        and chat_session.provider_initialized
    )
    prompt = last_message if resume_provider else _history_prompt(persisted_history, last_message)
    message_history = (
        _message_history_for_engine(chat_session.previous_messages)
        if engine.name == "langchain" and chat_session.previous_messages
        else None
    )

    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    allowed_tools = _chat_allowed_tools_for_agent(agent_name, agent_skill_names)
    if agent_managed_tool_names:
        for tool in ("list_tool_definitions", "get_tool_definition", "run_managed_tool"):
            if tool not in allowed_tools:
                allowed_tools.append(tool)
    mcp_config = (
        _build_mcp_config(
            session_token,
            llm_session_id=chat_session.session_id,
            llm_turn_id=chat_session.turn_id,
            llm_message_id=chat_session.user_message_id,
            agent_definition_id=effective_agent_id,
            tools=allowed_tools,
        )
        if session_token
        else {}
    )

    logger.info(
        "Chat v2 session: engine=%s, model=%s, agent=%s",
        engine.name,
        selected_model,
        agent_name or "default",
    )

    # Use an asyncio.Queue to bridge callback events → SSE stream
    event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
    event_persist_tasks: list[asyncio.Task] = []
    event_sequence = 0
    tool_call_inputs: dict[str, Any] = {}

    def persist_event(payload: dict, event: SessionEvent) -> None:
        """Persist a normalized session event without blocking SDK callbacks."""
        nonlocal event_sequence
        if not chat_session.repo or not chat_session.session_id:
            return
        event_sequence += 1
        sequence = event_sequence

        async def persist_and_audit() -> None:
            row = await chat_session.repo.add_event(
                chat_session.session_id,
                org_id=str(user["organization_id"]),
                turn_id=chat_session.turn_id,
                message_id=chat_session.user_message_id,
                sequence=sequence,
                event_type=payload.get("type", event.type.value),
                tool_name=payload.get("tool") or event.tool_name,
                tool_input=event.tool_input or payload.get("input"),
                tool_output=event.tool_output or payload.get("output"),
                detail=payload.get("text") or payload.get("error"),
                raw=_event_raw(event),
                visible=payload.get("type") not in {"text_delta"},
            )
            if engine.name == "langchain" or payload.get("type") != "tool_result":
                return
            try:
                from lucent.db.tool_audit import ToolAuditRepository, classify_tool_result

                output = event.tool_output or payload.get("output") or ""
                status, failure_class, error_message = classify_tool_result(output)
                audit = ToolAuditRepository(pool)
                tool_name = payload.get("tool") or event.tool_name or "unknown"
                await audit.log_tool_call(
                    tool_name=tool_name,
                    status=status,
                    source="chat.stream_v2.session_event",
                    input_payload=tool_call_inputs.get(tool_name, {}),
                    output_payload=output,
                    failure_class=failure_class,
                    error_message=error_message,
                    context={
                        "organization_id": str(user["organization_id"]),
                        "user_id": str(user["id"]),
                        "session_id": chat_session.session_id,
                        "turn_id": chat_session.turn_id,
                        "message_id": chat_session.user_message_id,
                        "llm_event_id": str(row["id"]),
                        "model": selected_model,
                        "reasoning_effort": reasoning_effort,
                        "engine": engine.name,
                        "agent_definition_id": effective_agent_id,
                        "skill_names": agent_skill_names,
                    },
                )
            except Exception:
                logger.debug("Failed to audit chat tool event", exc_info=True)

        event_persist_tasks.append(asyncio.create_task(persist_and_audit()))

    def on_event(event: SessionEvent):
        """Push normalized events into the queue for SSE consumption."""
        if event.type == SessionEventType.MESSAGE_DELTA:
            payload = {
                "type": "text_delta",
                "text": event.content or "",
            }
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.MESSAGE:
            payload = {
                "type": "message",
                "text": event.content or "",
            }
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.TOOL_CALL:
            tool_input = event.content or ""
            if event.tool_input is not None:
                try:
                    tool_input = json.dumps(event.tool_input, default=str)
                except Exception:
                    tool_input = str(event.tool_input)
            if event.tool_name:
                tool_call_inputs[event.tool_name] = event.tool_input or tool_input
            payload = {
                "type": "tool_call",
                "tool": event.tool_name or "unknown",
                "input": tool_input,
            }
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.TOOL_RESULT:
            payload = {
                "type": "tool_result",
                "tool": event.tool_name or "unknown",
                "output": event.tool_output or event.content or "",
            }
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.ERROR:
            payload = {
                "type": "error",
                "error": event.content or "Unknown error",
            }
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.OTHER and event.tool_name == "_reasoning":
            payload = {
                "type": "reasoning",
                "text": event.content or "",
            }
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.OTHER and event.tool_name == "_hook":
            payload = _hook_event_payload(event)
            event_queue.put_nowait(payload)
            persist_event(payload, event)
        elif event.type == SessionEventType.SESSION_IDLE:
            pass  # We'll handle done when the task completes

    # Run the streaming session in a background task
    async def run_llm():
        try:
            result = await engine.run_session_streaming(
                model=selected_model,
                system_message=system_prompt,
                prompt=prompt,
                mcp_config=mcp_config,
                on_event=on_event,
                timeout=chat_timeout_seconds(),
                idle_timeout=chat_timeout_seconds(),
                reasoning_effort=reasoning_effort,
                provider_session_id=chat_session.provider_session_id,
                resume=resume_provider,
                message_history=message_history,
                hooks=agent_hooks,
                approve_permissions=False,
                attachments=attachments,
                audit_context={
                    "source": "chat.stream_v2",
                    "organization_id": str(user["organization_id"]),
                    "user_id": str(user["id"]),
                    "session_id": chat_session.session_id,
                    "turn_id": chat_session.turn_id,
                    "message_id": chat_session.user_message_id,
                    "model": selected_model,
                    "reasoning_effort": reasoning_effort,
                    "engine": engine.name,
                    "agent_definition_id": effective_agent_id,
                    "skill_names": agent_skill_names,
                },
            )
            if result and chat_session.repo and chat_session.session_id:
                await chat_session.repo.add_message(
                    chat_session.session_id,
                    role="assistant",
                    content=result,
                    org_id=str(user["organization_id"]),
                    turn_id=chat_session.turn_id,
                    metadata={"model": selected_model, "engine": engine.name},
                )
                if engine.name == "copilot":
                    await chat_session.repo.mark_provider_initialized(
                        chat_session.session_id,
                        str(user["organization_id"]),
                        provider_session_id=chat_session.provider_session_id,
                    )
            # If we got no deltas from streaming, send the full result
            if result:
                event_queue.put_nowait({
                    "type": "message",
                    "text": result,
                })
        except Exception as e:
            logger.error("Chat v2 session failed: %s", e)
            event_queue.put_nowait({
                "type": "error",
                "error": str(e),
            })
        finally:
            if event_persist_tasks:
                await asyncio.gather(*event_persist_tasks, return_exceptions=True)
            await _maybe_capture_session_experience(user=user, chat_session=chat_session)
            event_queue.put_nowait(None)  # Sentinel: stream done

    async def generate():
        task = asyncio.create_task(run_llm())
        try:
            if chat_session.session_id:
                session_event = {"type": "session", "session_id": chat_session.session_id}
                yield f"data: {json.dumps(session_event)}\n\n"
            while True:
                event = await event_queue.get()
                if event is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            task.cancel()
            raise

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
