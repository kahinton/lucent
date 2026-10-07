"""Connection-scoped repository for daemon service operations."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from lucent.constants import DECOMPOSITION_LOCK_NAMESPACE

if TYPE_CHECKING:
    # Annotation-only (``from __future__ import annotations`` makes the hint a
    # string). An eager import here would break under stubbed asyncpg — e.g.
    # tests that stub it down to just ``connect``.
    from asyncpg import Connection


class DaemonRepository:
    """Encapsulate SQL used by the daemon's direct connection paths."""

    def __init__(self, conn: Connection):
        self.conn = conn

    async def resolve_organization(self, organization: str) -> dict[str, Any] | None:
        row = await self.conn.fetchrow(
            "SELECT id, name FROM organizations "
            "WHERE id::text = $1 OR name = $1 LIMIT 1",
            organization,
        )
        return dict(row) if row else None

    async def list_organizations_except(self, system_org_name: str) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            "SELECT id, name FROM organizations WHERE name <> $1 ORDER BY created_at",
            system_org_name,
        )
        return [dict(row) for row in rows]

    async def set_daemon_scope(self, organization_id: str) -> None:
        await self.conn.execute(
            "SELECT set_config('app.user_id', '', false), "
            "set_config('app.org_id', $1, false), "
            "set_config('app.role', 'daemon', false);",
            organization_id,
        )

    async def set_organization_scope(self, organization_id: str) -> None:
        await self.conn.execute(
            "SELECT set_config('app.org_id', $1, false);",
            organization_id,
        )

    async def revoke_active_daemon_key(self, user_id: str, key_name: str) -> None:
        await self.conn.execute(
            "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
            "WHERE user_id = $1 AND name = $2 AND revoked_at IS NULL",
            user_id,
            key_name,
        )

    async def prune_revoked_daemon_keys(self, user_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM api_keys WHERE user_id = $1 "
            "AND name LIKE 'daemon-%' AND revoked_at IS NOT NULL "
            "AND id NOT IN ("
            "  SELECT id FROM api_keys WHERE user_id = $1 "
            "  AND name LIKE 'daemon-%' AND revoked_at IS NOT NULL "
            "  ORDER BY revoked_at DESC LIMIT 5"
            ")",
            user_id,
        )

    async def create_daemon_key(
        self,
        user_id: str,
        organization_id: str,
        name: str,
        key_prefix: str,
        key_hash: str,
        *,
        scopes: list[str],
        ttl_hours: int,
    ) -> dict[str, Any]:
        row = await self.conn.fetchrow(
            "INSERT INTO api_keys "
            "(user_id, organization_id, name, key_prefix, key_hash, scopes, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '1 hour' * $7) "
            "RETURNING id, expires_at",
            user_id,
            organization_id,
            name,
            key_prefix,
            key_hash,
            scopes,
            ttl_hours,
        )
        return dict(row)

    async def revoke_api_key(self, key_id: str) -> None:
        await self.conn.execute(
            "UPDATE api_keys SET is_active = false, revoked_at = NOW() "
            "WHERE id = $1 AND revoked_at IS NULL",
            key_id,
        )

    async def create_scoped_key(
        self,
        user_id: str,
        organization_id: str,
        name: str,
        key_prefix: str,
        key_hash: str,
        *,
        scopes: list[str],
        ttl_minutes: int,
        memory_scope: str,
        memory_scope_user_id: str | None,
    ) -> dict[str, Any]:
        row = await self.conn.fetchrow(
            "INSERT INTO api_keys "
            "(user_id, organization_id, name, key_prefix, key_hash, scopes, "
            " expires_at, memory_scope, memory_scope_user_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '1 minute' * $7, $8, $9) "
            "RETURNING id, expires_at",
            user_id,
            organization_id,
            name,
            key_prefix,
            key_hash,
            scopes,
            ttl_minutes,
            memory_scope,
            memory_scope_user_id,
        )
        return dict(row)

    async def get_request_owner_context(
        self,
        *,
        user_id: str,
        organization_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        user_row = await self.conn.fetchrow(
            """
            SELECT id, organization_id, display_name, email, role
            FROM users
            WHERE id = $1::uuid AND organization_id = $2::uuid
            """,
            user_id,
            organization_id,
        )
        memory_row = await self.conn.fetchrow(
            """
            SELECT id, username, type, content, tags, importance, metadata,
                   created_at, updated_at, user_id, organization_id
            FROM memories
            WHERE type = 'individual'
              AND deleted_at IS NULL
              AND user_id = $1::uuid
              AND organization_id = $2::uuid
            """,
            user_id,
            organization_id,
        )
        return (
            dict(user_row) if user_row else None,
            dict(memory_row) if memory_row else None,
        )

    async def disable_memory_consolidation_schedule(self, org_id: str) -> None:
        await self.conn.execute(
            """UPDATE schedules
               SET enabled = false,
                   status = 'completed',
                   updated_at = NOW()
               WHERE title = 'Memory Consolidation'
                 AND organization_id = $1::uuid
                 AND is_system = true""",
            org_id,
        )

    async def find_system_schedule(self, organization_id: str, title: str) -> dict[str, Any] | None:
        row = await self.conn.fetchrow(
            "SELECT id FROM schedules "
            "WHERE title = $1 AND organization_id = $2::uuid AND is_system = true",
            title,
            organization_id,
        )
        return dict(row) if row else None

    async def update_system_schedule(self, schedule_id: str, organization_id: str, values: dict[str, Any]) -> None:
        await self.conn.execute(
            """UPDATE schedules SET
                   description = $3,
                   agent_type = $4,
                   schedule_type = $5,
                   interval_seconds = $6,
                   cron_expression = $7,
                   priority = $8,
                   prompt = $9,
                   trigger_type = 'schedule',
                   trigger_config = ($10::text)::jsonb,
                   request_template = ($11::text)::jsonb,
                   actions = ($12::text)::jsonb,
                   review_instructions = $13,
                   updated_at = NOW()
               WHERE id = $1::uuid AND organization_id = $2::uuid
                 AND is_system = true""",
            schedule_id,
            organization_id,
            values["description"],
            values["agent_type"],
            values["schedule_type"],
            values["interval_seconds"],
            values["cron_expression"],
            values["priority"],
            values["prompt"],
            json.dumps(values["trigger_config"]),
            json.dumps(values["request_template"]),
            json.dumps(values["actions"]),
            values["review_instructions"],
        )

    async def create_system_schedule(self, values: dict[str, Any]) -> None:
        await self.conn.execute(
            """INSERT INTO schedules
               (title, organization_id, description, agent_type, schedule_type,
                interval_seconds, cron_expression, next_run_at, priority, prompt,
                created_by, is_system, enabled, trigger_type, trigger_config,
                request_template, actions, review_instructions)
               VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10,
                   $11::uuid, true, true, 'schedule', ($12::text)::jsonb,
                   ($13::text)::jsonb, ($14::text)::jsonb, $15)""",
            values["title"],
            values["organization_id"],
            values["description"],
            values["agent_type"],
            values["schedule_type"],
            values["interval_seconds"],
            values["cron_expression"],
            values["next_run_at"],
            values["priority"],
            values["prompt"],
            values["created_by"],
            json.dumps(values["trigger_config"]),
            json.dumps(values["request_template"]),
            json.dumps(values["actions"]),
            values["review_instructions"],
        )

    async def list_active_goal_users(self, org_id: str) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            """
            SELECT user_id::text AS user_id,
                   SUM(goals_scanned)::int AS goals_scanned,
                   SUM(rejections_pending)::int AS rejections_pending
            FROM (
                SELECT user_id,
                       COUNT(*)::int AS goals_scanned,
                       0 AS rejections_pending
                FROM memories
                WHERE organization_id = $1::uuid
                  AND type = 'goal'
                  AND lifecycle_stage = 'active'
                  AND COALESCE(metadata->>'status', '') = 'active'
                  AND user_id IS NOT NULL
                GROUP BY user_id
                UNION ALL
                SELECT created_by AS user_id,
                       0 AS goals_scanned,
                       COUNT(*)::int AS rejections_pending
                FROM requests
                WHERE organization_id = $1::uuid
                  AND status = 'rejection_processing'
                  AND created_by IS NOT NULL
                GROUP BY created_by
            ) AS combined
            GROUP BY user_id
            ORDER BY user_id
            """,
            org_id,
        )
        return [dict(row) for row in rows]

    async def list_experience_compression_users(self, org_id: str) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            """
            SELECT DISTINCT user_id::text AS user_id
            FROM memories
            WHERE organization_id = $1::uuid
              AND user_id IS NOT NULL
              AND type = 'experience'
              AND deleted_at IS NULL
              AND COALESCE(lifecycle_stage, 'active') = 'active'
              AND created_at < date_trunc('day', now())
              AND NOT (
                  COALESCE(tags, '{}'::text[])
                  && ARRAY[
                      'daily-digest', 'pinned', 'do_not_consolidate',
                      'heartbeat', 'state', 'telemetry'
                  ]::text[]
              )
            ORDER BY user_id
            """,
            org_id,
        )
        return [dict(row) for row in rows]

    async def list_learning_extraction_users(self, org_id: str) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            """
            SELECT DISTINCT user_id::text AS user_id
            FROM memories
            WHERE organization_id = $1::uuid
              AND user_id IS NOT NULL
              AND deleted_at IS NULL
              AND COALESCE(lifecycle_stage, 'active') = 'active'
              AND (
                  COALESCE(tags, '{}'::text[])
                  && ARRAY[
                      'daemon-result', 'rejection-lesson',
                      'feedback-rejected', 'feedback-approved', 'validated'
                  ]::text[]
              )
              AND NOT ('lesson-extracted' = ANY(COALESCE(tags, '{}'::text[])))
              AND NOT (
                  COALESCE(tags, '{}'::text[])
                  && ARRAY['heartbeat', 'state', 'telemetry']::text[]
              )
            ORDER BY user_id
            """,
            org_id,
        )
        return [dict(row) for row in rows]

    async def count_user_cognitive_requests_since(
        self,
        *,
        organization_id: str,
        user_id: str,
        since: Any,
    ) -> int:
        count = await self.conn.fetchval(
            """
            SELECT COUNT(*)
            FROM requests
            WHERE organization_id = $1::uuid
              AND created_by = $2::uuid
              AND source = 'cognitive'
              AND created_at >= $3
            """,
            organization_id,
            user_id,
            since,
        )
        return int(count or 0)

    async def notify_request_ready(self, request_id: str) -> None:
        await self.conn.execute("SELECT pg_notify('request_ready', $1)", str(request_id))

    async def list_rejection_processing_requests(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            """
            SELECT id::text AS id, title, approval_comment
            FROM requests
            WHERE organization_id = $1::uuid
              AND created_by = $2::uuid
              AND status = 'rejection_processing'
            ORDER BY created_at
            """,
            organization_id,
            user_id,
        )
        return [dict(row) for row in rows]

    async def list_backfill_decomposition_requests(
        self,
        organization_id: str,
        *,
        min_age_seconds: int,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Requests the decomposition backfill should turn into tasks.

        Same eligibility predicate as ``request_is_still_undecomposed``:
        a request still mid-queue (non-terminal status, no tasks yet) plus
        an age window so we don't race a planner that is about to
        decompose it itself.
        """
        conditions = [
            "r.organization_id = $1::uuid",
            "r.approval_status IN ('pending_approval', 'auto_approved', 'approved')",
            "r.status IN ('pending', 'in_progress')",
            "NOT EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id)",
            "r.created_at <= now() - make_interval(secs => $2)",
        ]
        params: list[str | int] = [organization_id, min_age_seconds]
        if request_id:
            params.append(request_id)
            conditions.append(f"r.id = ${len(params)}::uuid")
        rows = await self.conn.fetch(
            f"""
            SELECT id::text AS request_id, created_by::text AS created_by,
                   title, created_at
            FROM requests r
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at
            """,
            *params,
        )
        return [dict(row) for row in rows]

    async def acquire_decomposition_lock(self, request_id: str) -> bool:
        return bool(
            await self.conn.fetchval(
                "SELECT pg_try_advisory_lock($1, hashtext($2)::int)",
                DECOMPOSITION_LOCK_NAMESPACE,
                request_id,
            )
        )

    async def request_is_still_undecomposed(self, request_id: str) -> bool:
        return bool(
            await self.conn.fetchval(
                """
                SELECT 1
                FROM requests r
                WHERE r.id = $1::uuid
                  AND r.approval_status IN ('pending_approval', 'auto_approved', 'approved')
                  AND r.status IN ('pending', 'in_progress')
                  AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id)
                """,
                request_id,
            )
        )

    async def release_decomposition_lock(self, request_id: str) -> None:
        await self.conn.execute(
            "SELECT pg_advisory_unlock($1, hashtext($2)::int)",
            DECOMPOSITION_LOCK_NAMESPACE,
            request_id,
        )

    async def get_terminal_request_status(self, request_id: str) -> str | None:
        row = await self.conn.fetchrow(
            """
            SELECT status FROM requests
            WHERE id = $1::uuid
              AND status IN ('cancelled', 'completed', 'failed')
            """,
            request_id,
        )
        return str(row["status"]) if row else None

    async def count_tasks_for_request(self, request_id: str) -> int:
        count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE request_id = $1::uuid",
            request_id,
        )
        return int(count or 0)

    async def get_review_requester(
        self,
        requester_user_id: str,
        org_id: str,
    ) -> dict[str, Any] | None:
        row = await self.conn.fetchrow(
            "SELECT id::text AS id, role, external_id "
            "FROM users "
            "WHERE id = $1::uuid AND organization_id = $2::uuid",
            requester_user_id,
            org_id,
        )
        return dict(row) if row else None

    async def get_review_human_owner(self, org_id: str) -> dict[str, Any] | None:
        row = await self.conn.fetchrow(
            "SELECT id::text AS id FROM users "
            "WHERE organization_id = $1::uuid "
            "  AND is_active = true "
            "  AND role IN ('owner', 'admin') "
            "  AND COALESCE(external_id, '') <> 'daemon-service' "
            "ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at "
            "LIMIT 1",
            org_id,
        )
        return dict(row) if row else None

    async def get_github_credential(
        self,
        *,
        organization_id: str,
        owner_user_id: str,
    ) -> dict[str, Any] | None:
        row = await self.conn.fetchrow(
            """
            SELECT id, encrypted_secret_payload
            FROM enterprise_credentials
            WHERE integration_type = 'github'
              AND scope_type = 'user'
              AND owner_user_id = $1::uuid
              AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            owner_user_id,
        )
        return dict(row) if row else None

    async def has_later_reusable_sandbox_task(
        self,
        *,
        request_id: str,
        task_id: str,
        sequence_order: int,
    ) -> bool:
        return bool(
            await self.conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM tasks
                    WHERE request_id = $1::uuid
                      AND id != $2::uuid
                      AND sequence_order > $3
                      AND status IN ('pending', 'planned', 'running', 'needs_review')
                      AND COALESCE(
                          (sandbox_config->>'reuse_within_request')::boolean,
                          false
                      )
                )
                """,
                request_id,
                task_id,
                sequence_order,
            )
        )

    async def count_owned_active_tasks(self, instance_id: str) -> int:
        count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM tasks "
            "WHERE claimed_by = $1 AND status IN ('claimed', 'running')",
            instance_id,
        )
        return int(count or 0)

    async def listen_connection_alive(self) -> bool:
        return bool(await self.conn.fetchval("SELECT 1"))

    async def has_schedules(self) -> bool:
        return bool(
            await self.conn.fetchval("SELECT 1 FROM schedules LIMIT 1")
        )
