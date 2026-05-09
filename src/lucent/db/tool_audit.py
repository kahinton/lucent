"""Operational audit log for LLM/agent tool calls.

This repository intentionally writes to ``tool_call_audit_log`` instead of
memories. The rows are telemetry for pattern analysis: which tools fail, under
which models/agents/sessions, and with what redacted inputs/results.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from asyncpg import Pool

_SECRET_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|token|secret|password|cookie|credential)",
    re.IGNORECASE,
)
_MAX_TEXT = 2000
_MAX_JSON_TEXT = 1000
_MAX_LIST_ITEMS = 20
_MAX_DICT_ITEMS = 40


def _uuid_or_none(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _redact_text(value: str, *, limit: int = _MAX_TEXT) -> str:
    text = str(value)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|token|secret|password|credential)=([^\s&]+)",
        r"\1=[REDACTED]",
        text,
    )
    return text[:limit] + ("…" if len(text) > limit else "")


def _redact_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= _MAX_DICT_ITEMS:
                out["__truncated__"] = True
                break
            key_str = str(key)
            if _SECRET_KEY_RE.search(key_str):
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = _redact_json(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        out = [_redact_json(item, depth=depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            out.append("[TRUNCATED]")
        return out
    if isinstance(value, str):
        return _redact_text(value, limit=_MAX_JSON_TEXT)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    try:
        return _redact_json(json.loads(json.dumps(value, default=str)), depth=depth + 1)
    except Exception:
        return _redact_text(str(value), limit=_MAX_JSON_TEXT)


def _coerce_skill_names(value: Any) -> list[str] | None:
    if not value:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return None


class ToolAuditRepository:
    """Repository for operational tool-call audit rows."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def log_tool_call(
        self,
        *,
        tool_name: str,
        status: str,
        source: str = "unknown",
        duration_ms: int | None = None,
        input_payload: Any = None,
        output_payload: Any = None,
        failure_class: str | None = None,
        error_message: str | None = None,
        error_code: str | None = None,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert one audit row, enriching from linked session/task when possible."""
        if status not in {"success", "failed", "blocked"}:
            status = "failed"
        ctx = dict(context or {})
        session_id = _uuid_or_none(ctx.get("session_id"))
        task_id = _uuid_or_none(ctx.get("task_id"))
        request_id = _uuid_or_none(ctx.get("request_id"))

        async with self.pool.acquire() as conn:
            if session_id:
                row = await conn.fetchrow(
                    """SELECT organization_id, user_id, agent_definition_id,
                              request_id, task_id, schedule_run_id,
                              model, reasoning_effort, engine
                       FROM llm_sessions
                       WHERE id = $1""",
                    session_id,
                )
                if row:
                    ctx.setdefault("organization_id", row["organization_id"])
                    ctx.setdefault("user_id", row["user_id"])
                    ctx.setdefault("agent_definition_id", row["agent_definition_id"])
                    ctx.setdefault("request_id", row["request_id"])
                    ctx.setdefault("task_id", row["task_id"])
                    ctx.setdefault("schedule_run_id", row["schedule_run_id"])
                    ctx.setdefault("model", row["model"])
                    ctx.setdefault("reasoning_effort", row["reasoning_effort"])
                    ctx.setdefault("engine", row["engine"])
                    task_id = task_id or _uuid_or_none(row["task_id"])
                    request_id = request_id or _uuid_or_none(row["request_id"])

            if task_id:
                row = await conn.fetchrow(
                    """SELECT organization_id, request_id, agent_definition_id,
                              agent_type, model, reasoning_effort
                       FROM tasks
                       WHERE id = $1""",
                    task_id,
                )
                if row:
                    ctx.setdefault("organization_id", row["organization_id"])
                    ctx.setdefault("request_id", row["request_id"])
                    ctx.setdefault("agent_definition_id", row["agent_definition_id"])
                    ctx.setdefault("agent_type", row["agent_type"])
                    ctx.setdefault("model", row["model"])
                    ctx.setdefault("reasoning_effort", row["reasoning_effort"])
                    request_id = request_id or _uuid_or_none(row["request_id"])

            if request_id and not ctx.get("user_id"):
                row = await conn.fetchrow(
                    "SELECT organization_id, created_by FROM requests WHERE id = $1",
                    request_id,
                )
                if row:
                    ctx.setdefault("organization_id", row["organization_id"])
                    ctx.setdefault("user_id", row["created_by"])

            row = await conn.fetchrow(
                """INSERT INTO tool_call_audit_log (
                       organization_id, user_id, api_key_id,
                       session_id, turn_id, message_id, llm_event_id,
                       request_id, task_id, schedule_run_id,
                       agent_definition_id, agent_type, skill_names,
                       model, reasoning_effort, engine, provider, source,
                       tool_name, tool_namespace, tool_call_id, mcp_server,
                       status, failure_class, error_message, error_code,
                       duration_ms, input_preview, output_preview, metadata
                   ) VALUES (
                       $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                       $11, $12, $13, $14, $15, $16, $17, $18,
                       $19, $20, $21, $22, $23, $24, $25, $26,
                       $27, $28, $29, $30
                   ) RETURNING *""",
                _uuid_or_none(ctx.get("organization_id")),
                _uuid_or_none(ctx.get("user_id")),
                _uuid_or_none(ctx.get("api_key_id")),
                session_id,
                _uuid_or_none(ctx.get("turn_id")),
                _uuid_or_none(ctx.get("message_id")),
                _uuid_or_none(ctx.get("llm_event_id")),
                _uuid_or_none(ctx.get("request_id")),
                task_id,
                _uuid_or_none(ctx.get("schedule_run_id")),
                _uuid_or_none(ctx.get("agent_definition_id")),
                ctx.get("agent_type"),
                _coerce_skill_names(ctx.get("skill_names")),
                ctx.get("model"),
                ctx.get("reasoning_effort"),
                ctx.get("engine"),
                ctx.get("provider"),
                source,
                tool_name,
                ctx.get("tool_namespace"),
                ctx.get("tool_call_id"),
                ctx.get("mcp_server"),
                status,
                failure_class,
                _redact_text(error_message, limit=_MAX_TEXT) if error_message else None,
                error_code,
                duration_ms,
                json.dumps(_redact_json(input_payload if input_payload is not None else {})),
                _redact_text(str(output_payload), limit=_MAX_TEXT)
                if output_payload is not None
                else None,
                json.dumps(_redact_json(metadata or {})),
            )
        return dict(row) if row else {}


def classify_tool_result(result_text: str | None) -> tuple[str, str | None, str | None]:
    """Classify a tool result string into audit status/failure metadata."""
    if not result_text:
        return "success", None, None
    text = str(result_text)
    lower = text.lower()
    if lower.startswith("error calling tool"):
        return "failed", "tool_error", text[:_MAX_TEXT]
    if "unauthorized" in lower or "status_code=401" in lower or "http 401" in lower:
        return "failed", "auth_error", text[:_MAX_TEXT]
    if "tool is not allowed" in lower or "blocked by hook" in lower:
        return "blocked", "blocked", text[:_MAX_TEXT]
    return "success", None, None
