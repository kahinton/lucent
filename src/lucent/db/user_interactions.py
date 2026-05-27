"""Repository for proactive Lucent↔user interactions.

A user interaction is a durable conversational handoff from the daemon,
workflow engine, or another Lucent subsystem to a human.  It can carry
structured references to the exact requests, memories, task outputs, workflow
runs, or chat sessions that motivated the message so future daemon cycles can
resume from the saved context instead of rediscovering it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Pool

VALID_INTERACTION_SOURCES = {
    "daemon",
    "workflow",
    "task",
    "request",
    "integration",
    "system",
    "human",
}
VALID_INTERACTION_TYPES = {
    "message",
    "clarification",
    "review",
    "decision",
    "workflow_output",
    "handoff",
}
VALID_INTERACTION_STATUSES = {
    "open",
    "waiting_on_user",
    "responded",
    "resolved",
    "dismissed",
}
OPEN_INTERACTION_STATUSES = {"open", "waiting_on_user", "responded"}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
VALID_REFERENCE_TYPES = {
    "request",
    "task",
    "task_output",
    "memory",
    "workflow",
    "schedule_run",
    "llm_session",
    "url",
    "other",
}
VALID_SENDER_TYPES = {"daemon", "user", "system"}

_PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _validate_choice(value: str, valid: set[str], field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in valid:
        raise ValueError(f"Invalid {field} '{value}'. Must be one of: {', '.join(sorted(valid))}")
    return normalized


def _normalize_references(references: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for ref in references or []:
        if not isinstance(ref, dict):
            raise ValueError("Each interaction reference must be an object")
        ref_type = _validate_choice(
            str(ref.get("reference_type") or ref.get("type") or "other"),
            VALID_REFERENCE_TYPES,
            "reference_type",
        )
        raw_reference_id = ref.get("reference_id") or ref.get("id")
        reference_id = _uuid(raw_reference_id) if raw_reference_id else None
        label = str(ref.get("label") or ref.get("title") or "").strip() or None
        url = str(ref.get("url") or "").strip() or None
        metadata = _json_dict(ref.get("metadata"))
        if reference_id is None and not label and not url:
            raise ValueError("Interaction reference requires reference_id, label, or url")
        normalized.append(
            {
                "reference_type": ref_type,
                "reference_id": reference_id,
                "label": label[:256] if label else None,
                "url": url,
                "metadata": metadata,
            }
        )
    return normalized


def _serialize_dt(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


class UserInteractionRepository:
    """Persistence for user-facing Lucent interactions and replies."""

    def __init__(self, pool: Pool):
        self.pool = pool

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = _json_dict(item.get("metadata"))
        if "priority_sort" not in item:
            item["priority_sort"] = _PRIORITY_ORDER.get(item.get("priority", "medium"), 2)
        return item

    def _message_row_to_dict(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = _json_dict(item.get("metadata"))
        return item

    def _reference_row_to_dict(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = _json_dict(item.get("metadata"))
        return item

    async def create_interaction(
        self,
        *,
        org_id: str | UUID,
        user_id: str | UUID | None,
        title: str,
        body: str,
        created_by: str | UUID | None = None,
        source: str = "daemon",
        interaction_type: str = "message",
        priority: str = "medium",
        requires_response: bool = False,
        response_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
        references: list[dict[str, Any]] | None = None,
        dedupe_key: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Create a new interaction, or return an existing open dedupe match.

        ``dedupe_key`` is intentionally first-class.  Producers that run every
        daemon cycle can send a stable key such as
        ``clarify:<goal-id>:<milestone-index>`` and Lucent will not spam the
        user with repeated open messages.
        """
        source = _validate_choice(source, VALID_INTERACTION_SOURCES, "source")
        interaction_type = _validate_choice(
            interaction_type, VALID_INTERACTION_TYPES, "interaction_type"
        )
        priority = _validate_choice(priority, VALID_PRIORITIES, "priority")
        initial_status = status or ("waiting_on_user" if requires_response else "open")
        initial_status = _validate_choice(initial_status, VALID_INTERACTION_STATUSES, "status")
        if initial_status in {"resolved", "dismissed"}:
            raise ValueError("New interactions cannot start resolved or dismissed")
        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title:
            raise ValueError("Interaction title is required")
        if not body:
            raise ValueError("Interaction body is required")
        dedupe_key = str(dedupe_key or "").strip() or None
        normalized_refs = _normalize_references(references)
        metadata = _json_dict(metadata)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if dedupe_key:
                    existing = await conn.fetchrow(
                        """SELECT * FROM user_interactions
                           WHERE organization_id = $1::uuid
                             AND user_id IS NOT DISTINCT FROM $2::uuid
                             AND dedupe_key = $3
                             AND status IN ('open', 'waiting_on_user', 'responded')
                           ORDER BY updated_at DESC
                           LIMIT 1""",
                        str(org_id),
                        str(user_id) if user_id else None,
                        dedupe_key,
                    )
                    if existing:
                        detail = await self._get_interaction_detail_locked(
                            conn,
                            str(existing["id"]),
                            str(org_id),
                            user_id=str(user_id) if user_id else None,
                        )
                        if detail:
                            detail["deduplicated"] = True
                            return detail

                now = datetime.now(timezone.utc)
                row = await conn.fetchrow(
                    """INSERT INTO user_interactions (
                           organization_id, user_id, created_by, source,
                           interaction_type, status, priority, title, body,
                           response_prompt, requires_response, dedupe_key,
                           metadata, created_at, updated_at
                       ) VALUES (
                           $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7,
                           $8, $9, $10, $11, $12, $13::jsonb, $14, $14
                       ) RETURNING *""",
                    str(org_id),
                    str(user_id) if user_id else None,
                    str(created_by) if created_by else None,
                    source,
                    interaction_type,
                    initial_status,
                    priority,
                    title[:256],
                    body,
                    response_prompt,
                    requires_response,
                    dedupe_key,
                    json.dumps(metadata, default=_serialize_dt),
                    now,
                )
                interaction_id = str(row["id"])
                sender_type = "system" if source == "system" else "daemon"
                await conn.execute(
                    """INSERT INTO user_interaction_messages (
                           interaction_id, sender_type, sender_user_id, body,
                           metadata, created_at
                       ) VALUES ($1::uuid, $2, $3::uuid, $4, $5::jsonb, $6)""",
                    interaction_id,
                    sender_type,
                    str(created_by) if created_by and sender_type == "user" else None,
                    body,
                    json.dumps({"initial": True, "source": source}),
                    now,
                )
                for ref in normalized_refs:
                    await self._insert_reference(conn, interaction_id, ref)

                detail = await self._get_interaction_detail_locked(
                    conn,
                    interaction_id,
                    str(org_id),
                    user_id=str(user_id) if user_id else None,
                )
                if detail:
                    detail["deduplicated"] = False
                    return detail
        raise RuntimeError("Failed to create interaction")

    async def _insert_reference(self, conn, interaction_id: str, ref: dict[str, Any]) -> None:
        await conn.execute(
            """INSERT INTO user_interaction_references (
                   interaction_id, reference_type, reference_id, label, url, metadata
               ) VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6::jsonb)""",
            interaction_id,
            ref["reference_type"],
            str(ref["reference_id"]) if ref.get("reference_id") else None,
            ref.get("label"),
            ref.get("url"),
            json.dumps(ref.get("metadata") or {}, default=_serialize_dt),
        )

    async def list_interactions(
        self,
        *,
        org_id: str | UUID,
        user_id: str | UUID | None = None,
        status: str | list[str] | None = None,
        include_resolved: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: list[Any] = [str(org_id)]
        clauses = ["i.organization_id = $1::uuid"]
        if user_id is not None:
            params.append(str(user_id))
            clauses.append(f"(i.user_id = ${len(params)}::uuid OR i.user_id IS NULL)")
        if status:
            statuses = [status] if isinstance(status, str) else status
            normalized = [
                _validate_choice(s, VALID_INTERACTION_STATUSES, "status") for s in statuses
            ]
            params.append(normalized)
            clauses.append(f"i.status = ANY(${len(params)}::varchar[])")
        elif not include_resolved:
            clauses.append("i.status NOT IN ('resolved', 'dismissed')")

        where = " AND ".join(clauses)
        count_query = f"SELECT COUNT(*) FROM user_interactions i WHERE {where}"
        viewer_param = None
        if user_id is not None:
            viewer_param = str(user_id)
        params_for_query = list(params)
        params_for_query.extend([viewer_param, limit, offset])
        viewer_idx = len(params) + 1
        limit_idx = len(params) + 2
        offset_idx = len(params) + 3
        query = f"""
            SELECT i.*,
                   COALESCE(msg_stats.message_count, 0)::int AS message_count,
                   COALESCE(ref_stats.reference_count, 0)::int AS reference_count,
                   lm.sender_type AS last_message_sender,
                   lm.body AS last_message_body,
                   lm.created_at AS last_message_at,
                   v.last_viewed_at,
                   (i.requires_response AND i.status = 'waiting_on_user') AS needs_response,
                   ((v.last_viewed_at IS NULL OR v.last_viewed_at < i.updated_at)
                       AND COALESCE(lm.sender_type, 'daemon') <> 'user') AS is_unread
            FROM user_interactions i
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS message_count
                FROM user_interaction_messages m
                WHERE m.interaction_id = i.id
            ) msg_stats ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS reference_count
                FROM user_interaction_references r
                WHERE r.interaction_id = i.id
            ) ref_stats ON TRUE
            LEFT JOIN LATERAL (
                SELECT m.sender_type, m.body, m.created_at
                FROM user_interaction_messages m
                WHERE m.interaction_id = i.id
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 1
            ) lm ON TRUE
            LEFT JOIN user_interaction_views v
              ON v.interaction_id = i.id
             AND v.user_id = ${viewer_idx}::uuid
            WHERE {where}
            ORDER BY
                CASE WHEN i.requires_response AND i.status = 'waiting_on_user' THEN 0 ELSE 1 END,
                CASE WHEN (v.last_viewed_at IS NULL OR v.last_viewed_at < i.updated_at)
                          AND COALESCE(lm.sender_type, 'daemon') <> 'user'
                     THEN 0 ELSE 1 END,
                CASE i.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                WHEN 'medium' THEN 2 ELSE 3 END,
                i.updated_at DESC,
                i.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)
            rows = await conn.fetch(query, *params_for_query)
        return {
            "items": [self._row_to_dict(row) for row in rows],
            "total_count": int(total or 0),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < int(total or 0),
        }

    async def count_attention_needed(
        self,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> int:
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """SELECT COUNT(*)
                   FROM user_interactions i
                   LEFT JOIN user_interaction_views v
                     ON v.interaction_id = i.id
                    AND v.user_id = $2::uuid
                   LEFT JOIN LATERAL (
                       SELECT m.sender_type
                       FROM user_interaction_messages m
                       WHERE m.interaction_id = i.id
                       ORDER BY m.created_at DESC, m.id DESC
                       LIMIT 1
                   ) lm ON TRUE
                   WHERE i.organization_id = $1::uuid
                     AND (i.user_id = $2::uuid OR i.user_id IS NULL)
                     AND i.status IN ('open', 'waiting_on_user', 'responded')
                     AND (
                         (i.requires_response AND i.status = 'waiting_on_user')
                         OR (
                             (v.last_viewed_at IS NULL OR v.last_viewed_at < i.updated_at)
                             AND COALESCE(lm.sender_type, 'daemon') <> 'user'
                         )
                     )""",
                str(org_id),
                str(user_id),
            )
        return int(count or 0)

    async def get_interaction(
        self,
        interaction_id: str | UUID,
        org_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            return await self._get_interaction_detail_locked(
                conn,
                str(interaction_id),
                str(org_id),
                user_id=str(user_id) if user_id else None,
            )

    async def _get_interaction_detail_locked(
        self,
        conn,
        interaction_id: str,
        org_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        params: list[Any] = [interaction_id, org_id]
        user_clause = ""
        if user_id is not None:
            params.append(user_id)
            user_clause = f" AND (user_id = ${len(params)}::uuid OR user_id IS NULL)"
        row = await conn.fetchrow(
            f"""SELECT * FROM user_interactions
                WHERE id = $1::uuid
                  AND organization_id = $2::uuid
                  {user_clause}""",
            *params,
        )
        if not row:
            return None
        interaction = self._row_to_dict(row)
        message_rows = await conn.fetch(
            """SELECT * FROM user_interaction_messages
               WHERE interaction_id = $1::uuid
               ORDER BY created_at, id""",
            interaction_id,
        )
        reference_rows = await conn.fetch(
            """SELECT * FROM user_interaction_references
               WHERE interaction_id = $1::uuid
               ORDER BY created_at, id""",
            interaction_id,
        )
        interaction["messages"] = [self._message_row_to_dict(r) for r in message_rows]
        interaction["references"] = [self._reference_row_to_dict(r) for r in reference_rows]
        interaction["message_count"] = len(interaction["messages"])
        interaction["reference_count"] = len(interaction["references"])
        return interaction

    async def add_message(
        self,
        *,
        interaction_id: str | UUID,
        org_id: str | UUID,
        sender_type: str,
        body: str,
        sender_user_id: str | UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sender_type = _validate_choice(sender_type, VALID_SENDER_TYPES, "sender_type")
        body = str(body or "").strip()
        if not body:
            raise ValueError("Message body is required")
        now = datetime.now(timezone.utc)
        metadata = _json_dict(metadata)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    """SELECT * FROM user_interactions
                       WHERE id = $1::uuid AND organization_id = $2::uuid
                       FOR UPDATE""",
                    str(interaction_id),
                    str(org_id),
                )
                if not existing:
                    raise ValueError("Interaction not found")
                if existing["status"] in {"resolved", "dismissed"} and sender_type != "system":
                    raise ValueError("Interaction is closed")
                await conn.execute(
                    """INSERT INTO user_interaction_messages (
                           interaction_id, sender_type, sender_user_id, body,
                           metadata, created_at
                       ) VALUES ($1::uuid, $2, $3::uuid, $4, $5::jsonb, $6)""",
                    str(interaction_id),
                    sender_type,
                    str(sender_user_id) if sender_user_id else None,
                    body,
                    json.dumps(metadata, default=_serialize_dt),
                    now,
                )
                if sender_type == "user":
                    await conn.execute(
                        """UPDATE user_interactions
                           SET status = CASE
                                   WHEN status IN ('open', 'waiting_on_user') THEN 'responded'
                                   ELSE status
                               END,
                               first_response_at = COALESCE(first_response_at, $3),
                               last_response_at = $3,
                               updated_at = $3
                           WHERE id = $1::uuid AND organization_id = $2::uuid""",
                        str(interaction_id),
                        str(org_id),
                        now,
                    )
                elif sender_type in {"daemon", "system"}:
                    await conn.execute(
                        """UPDATE user_interactions
                           SET status = CASE
                                   WHEN status IN ('resolved', 'dismissed') THEN status
                                   WHEN requires_response THEN 'waiting_on_user'
                                   ELSE 'open'
                               END,
                               updated_at = $3
                           WHERE id = $1::uuid AND organization_id = $2::uuid""",
                        str(interaction_id),
                        str(org_id),
                        now,
                    )
                detail = await self._get_interaction_detail_locked(
                    conn,
                    str(interaction_id),
                    str(org_id),
                )
                if detail:
                    return detail
        raise RuntimeError("Failed to add interaction message")

    async def mark_viewed(
        self,
        *,
        interaction_id: str | UUID,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO user_interaction_views (
                       interaction_id, user_id, organization_id,
                       first_viewed_at, last_viewed_at
                   )
                   SELECT id, $3::uuid, organization_id, NOW(), NOW()
                   FROM user_interactions
                   WHERE id = $1::uuid
                     AND organization_id = $2::uuid
                     AND (user_id = $3::uuid OR user_id IS NULL)
                   ON CONFLICT (interaction_id, user_id)
                   DO UPDATE SET last_viewed_at = EXCLUDED.last_viewed_at
                   RETURNING *""",
                str(interaction_id),
                str(org_id),
                str(user_id),
            )
        return dict(row) if row else None

    async def resolve_interaction(
        self,
        *,
        interaction_id: str | UUID,
        org_id: str | UUID,
        user_id: str | UUID | None = None,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._close_interaction(
            interaction_id=interaction_id,
            org_id=org_id,
            user_id=user_id,
            status="resolved",
            timestamp_column="resolved_at",
            note=note,
        )

    async def dismiss_interaction(
        self,
        *,
        interaction_id: str | UUID,
        org_id: str | UUID,
        user_id: str | UUID | None = None,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._close_interaction(
            interaction_id=interaction_id,
            org_id=org_id,
            user_id=user_id,
            status="dismissed",
            timestamp_column="dismissed_at",
            note=note,
        )

    async def _close_interaction(
        self,
        *,
        interaction_id: str | UUID,
        org_id: str | UUID,
        user_id: str | UUID | None,
        status: str,
        timestamp_column: str,
        note: str | None,
    ) -> dict[str, Any] | None:
        if status not in {"resolved", "dismissed"}:
            raise ValueError("Closed status must be resolved or dismissed")
        now = datetime.now(timezone.utc)
        params: list[Any] = [str(interaction_id), str(org_id), now, status]
        user_clause = ""
        if user_id is not None:
            params.append(str(user_id))
            user_clause = f" AND (user_id = ${len(params)}::uuid OR user_id IS NULL)"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchrow(
                    f"""UPDATE user_interactions
                        SET status = $4,
                            {timestamp_column} = $3,
                            updated_at = $3
                        WHERE id = $1::uuid
                          AND organization_id = $2::uuid
                          {user_clause}
                        RETURNING *""",
                    *params,
                )
                if not updated:
                    return None
                if note:
                    await conn.execute(
                        """INSERT INTO user_interaction_messages (
                               interaction_id, sender_type, sender_user_id, body,
                               metadata, created_at
                           ) VALUES ($1::uuid, 'system', $2::uuid, $3, $4::jsonb, $5)""",
                        str(interaction_id),
                        str(user_id) if user_id else None,
                        note.strip(),
                        json.dumps({"status_transition": status}),
                        now,
                    )
                detail = await self._get_interaction_detail_locked(
                    conn,
                    str(interaction_id),
                    str(org_id),
                )
                return detail
