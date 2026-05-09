"""Repository for persisted LLM sessions, messages, and lineage events."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool

_VALID_SESSION_KINDS = {
    "chat",
    "embedded_chat",
    "task",
    "request",
    "daemon",
    "schedule",
    "integration",
}
_VALID_SESSION_STATUSES = {"active", "idle", "archived", "deleted", "error"}
_VALID_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}
_VALID_REQUEST_RELATIONS = {"created", "discussed", "reviewed", "handoff"}
_SESSION_EXPERIENCE_TAGS = [
    "session-experience",
    "chat-session",
    "auto-captured",
]
_SIGNIFICANT_SESSION_TOOLS = {
    "create_request",
    "create_memory",
    "update_memory",
    "delete_memory",
    "link_request_memory",
    "link_task_memory",
    "record_task_output",
    "create_task",
    "propose_sandbox_template",
    "create_agent_definition",
    "update_agent_definition",
    "create_skill_definition",
    "update_skill_definition",
}
_TRIVIAL_USER_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(tell|crack|make).{0,20}\bjoke\b",
        r"\bjoke\b",
        r"\bthanks?\b",
        r"\bhello\b|\bhi\b|\bhey\b",
        r"\bwhat (have|did) we been working on\b",
        r"\bwhat('?s| is) going on\b",
        r"\bstatus update\b",
    )
]


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _title_from_message(content: str | None) -> str | None:
    if not content:
        return None
    first_line = " ".join(content.strip().splitlines()[0:1]).strip()
    if not first_line:
        return None
    return first_line[:80]


def _safe_metadata(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_preview(content: str | None, limit: int = 240) -> str:
    text = " ".join(str(content or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _is_trivial_user_text(text: str) -> bool:
    compact = " ".join((text or "").split())
    if len(compact) > 220:
        return False
    return any(pattern.search(compact) for pattern in _TRIVIAL_USER_PATTERNS)


def _session_capture_evaluation(
    *,
    session: dict,
    messages: list[dict],
    events: list[dict],
    requests: list[dict],
) -> dict[str, Any]:
    """Cheap deterministic signal check for session→experience capture."""
    user_messages = [m for m in messages if m.get("role") == "user"]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]
    user_text = "\n".join(str(m.get("content") or "") for m in user_messages)
    assistant_text = "\n".join(str(m.get("content") or "") for m in assistant_messages)
    tool_names = [str(e.get("tool_name") or "") for e in events if e.get("tool_name")]
    significant_tools = sorted({t for t in tool_names if t in _SIGNIFICANT_SESSION_TOOLS})

    score = 0
    reasons: list[str] = []
    if requests:
        score += 4
        reasons.append("linked_requests")
    if significant_tools:
        score += 3
        reasons.append("mutating_tools")
    if len(user_messages) >= 2 and len(assistant_messages) >= 2:
        score += 1
        reasons.append("multi_turn")
    if len(user_text) >= 600 or len(assistant_text) >= 1200:
        score += 1
        reasons.append("substantial_transcript")
    if session.get("kind") in {"task", "request", "daemon", "integration"}:
        score += 2
        reasons.append("work_session_kind")

    trivial = (
        len(user_messages) <= 1
        and not requests
        and not significant_tools
        and _is_trivial_user_text(user_text)
    )
    should_capture = not trivial and (bool(requests) or bool(significant_tools) or score >= 3)
    return {
        "should_capture": should_capture,
        "score": score,
        "reasons": reasons,
        "trivial": trivial,
        "message_count": len(messages),
        "user_message_count": len(user_messages),
        "assistant_message_count": len(assistant_messages),
        "tool_names": sorted({t for t in tool_names if t}),
        "significant_tools": significant_tools,
        "request_ids": [str(r["request_id"]) for r in requests if r.get("request_id")],
    }


class LLMSessionRepository:
    """Persistence for provider-backed and provider-independent LLM sessions."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def create_session(
        self,
        *,
        org_id: str | UUID,
        user_id: str | UUID | None = None,
        kind: str = "chat",
        title: str | None = None,
        summary: str | None = None,
        engine: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agent_definition_id: str | UUID | None = None,
        provider_session_id: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
        request_id: str | UUID | None = None,
        task_id: str | UUID | None = None,
        schedule_run_id: str | UUID | None = None,
        parent_session_id: str | UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        if kind not in _VALID_SESSION_KINDS:
            raise ValueError(f"Invalid LLM session kind: {kind}")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO llm_sessions (
                       organization_id, user_id, kind, status, title, summary,
                       engine, model, reasoning_effort, agent_definition_id,
                       provider_session_id, provider_metadata, request_id,
                       task_id, schedule_run_id, parent_session_id, metadata,
                       last_message_at
                   ) VALUES (
                       $1, $2, $3, 'active', $4, $5, $6, $7, $8, $9,
                       $10, $11, $12, $13, $14, $15, $16, NULL
                   ) RETURNING *""",
                _uuid(org_id),
                _uuid(user_id),
                kind,
                title,
                summary,
                engine,
                model,
                reasoning_effort,
                _uuid(agent_definition_id),
                provider_session_id,
                provider_metadata or {},
                _uuid(request_id),
                _uuid(task_id),
                _uuid(schedule_run_id),
                _uuid(parent_session_id),
                metadata or {},
            )
        return dict(row)

    async def get_session(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
        include_archived: bool = False,
    ) -> dict | None:
        clauses = ["id = $1", "organization_id = $2"]
        params: list[Any] = [_uuid(session_id), _uuid(org_id)]
        if user_id is not None:
            params.append(_uuid(user_id))
            clauses.append(f"user_id = ${len(params)}")
        if not include_archived:
            clauses.append("status <> 'deleted'")
        query = f"SELECT * FROM llm_sessions WHERE {' AND '.join(clauses)}"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def list_sessions(
        self,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
        kind: str | None = None,
        include_archived: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        params: list[Any] = [_uuid(org_id)]
        clauses = ["s.organization_id = $1"]
        if user_id is not None:
            params.append(_uuid(user_id))
            clauses.append(f"s.user_id = ${len(params)}")
        if kind:
            if kind not in _VALID_SESSION_KINDS:
                raise ValueError(f"Invalid LLM session kind: {kind}")
            params.append(kind)
            clauses.append(f"s.kind = ${len(params)}")
        if not include_archived:
            clauses.append("s.status NOT IN ('archived', 'deleted')")
        where_sql = " AND ".join(clauses)
        count_query = f"SELECT COUNT(*) AS total FROM llm_sessions s WHERE {where_sql}"
        query = (
            "SELECT s.*, "
            "       COUNT(m.id) AS message_count, "
            "       COUNT(DISTINCT sr.request_id) AS linked_request_count "
            "FROM llm_sessions s "
            "LEFT JOIN llm_messages m ON m.session_id = s.id "
            "LEFT JOIN llm_session_requests sr ON sr.session_id = s.id "
            f"WHERE {where_sql} "
            "GROUP BY s.id "
            "ORDER BY COALESCE(s.last_message_at, s.updated_at, s.created_at) DESC "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)
            rows = await conn.fetch(query, *params, limit, offset)
        return {
            "items": [dict(r) for r in rows],
            "total_count": int(total or 0),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < int(total or 0),
        }

    async def update_session(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
        title: str | None = None,
        summary: str | None = None,
        engine: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agent_definition_id: str | UUID | None = None,
        provider_session_id: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> dict | None:
        if status is not None and status not in _VALID_SESSION_STATUSES:
            raise ValueError(f"Invalid LLM session status: {status}")
        sets: list[str] = []
        params: list[Any] = []

        def add(field: str, value: Any, cast: str = "") -> None:
            params.append(value)
            sets.append(f"{field} = ${len(params)}{cast}")

        if title is not None:
            add("title", title)
        if summary is not None:
            add("summary", summary)
        if engine is not None:
            add("engine", engine)
        if model is not None:
            add("model", model)
        if reasoning_effort is not None:
            add("reasoning_effort", reasoning_effort or None)
        if agent_definition_id is not None:
            add("agent_definition_id", _uuid(agent_definition_id))
        if provider_session_id is not None:
            add("provider_session_id", provider_session_id)
        if provider_metadata is not None:
            add("provider_metadata", provider_metadata)
        if metadata is not None:
            add("metadata", metadata)
        if status is not None:
            add("status", status)
            if status == "archived":
                sets.append("archived_at = COALESCE(archived_at, NOW())")
            elif status != "archived":
                sets.append("archived_at = NULL")

        if not sets:
            return await self.get_session(session_id, org_id, user_id=user_id)

        sets.append("updated_at = NOW()")
        params.append(_uuid(session_id))
        params.append(_uuid(org_id))
        clauses = [f"id = ${len(params) - 1}", f"organization_id = ${len(params)}"]
        if user_id is not None:
            params.append(_uuid(user_id))
            clauses.append(f"user_id = ${len(params)}")
        query = (
            f"UPDATE llm_sessions SET {', '.join(sets)} "
            f"WHERE {' AND '.join(clauses)} RETURNING *"
        )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def mark_provider_initialized(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        provider_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict | None:
        existing = await self.get_session(session_id, org_id, include_archived=True)
        if not existing:
            return None
        merged = dict(existing.get("provider_metadata") or {})
        merged.update(metadata or {})
        merged["provider_initialized"] = True
        return await self.update_session(
            session_id,
            org_id,
            provider_session_id=provider_session_id or existing.get("provider_session_id"),
            provider_metadata=merged,
            status="idle",
        )

    async def add_message(
        self,
        session_id: str | UUID,
        *,
        role: str,
        content: str,
        org_id: str | UUID,
        turn_id: str | UUID | None = None,
        provider_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        if role not in _VALID_MESSAGE_ROLES:
            raise ValueError(f"Invalid LLM message role: {role}")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    """SELECT id, title FROM llm_sessions
                       WHERE id = $1 AND organization_id = $2
                       FOR UPDATE""",
                    _uuid(session_id),
                    _uuid(org_id),
                )
                if not session:
                    raise ValueError("LLM session not found")
                sequence = await conn.fetchval(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM llm_messages WHERE session_id = $1",
                    _uuid(session_id),
                )
                row = await conn.fetchrow(
                    """INSERT INTO llm_messages
                           (session_id, turn_id, sequence, role, content,
                            provider_message_id, metadata)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       RETURNING *""",
                    _uuid(session_id),
                    _uuid(turn_id),
                    sequence,
                    role,
                    content or "",
                    provider_message_id,
                    metadata or {},
                )
                title_update = ""
                title_params: list[Any] = []
                if role == "user" and not session.get("title"):
                    generated_title = _title_from_message(content)
                    if generated_title:
                        title_update = ", title = $3"
                        title_params.append(generated_title)
                await conn.execute(
                    f"""UPDATE llm_sessions
                        SET last_message_at = NOW(), updated_at = NOW(), status = 'active'
                            {title_update}
                        WHERE id = $1 AND organization_id = $2""",
                    _uuid(session_id),
                    _uuid(org_id),
                    *title_params,
                )
        return dict(row)

    async def list_messages(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        roles: set[str] | None = None,
        limit: int = 200,
        before_sequence: int | None = None,
    ) -> list[dict]:
        params: list[Any] = [_uuid(session_id), _uuid(org_id)]
        clauses = ["m.session_id = $1", "s.organization_id = $2"]
        if roles:
            invalid = roles - _VALID_MESSAGE_ROLES
            if invalid:
                raise ValueError(f"Invalid LLM message roles: {sorted(invalid)}")
            params.append(list(roles))
            clauses.append(f"m.role = ANY(${len(params)}::varchar[])")
        if before_sequence is not None:
            params.append(before_sequence)
            clauses.append(f"m.sequence < ${len(params)}")
        params.append(limit)
        query = (
            "SELECT m.* FROM llm_messages m "
            "JOIN llm_sessions s ON s.id = m.session_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY m.sequence "
            f"LIMIT ${len(params)}"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def add_event(
        self,
        session_id: str | UUID,
        *,
        event_type: str,
        org_id: str | UUID,
        message_id: str | UUID | None = None,
        turn_id: str | UUID | None = None,
        sequence: int | None = None,
        tool_name: str | None = None,
        tool_input: Any = None,
        tool_output: Any = None,
        detail: str | None = None,
        raw: dict[str, Any] | None = None,
        visible: bool = True,
    ) -> dict:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                session_exists = await conn.fetchval(
                    """SELECT 1 FROM llm_sessions
                       WHERE id = $1 AND organization_id = $2
                       FOR UPDATE""",
                    _uuid(session_id),
                    _uuid(org_id),
                )
                if not session_exists:
                    raise ValueError("LLM session not found")
                if sequence is None:
                    sequence = await conn.fetchval(
                        """SELECT COALESCE(MAX(sequence), 0) + 1
                           FROM llm_session_events WHERE session_id = $1""",
                        _uuid(session_id),
                    )
                row = await conn.fetchrow(
                    """INSERT INTO llm_session_events
                           (session_id, message_id, turn_id, sequence, event_type,
                            tool_name, tool_input, tool_output, detail, raw, visible)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                       ON CONFLICT (session_id, sequence) DO UPDATE
                       SET event_type = EXCLUDED.event_type,
                           tool_name = EXCLUDED.tool_name,
                           tool_input = EXCLUDED.tool_input,
                           tool_output = EXCLUDED.tool_output,
                           detail = EXCLUDED.detail,
                           raw = EXCLUDED.raw,
                           visible = EXCLUDED.visible
                       RETURNING *""",
                    _uuid(session_id),
                    _uuid(message_id),
                    _uuid(turn_id),
                    sequence,
                    event_type[:64],
                    tool_name,
                    _json_value(tool_input),
                    _json_value(tool_output),
                    detail,
                    raw or {},
                    visible,
                )
                await conn.execute(
                    "UPDATE llm_sessions SET updated_at = NOW() WHERE id = $1",
                    _uuid(session_id),
                )
        return dict(row)

    async def list_events(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        limit: int = 500,
        visible_only: bool = False,
    ) -> list[dict]:
        params: list[Any] = [_uuid(session_id), _uuid(org_id)]
        clauses = ["e.session_id = $1", "s.organization_id = $2"]
        if visible_only:
            clauses.append("e.visible = TRUE")
        params.append(limit)
        query = (
            "SELECT e.* FROM llm_session_events e "
            "JOIN llm_sessions s ON s.id = e.session_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY e.sequence "
            f"LIMIT ${len(params)}"
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def link_request(
        self,
        session_id: str | UUID,
        request_id: str | UUID,
        *,
        org_id: str | UUID,
        message_id: str | UUID | None = None,
        event_id: str | UUID | None = None,
        relation: str = "created",
        set_origin_if_empty: bool = True,
    ) -> dict | None:
        if relation not in _VALID_REQUEST_RELATIONS:
            raise ValueError(f"Invalid LLM session request relation: {relation}")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                session_exists = await conn.fetchval(
                    "SELECT 1 FROM llm_sessions WHERE id = $1 AND organization_id = $2",
                    _uuid(session_id),
                    _uuid(org_id),
                )
                request_exists = await conn.fetchval(
                    "SELECT 1 FROM requests WHERE id = $1 AND organization_id = $2",
                    _uuid(request_id),
                    _uuid(org_id),
                )
                if not session_exists or not request_exists:
                    return None
                row = await conn.fetchrow(
                    """INSERT INTO llm_session_requests
                           (session_id, request_id, message_id, event_id, relation)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (session_id, request_id, relation) DO UPDATE
                       SET message_id = COALESCE(
                               llm_session_requests.message_id,
                               EXCLUDED.message_id
                           ),
                           event_id = COALESCE(llm_session_requests.event_id, EXCLUDED.event_id)
                       RETURNING *""",
                    _uuid(session_id),
                    _uuid(request_id),
                    _uuid(message_id),
                    _uuid(event_id),
                    relation,
                )
                if set_origin_if_empty:
                    await conn.execute(
                        """UPDATE requests
                           SET origin_session_id = COALESCE(origin_session_id, $1),
                               origin_message_id = COALESCE(origin_message_id, $2),
                               origin_event_id = COALESCE(origin_event_id, $3),
                               updated_at = NOW()
                           WHERE id = $4 AND organization_id = $5""",
                        _uuid(session_id),
                        _uuid(message_id),
                        _uuid(event_id),
                        _uuid(request_id),
                        _uuid(org_id),
                    )
        return dict(row) if row else None

    async def get_session_detail(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
        include_events: bool = True,
    ) -> dict | None:
        session = await self.get_session(
            session_id,
            org_id,
            user_id=user_id,
            include_archived=True,
        )
        if not session:
            return None
        session["messages"] = await self.list_messages(session_id, org_id, limit=500)
        session["events"] = (
            await self.list_events(session_id, org_id, limit=1000) if include_events else []
        )
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT lsr.*, r.title AS request_title, r.status AS request_status
                   FROM llm_session_requests lsr
                   JOIN requests r ON r.id = lsr.request_id
                   WHERE lsr.session_id = $1 AND r.organization_id = $2
                   ORDER BY lsr.created_at DESC""",
                _uuid(session_id),
                _uuid(org_id),
            )
        session["requests"] = [dict(r) for r in rows]
        return session

    async def evaluate_experience_capture(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        """Return session detail plus deterministic capture eligibility signals."""
        session = await self.get_session_detail(
            session_id,
            org_id,
            user_id=user_id,
            include_events=True,
        )
        if not session:
            return {
                "session": None,
                "evaluation": {"should_capture": False, "reason": "session_not_found"},
            }
        evaluation = _session_capture_evaluation(
            session=session,
            messages=session.get("messages") or [],
            events=session.get("events") or [],
            requests=session.get("requests") or [],
        )
        return {"session": session, "evaluation": evaluation}

    async def maybe_capture_experience(
        self,
        session_id: str | UUID,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
        content_override: str | None = None,
        summary_mode: str = "deterministic",
        summary_model: str | None = None,
        summary_error: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one experience memory summarizing a meaningful session.

        This intentionally avoids an LLM call. Capture is based on durable signals
        already stored for the session: linked requests, mutating tool events,
        transcript size, and session kind. Trivial chats are skipped.
        """
        capture_context = await self.evaluate_experience_capture(
            session_id,
            org_id,
            user_id=user_id,
        )
        session = capture_context.get("session")
        if not session:
            return {"status": "skipped", "reason": "session_not_found"}

        metadata = _safe_metadata(session.get("metadata"))
        if metadata.get("experience_capture_disabled"):
            return {"status": "skipped", "reason": "capture_disabled"}

        evaluation = capture_context["evaluation"]
        if not evaluation["should_capture"]:
            merged = {
                **metadata,
                "experience_capture": {
                    "status": "skipped",
                    "reason": "insufficient_signal",
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "score": evaluation["score"],
                    "trivial": evaluation["trivial"],
                },
            }
            await self.update_session(session_id, org_id, user_id=user_id, metadata=merged)
            return {
                "status": "skipped",
                "reason": "insufficient_signal",
                "evaluation": evaluation,
            }

        from lucent.db.memory import MemoryRepository

        memory_repo = MemoryRepository(self.pool)
        existing_memory_id = metadata.get("experience_memory_id")
        existing_memory = None
        if existing_memory_id:
            try:
                existing_memory = await memory_repo.get(UUID(str(existing_memory_id)))
            except ValueError:
                existing_memory = None

        if not existing_memory:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT id
                       FROM memories
                       WHERE organization_id = $1
                         AND type = 'experience'
                         AND deleted_at IS NULL
                         AND metadata->>'session_id' = $2
                       ORDER BY updated_at DESC
                       LIMIT 1""",
                    _uuid(org_id),
                    str(session_id),
                )
            if row:
                existing_memory = await memory_repo.get(row["id"])

        normalized_override = (content_override or "").strip()
        if normalized_override == "NO_EXPERIENCE_NEEDED":
            normalized_override = ""
        content = normalized_override or self._build_session_experience_content(
            session,
            evaluation,
        )
        memory_metadata = {
            "source": "llm_session",
            "session_id": str(session["id"]),
            "session_kind": session.get("kind"),
            "session_title": session.get("title"),
            "request_ids": evaluation["request_ids"],
            "tool_names": evaluation["tool_names"],
            "significant_tools": evaluation["significant_tools"],
            "message_count": evaluation["message_count"],
            "capture_score": evaluation["score"],
            "capture_reasons": evaluation["reasons"],
            "summary_mode": summary_mode,
            "summary_model": summary_model,
            "summary_error": summary_error,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        importance = min(8, max(4, 3 + int(evaluation["score"])))

        if existing_memory:
            existing_tags = list(existing_memory.get("tags") or [])
            tags = list(dict.fromkeys(existing_tags + _SESSION_EXPERIENCE_TAGS))
            existing_meta = _safe_metadata(existing_memory.get("metadata"))
            memory = await memory_repo.update(
                existing_memory["id"],
                content=content,
                tags=tags,
                importance=importance,
                metadata={**existing_meta, **memory_metadata},
            )
            status = "updated"
        else:
            username = session.get("title") or "Lucent session"
            memory = await memory_repo.create(
                username=str(username)[:255],
                type="experience",
                content=content,
                tags=list(_SESSION_EXPERIENCE_TAGS),
                importance=importance,
                metadata=memory_metadata,
                user_id=session.get("user_id"),
                organization_id=session.get("organization_id"),
            )
            status = "created"

        if not memory:
            return {"status": "skipped", "reason": "memory_write_failed"}

        await self._link_session_experience_to_requests(
            memory_id=memory["id"],
            org_id=org_id,
            request_ids=evaluation["request_ids"],
        )

        merged = {
            **metadata,
            "experience_memory_id": str(memory["id"]),
            "experience_capture": {
                "status": status,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "score": evaluation["score"],
                "reasons": evaluation["reasons"],
            },
        }
        await self.update_session(session_id, org_id, user_id=user_id, metadata=merged)
        try:
            await self.add_event(
                session_id,
                org_id=org_id,
                event_type="experience_memory_captured",
                detail=f"Session experience memory {status}: {memory['id']}",
                raw={"memory_id": str(memory["id"]), "status": status},
                visible=False,
            )
        except Exception:
            pass

        return {
            "status": status,
            "memory_id": str(memory["id"]),
            "evaluation": evaluation,
        }

    def _build_session_experience_content(
        self,
        session: dict,
        evaluation: dict[str, Any],
    ) -> str:
        messages = session.get("messages") or []
        requests = session.get("requests") or []
        user_messages = [m for m in messages if m.get("role") == "user"]
        assistant_messages = [m for m in messages if m.get("role") == "assistant"]
        first_user = _message_preview(user_messages[0].get("content") if user_messages else "")
        latest_user = _message_preview(user_messages[-1].get("content") if user_messages else "")
        latest_assistant = _message_preview(
            assistant_messages[-1].get("content") if assistant_messages else ""
        )
        request_lines = [
            f"- {r.get('request_title') or r.get('request_id')} "
            f"({r.get('request_status', 'unknown')}, relation={r.get('relation')})"
            for r in requests[:10]
        ]
        tool_names = evaluation.get("tool_names") or []
        title = session.get("title") or first_user or "Untitled session"
        captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        parts = [
            f"# Session experience: {title}",
            "",
            f"Captured: {captured_at}",
            f"Session ID: {session.get('id')}",
            f"Kind: {session.get('kind', 'chat')}",
            f"Signal: score={evaluation.get('score')} "
            f"reasons={', '.join(evaluation.get('reasons') or []) or 'n/a'}",
            "",
            "## Conversation focus",
            f"- First user ask: {first_user or 'n/a'}",
            f"- Latest user ask: {latest_user or 'n/a'}",
            f"- Latest assistant outcome: {latest_assistant or 'n/a'}",
            "",
            "## Linked work",
        ]
        parts.extend(request_lines or ["- No linked requests."])
        parts.extend([
            "",
            "## Tool/work signals",
            f"- Tools observed: {', '.join(tool_names) if tool_names else 'none'}",
            f"- Messages: {evaluation.get('message_count', 0)} "
            f"({evaluation.get('user_message_count', 0)} user, "
            f"{evaluation.get('assistant_message_count', 0)} assistant)",
            "",
            "## Why this was captured",
            "This session crossed Lucent's deterministic significance threshold "
            "for durable experience capture. It is intended as glue linking "
            "conversation context, requests, task outputs, and later learning extraction.",
        ])
        return "\n".join(parts)

    async def _link_session_experience_to_requests(
        self,
        *,
        memory_id: str | UUID,
        org_id: str | UUID,
        request_ids: list[str],
    ) -> None:
        if not request_ids:
            return
        async with self.pool.acquire() as conn:
            for request_id in request_ids:
                try:
                    await conn.execute(
                        """INSERT INTO request_memories (request_id, memory_id, relation)
                           SELECT $1::uuid, $2::uuid, 'context'
                           WHERE EXISTS (
                               SELECT 1 FROM requests
                               WHERE id = $1::uuid AND organization_id = $3::uuid
                           )
                           ON CONFLICT DO NOTHING""",
                        request_id,
                        str(memory_id),
                        str(org_id),
                    )
                except Exception:
                    continue


def _json_value(value: Any) -> Any:
    """Normalize arbitrary values into JSONB-compatible shapes."""
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
