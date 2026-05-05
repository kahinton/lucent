"""Repository for persisted LLM sessions, messages, and lineage events."""

from __future__ import annotations

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


def _json_value(value: Any) -> Any:
    """Normalize arbitrary values into JSONB-compatible shapes."""
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
