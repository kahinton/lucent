"""Operational audit log for LLM/agent tool calls.

This repository intentionally writes to ``tool_call_audit_log`` instead of
memories. The rows are telemetry for pattern analysis: which tools fail, under
which models/agents/sessions, and with what redacted inputs/results.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from asyncpg import Pool

logger = logging.getLogger(__name__)

# Failure classes that map directly to the Pattern 1 / Pattern 2 remediation work
# on the memory-server. Emitting a structured log line for these — in addition to
# the audit row insert — gives operators a grep-friendly signal that
# `analyze_tool_failure_patterns` will surface on its next pass.
_REMEDIATION_FAILURE_CLASSES = frozenset(
    {
        "mcp_timeout",
        "db_pool_acquire_timeout",
        "auth_error",
        "forbidden",
        "rate_limited",
        "invalid_input",
    }
)

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
        # Observability hook (Patterns 1 + 2): emit a single structured line
        # whenever the audit row records a remediation-relevant failure. The
        # audit table itself is the source of truth for
        # ``analyze_tool_failure_patterns``; this log line gives operators a
        # grep-friendly signal in stdout/journalctl when regressions reappear.
        if status != "ok" and failure_class in _REMEDIATION_FAILURE_CLASSES:
            logger.warning(
                "tool_audit_failure tool=%s failure_class=%s status=%s "
                "duration_ms=%s source=%s",
                tool_name,
                failure_class,
                status,
                duration_ms,
                source,
                extra={
                    "event": "tool_audit_failure",
                    "tool_name": tool_name,
                    "failure_class": failure_class,
                    "status": status,
                    "duration_ms": duration_ms,
                    "source": source,
                },
            )
        return dict(row) if row else {}

    async def analyze_failure_patterns(
        self,
        *,
        org_id: str | UUID,
        since_days: int = 7,
        min_failures: int = 3,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Aggregate repeated tool failures into improvement candidates."""
        since_days = max(1, min(int(since_days or 7), 90))
        min_failures = max(2, min(int(min_failures or 3), 100))
        limit = max(1, min(int(limit or 20), 100))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, created_at, organization_id, user_id, session_id,
                          request_id, task_id, agent_definition_id, agent_type,
                          skill_names, model, reasoning_effort, engine, source,
                          tool_name, mcp_server, status, failure_class,
                          error_message, duration_ms, input_preview, output_preview,
                          metadata
                   FROM tool_call_audit_log
                   WHERE organization_id = $1::uuid
                     AND status IN ('failed', 'blocked')
                     AND created_at >= NOW() - ($2::text || ' days')::interval
                   ORDER BY created_at DESC
                   LIMIT 2000""",
                str(org_id),
                str(since_days),
            )

        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            tool = str(item.get("tool_name") or "unknown")
            failure = str(item.get("failure_class") or item.get("status") or "unknown")
            agent_key = str(item.get("agent_definition_id") or item.get("agent_type") or "")
            if agent_key:
                groups.setdefault(("agent", agent_key, tool, failure), []).append(item)
            for skill in item.get("skill_names") or []:
                groups.setdefault(("skill", str(skill), tool, failure), []).append(item)
            groups.setdefault(("tool", tool, tool, failure), []).append(item)

        patterns: list[dict[str, Any]] = []
        for (dimension, target, tool, failure), items in groups.items():
            if len(items) < min_failures:
                continue
            models = sorted({str(i.get("model")) for i in items if i.get("model")})
            agents = sorted({str(i.get("agent_type")) for i in items if i.get("agent_type")})
            skills = sorted({s for i in items for s in (i.get("skill_names") or [])})
            request_ids = sorted({str(i.get("request_id")) for i in items if i.get("request_id")})
            task_ids = sorted({str(i.get("task_id")) for i in items if i.get("task_id")})
            samples = [
                {
                    "id": str(i["id"]),
                    "created_at": i["created_at"].isoformat(),
                    "status": i.get("status"),
                    "error_message": i.get("error_message"),
                    "input_preview": i.get("input_preview"),
                    "output_preview": i.get("output_preview"),
                    "model": i.get("model"),
                    "agent_type": i.get("agent_type"),
                    "request_id": str(i["request_id"]) if i.get("request_id") else None,
                    "task_id": str(i["task_id"]) if i.get("task_id") else None,
                }
                for i in items[:3]
            ]
            recommended_action = _recommended_action_for_pattern(
                dimension=dimension,
                tool_name=tool,
                failure_class=failure,
            )
            patterns.append({
                "pattern_key": "|".join([dimension, target, tool, failure]),
                "dimension": dimension,
                "target": target,
                "tool_name": tool,
                "failure_class": failure,
                "failure_count": len(items),
                "first_seen_at": min(i["created_at"] for i in items).isoformat(),
                "last_seen_at": max(i["created_at"] for i in items).isoformat(),
                "affected_models": models,
                "affected_agents": agents,
                "affected_skills": skills,
                "sample_request_ids": request_ids[:10],
                "sample_task_ids": task_ids[:10],
                "sample_failures": samples,
                "recommended_action": recommended_action,
                "proposal_evidence": {
                    "source": "tool_call_audit_log",
                    "pattern_dimension": dimension,
                    "pattern_target": target,
                    "tool_name": tool,
                    "failure_class": failure,
                    "failure_count": len(items),
                    "since_days": since_days,
                    "sample_audit_ids": [str(i["id"]) for i in items[:10]],
                    "affected_models": models,
                    "affected_agents": agents,
                    "affected_skills": skills,
                    "sample_request_ids": request_ids[:10],
                    "sample_task_ids": task_ids[:10],
                },
            })

        patterns.sort(
            key=lambda p: (p["failure_count"], len(p["sample_task_ids"])),
            reverse=True,
        )
        return {
            "since_days": since_days,
            "min_failures": min_failures,
            "total_failed_rows_scanned": len(rows),
            "patterns": patterns[:limit],
        }


def classify_tool_result(
    result_text: str | None,
    *,
    tool_name: str | None = None,
    exit_code: int | None = None,
    runner_status: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Classify a tool result string into audit status/failure metadata.

    Pattern 2 hardening:
      * For ``tool_name='bash'`` (and other shell-runner tools), auth-class
        substrings in *stdout* are not by themselves a failure — investigative
        scripts routinely print other tools' error corpora (e.g. JSON of
        previous failures) which would otherwise trigger false-positive
        ``auth_error`` rows. Require ``exit_code != 0`` (or
        ``runner_status='failed'``) before labelling bash output as auth/rate
        failures.
      * Distinguish HTTP 401 (``auth_error``), 403 (``forbidden``), and 429
        (``rate_limited``) so ``analyze_tool_failure_patterns`` and operators
        can act on them differently (re-mint vs. permission gap vs. backoff).
    """
    if not result_text:
        return "success", None, None
    text = str(result_text)
    lower = text.lower()
    is_shell_tool = (tool_name or "").lower() in {"bash", "shell", "sh", "powershell"}
    shell_failed = exit_code not in (None, 0) or (runner_status or "").lower() == "failed"
    if lower.startswith("error calling tool") or lower.startswith("error:"):
        if is_shell_tool and not shell_failed and (
            "status_code=429" in lower
            or "http 429" in lower
            or "too many requests" in lower
            or "status_code=403" in lower
            or "http 403" in lower
            or "forbidden" in lower
            or "unauthorized" in lower
            or "status_code=401" in lower
            or "http 401" in lower
            or "invalid or expired credentials" in lower
        ):
            return "success", None, None
        # Pattern 1: typed timeout / pool / validation errors.
        if "mcptimeouterror" in lower or "timed out after" in lower:
            return "failed", "mcp_timeout", text[:_MAX_TEXT]
        if "dbpoolacquiretimeout" in lower or "pool acquire timeout" in lower:
            return "failed", "db_pool_acquire_timeout", text[:_MAX_TEXT]
        if "invalid input:" in lower:
            return "failed", "invalid_input", text[:_MAX_TEXT]
        # Pattern 2: distinct HTTP-status classes inside an MCP error envelope.
        if "status_code=429" in lower or "http 429" in lower or "too many requests" in lower:
            return "failed", "rate_limited", text[:_MAX_TEXT]
        if "status_code=403" in lower or "http 403" in lower or "forbidden" in lower:
            return "failed", "forbidden", text[:_MAX_TEXT]
        if (
            "unauthorized" in lower
            or "status_code=401" in lower
            or "http 401" in lower
            or "invalid or expired credentials" in lower
        ):
            return "failed", "auth_error", text[:_MAX_TEXT]
        return "failed", "tool_error", text[:_MAX_TEXT]
    if "unexpected user permission response" in lower:
        return "failed", "permission_protocol_error", text[:_MAX_TEXT]
    if "failed to fetch" in lower or "typeerror: fetch failed" in lower:
        return "failed", "fetch_error", text[:_MAX_TEXT]
    # Pattern 2 — bash false-positive guard. Only classify keyword-bearing
    # stdout as a real failure when the runner itself reported failure.
    if is_shell_tool and not shell_failed:
        return "success", None, None
    if "status_code=429" in lower or "http 429" in lower or "too many requests" in lower:
        return "failed", "rate_limited", text[:_MAX_TEXT]
    if "status_code=403" in lower or "http 403" in lower:
        return "failed", "forbidden", text[:_MAX_TEXT]
    if (
        "unauthorized" in lower
        or "status_code=401" in lower
        or "http 401" in lower
        or "invalid or expired credentials" in lower
    ):
        return "failed", "auth_error", text[:_MAX_TEXT]
    if "tool is not allowed" in lower or "blocked by hook" in lower:
        return "blocked", "blocked", text[:_MAX_TEXT]
    return "success", None, None


def _recommended_action_for_pattern(
    *,
    dimension: str,
    tool_name: str,
    failure_class: str,
) -> str:
    if dimension == "agent":
        return (
            f"Propose a focused skill or agent-definition update teaching the agent "
            f"how to call `{tool_name}` successfully and how to recover from "
            f"`{failure_class}` failures. Include this pattern evidence in the proposal."
        )
    if dimension == "skill":
        return (
            f"Propose an update/replacement skill that adds concrete `{tool_name}` usage "
            f"steps, common `{failure_class}` pitfalls, and verification criteria."
        )
    return (
        f"Investigate `{tool_name}` as a cross-agent tool reliability issue. Consider "
        "a generic skill, hook, or tool/schema improvement with this evidence."
    )
