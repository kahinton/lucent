"""Provider-reported LLM token usage persistence and reporting."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from asyncpg import Pool
from lucent.db.pool import scoped_acquire


def _uuid(value: str | UUID | None) -> UUID | None:
    return UUID(str(value)) if value else None


class TokenUsageRepository:
    """Append-only usage ledger with organization-scoped aggregations."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def record(
        self,
        *,
        organization_id: str | UUID,
        user_id: str | UUID | None,
        session_id: str | UUID | None,
        turn_id: str | UUID | None,
        message_id: str | UUID | None,
        model: str,
        engine: str,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        values = {
            key: max(0, int(usage.get(key) or 0))
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            )
        }
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """INSERT INTO llm_token_usage (
                       organization_id, user_id, session_id, turn_id, message_id,
                       model, engine, provider_call_id, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens, reasoning_tokens, provider_metadata
                   ) VALUES (
                       $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb
                   ) ON CONFLICT (session_id, provider_call_id)
                     WHERE session_id IS NOT NULL AND provider_call_id IS NOT NULL
                     DO NOTHING
                   RETURNING *""",
                _uuid(organization_id),
                _uuid(user_id),
                _uuid(session_id),
                _uuid(turn_id),
                _uuid(message_id),
                model[:128],
                engine[:32],
                usage.get("provider_call_id"),
                values["input_tokens"],
                values["output_tokens"],
                values["cache_read_tokens"],
                values["cache_write_tokens"],
                values["reasoning_tokens"],
                usage.get("provider_metadata") or {},
            )
        return dict(row) if row else {}

    async def get_session_usage(
        self,
        session_id: str | UUID,
        *,
        organization_id: str | UUID,
        user_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        """Most recent usage record for one chat session (org- and user-scoped).

        Context usage consumers read ``input_tokens`` from the latest call:
        each turn re-sends the full conversation, so the latest call's
        input_tokens approximates the model context currently in use.
        """
        clauses = ["session_id = $1", "organization_id = $2"]
        params: list[Any] = [_uuid(session_id), _uuid(organization_id)]
        if user_id is not None:
            params.append(_uuid(user_id))
            clauses.append(f"user_id = ${len(params)}")
        sql = f"""
            SELECT input_tokens,
                   output_tokens,
                   cache_read_tokens,
                   cache_write_tokens,
                   reasoning_tokens,
                   input_tokens + output_tokens AS total_tokens,
                   model,
                   created_at
            FROM llm_token_usage WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT 1
        """
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            row = await conn.fetchrow(sql, *params)
        return dict(row) if row else {}

    async def get_report(
        self,
        organization_id: str | UUID,
        *,
        user_id: str | UUID | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        params: list[Any] = [_uuid(organization_id)]
        clauses = ["organization_id = $1"]
        if user_id is not None:
            params.append(_uuid(user_id))
            clauses.append(f"user_id = ${len(params)}")
        if starts_at is not None:
            params.append(starts_at)
            clauses.append(f"created_at >= ${len(params)}")
        if ends_at is not None:
            params.append(ends_at)
            clauses.append(f"created_at < ${len(params)}")
        where = " AND ".join(clauses)
        summary_sql = f"""
            SELECT COUNT(*) AS call_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(GREATEST(input_tokens - cache_read_tokens, 0)), 0)
                       AS uncached_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
            FROM llm_token_usage WHERE {where}
        """
        model_sql = f"""
            SELECT model, engine, COUNT(*) AS call_count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(GREATEST(input_tokens - cache_read_tokens, 0)), 0)
                       AS uncached_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
            FROM llm_token_usage WHERE {where}
            GROUP BY model, engine
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC, model
        """
        user_model_sql = f"""
            SELECT u.user_id, COALESCE(users.display_name, users.email, 'Deleted user')
                       AS user_name, u.model, u.engine, u.call_count, u.input_tokens,
                   u.uncached_input_tokens, u.output_tokens, u.cache_read_tokens,
                   u.cache_write_tokens,
                   u.reasoning_tokens
            FROM (
                SELECT user_id, model, engine, COUNT(*) AS call_count,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(GREATEST(input_tokens - cache_read_tokens, 0)), 0)
                           AS uncached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
                FROM llm_token_usage WHERE {where}
                GROUP BY user_id, model, engine
            ) u LEFT JOIN users ON users.id = u.user_id
            ORDER BY u.input_tokens + u.output_tokens DESC, user_name, u.model
        """
        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            summary = await conn.fetchrow(summary_sql, *params)
            models = await conn.fetch(model_sql, *params)
            user_models = await conn.fetch(user_model_sql, *params)
        return {
            "summary": dict(summary),
            "by_model": [dict(row) for row in models],
            "by_user_model": [dict(row) for row in user_models],
        }
