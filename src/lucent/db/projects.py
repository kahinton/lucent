"""Persistence for Projects — user-owned workspaces grouping chats, files, and
standing instructions.

Tenant isolation is enforced by construction at this layer (Kyle's standing
data-layer directive, 2026-09-10): every public method REQUIRES an explicit
``org_id`` + ``user_id`` pair, validates them non-empty via ``_require_scope``
before any SQL runs, and every statement references them structurally — a
missing scope is impossible to express, not merely discouraged. There is
deliberately no org-wide or unscoped query variant anywhere in this module.

Membership model (migration 111): ``llm_sessions.project_id`` is the
authoritative membership anchor; ``llm_messages.project_id`` is a
denormalized mirror for per-turn context scoping, maintained whenever a
session moves; ``user_files.project_id`` files durable files into the
workspace. Migration 112 extends the same single-membership pattern to
``memories.project_id`` (goal/technical/experience/individual attachments)
and ``user_interactions.project_id`` (handoffs). NULL everywhere means
unfiled. Deleting a project un-files (SET NULL) — it never deletes chats,
files, memories, or handoffs.

Per-turn injection (Projects v2, 2026-09-12): the chat caller re-reads
:func:`get_context_for_session` every turn (fail-closed, scoped) and threads
it via ``session_state["_project_context"]``; the engine renders a
metadata-only standing block (lucent.llm.project_context) into the system
prompt. No hook mediates injection anymore.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from asyncpg import Pool
from lucent.db.pool import scoped_acquire


class ProjectNotFoundError(LookupError):
    """Raised when a project does not exist for the scoped (org, user) pair."""


def _require_scope(org_id: Any, user_id: Any) -> tuple[UUID, UUID]:
    """Validate that a scoping pair is present and well-formed.

    Called by every repository method before touching SQL. A missing scope is
    a caller bug and fails loudly here (TypeError) instead of silently
    widening the query to the whole organization.
    """
    if org_id is None or user_id is None:
        raise TypeError(
            "Project queries are user-scoped by construction: "
            "org_id and user_id are both required — refusing to run unscoped"
        )
    return UUID(str(org_id)), UUID(str(user_id))


def _uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


class ProjectRepository:
    """All Projects SQL lives here, mandatory (org_id, user_id)-scoped.

    The pair is non-optional on every method, referenced in every statement's
    WHERE or INSERT, and validated up front — tenant scoping holds even for
    the write path's verification reads.
    """

    def __init__(self, pool: Pool):
        self.pool = pool

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    async def create(
        self, *, org_id: str | UUID, user_id: str | UUID, name: str,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """INSERT INTO projects (organization_id, user_id, name, instructions)
                   VALUES ($1, $2, $3, $4)
                   RETURNING *""",
                org,
                user,
                name.strip(),
                instructions,
            )
        return dict(row)

    async def get_owned(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID
    ) -> dict[str, Any] | None:
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """SELECT * FROM projects
                   WHERE id = $1 AND organization_id = $2 AND user_id = $3""",
                _uuid(project_id),
                org,
                user,
            )
        return dict(row) if row else None

    async def get_owned_with_counts(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID
    ) -> dict[str, Any] | None:
        """One owned project plus its member counts (single query)."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """SELECT p.*,
                          (SELECT COUNT(*) FROM llm_sessions s
                             WHERE s.project_id = p.id
                               AND s.status NOT IN ('archived', 'deleted'))
                              AS session_count,
                          (SELECT COUNT(*) FROM user_files f
                             WHERE f.project_id = p.id AND f.deleted_at IS NULL)
                              AS file_count
                   FROM projects p
                   WHERE p.id = $1 AND p.organization_id = $2 AND p.user_id = $3""",
                _uuid(project_id),
                org,
                user,
            )
        return dict(row) if row else None

    async def rename(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
        name: str,
    ) -> dict[str, Any] | None:
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """UPDATE projects
                   SET name = $4, updated_at = NOW()
                   WHERE id = $1 AND organization_id = $2 AND user_id = $3
                   RETURNING *""",
                _uuid(project_id),
                org,
                user,
                name.strip(),
            )
        return dict(row) if row else None

    async def set_instructions(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
        instructions: str | None,
    ) -> dict[str, Any] | None:
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """UPDATE projects
                   SET instructions = $4, updated_at = NOW()
                   WHERE id = $1 AND organization_id = $2 AND user_id = $3
                   RETURNING *""",
                _uuid(project_id),
                org,
                user,
                instructions,
            )
        return dict(row) if row else None

    async def delete_owned(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID
    ) -> bool:
        """Delete the project; member chats/files un-file via ON DELETE SET NULL."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """DELETE FROM projects
                   WHERE id = $1 AND organization_id = $2 AND user_id = $3
                   RETURNING id""",
                _uuid(project_id),
                org,
                user,
            )
        return row is not None

    async def list_owned(
        self, *, org_id: str | UUID, user_id: str | UUID,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM projects
                   WHERE organization_id = $1 AND user_id = $2""",
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT p.*,
                          (SELECT COUNT(*) FROM llm_sessions s
                             WHERE s.project_id = p.id
                               AND s.status NOT IN ('archived', 'deleted'))
                              AS session_count,
                          (SELECT COUNT(*) FROM user_files f
                             WHERE f.project_id = p.id AND f.deleted_at IS NULL)
                              AS file_count
                   FROM projects p
                   WHERE p.organization_id = $1 AND p.user_id = $2
                   ORDER BY p.updated_at DESC
                   LIMIT $3 OFFSET $4""",
                org,
                user,
                limit,
                offset,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total_count,
        }

    # ------------------------------------------------------------------
    # Membership — sessions (chats)
    # ------------------------------------------------------------------

    async def _require_project(
        self, conn: Any, project_id: str | UUID, org: UUID, user: UUID
    ) -> UUID:
        row = await conn.fetchrow(
            """SELECT id FROM projects
               WHERE id = $1 AND organization_id = $2 AND user_id = $3""",
            _uuid(project_id),
            org,
            user,
        )
        if not row:
            raise ProjectNotFoundError(
                f"Project {project_id} not found for user {user} in org {org}"
            )
        return row["id"]

    async def _project_exists(self, conn: Any, project_id: Any, org: UUID, user: UUID) -> bool:
        return bool(
            await conn.fetchval(
                """SELECT 1 FROM projects
                   WHERE id = $1 AND organization_id = $2 AND user_id = $3""",
                project_id,
                org,
                user,
            )
        )

    async def set_session_project(
        self,
        session_id: str | UUID,
        project_id: str | UUID | None,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> dict[str, Any] | None:
        """Move one chat into (or out of, project_id=None) a project.

        In one transaction: verifies the target project is owned by the same
        (org, user) pair as the session's own scoping, updates
        llm_sessions.project_id, and mirrors the value onto the session's
        llm_messages (the denormalized copy message-phase scoping reads).
        """
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            async with conn.transaction():
                if project_id is not None:
                    await self._require_project(conn, project_id, org, user)
                row = await conn.fetchrow(
                    """UPDATE llm_sessions
                       SET project_id = $3, updated_at = NOW()
                       WHERE id = $1 AND organization_id = $2 AND user_id = $4
                       RETURNING *""",
                    _uuid(session_id),
                    org,
                    _uuid(project_id),
                    user,
                )
                if not row:
                    return None
                await conn.execute(
                    """UPDATE llm_messages
                       SET project_id = $2
                       WHERE session_id = $1""",
                    row["id"],
                    _uuid(project_id),
                )
        return dict(row)

    async def set_sessions_project_bulk(
        self,
        session_ids: list[str | UUID],
        project_id: str | UUID | None,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> int:
        """Bulk move chats into (or out of, project_id=None) a project.

        Same ownership contract as :meth:`set_session_project`. Returns the
        number of sessions actually moved; ids that do not belong to the
        caller are silently not-moved (never another user's data), and the
        caller surfaces the mismatch via the moved-count response.
        """
        org, user = _require_scope(org_id, user_id)
        if not session_ids:
            return 0
        session_uuids = [_uuid(s) for s in session_ids]
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            async with conn.transaction():
                moved = await conn.fetchval(
                    """WITH moved AS (
                           UPDATE llm_sessions
                           SET project_id = $3, updated_at = NOW()
                           WHERE id = ANY($1::uuid[])
                             AND organization_id = $2
                             AND user_id = $4
                             AND ($3::uuid IS NULL OR EXISTS (
                                  SELECT 1 FROM projects p
                                   WHERE p.id = $3
                                     AND p.organization_id = llm_sessions.organization_id
                                     AND p.user_id = llm_sessions.user_id))
                       RETURNING id)
                       SELECT COUNT(*) FROM moved""",
                    session_uuids,
                    org,
                    _uuid(project_id),
                    user,
                )
                if moved:
                    await conn.execute(
                        """UPDATE llm_messages
                           SET project_id = $2
                           WHERE session_id = ANY($1::uuid[])""",
                        session_uuids,
                        _uuid(project_id),
                    )
        return int(moved or 0)

    async def list_sessions_in_project(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """Chat sessions filed into a project, newest-activity first.

        The project is verified owned by the caller first, then the listing is
        (org, user)-scoped on llm_sessions itself.
        """
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            await self._require_project(conn, project_id, org, user)
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM llm_sessions
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND status NOT IN ('archived', 'deleted')""",
                _uuid(project_id),
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT s.*, COUNT(m.id) AS message_count
                   FROM llm_sessions s
                   LEFT JOIN llm_messages m ON m.session_id = s.id
                   WHERE s.project_id = $1 AND s.organization_id = $2
                     AND s.user_id = $3
                     AND s.status NOT IN ('archived', 'deleted')
                   GROUP BY s.id
                   ORDER BY COALESCE(s.last_message_at, s.updated_at, s.created_at) DESC
                   LIMIT $4 OFFSET $5""",
                _uuid(project_id),
                org,
                user,
                limit,
                offset,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_context_for_session(
        self, session_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
    ) -> dict[str, Any] | None:
        """Project context for a chat session's turn (system-prompt injection).

        Returns the project the chat is filed into — id, name, standing
        instructions — plus its durable files (metadata only, content stays
        behind the storage provider) and the ids of memories attached to the
        project (ids only: used as a ranking boost key by the message-memory
        hook — no memory content flows through this path), scoped fail-closed
        to the calling ``(org, user)`` pair on both the session and the
        project side. Returns ``None`` when the chat is unfiled or does not
        belong to the caller, so callers treat that as plain "no project
        context" — never as a fallback to another tenant's data.

        File ``metadata`` rides along so the engine-side project block can
        render a one-line description per manifest entry; it is still
        metadata, never file content.
        """
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """SELECT p.id, p.name, p.instructions, p.updated_at
                   FROM llm_sessions s
                   JOIN projects p ON p.id = s.project_id
                    AND p.organization_id = s.organization_id
                    AND p.user_id = s.user_id
                   WHERE s.id = $1
                     AND s.organization_id = $2
                     AND s.user_id = $3
                     AND s.status <> 'deleted'""",
                _uuid(session_id),
                org,
                user,
            )
            if not row:
                return None
            files = await conn.fetch(
                """SELECT id, filename, display_name, mime_type, size_bytes, metadata,
                          updated_at
                   FROM user_files
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND deleted_at IS NULL
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC""",
                row["id"],
                org,
                user,
            )
            memory_rows = await conn.fetch(
                """SELECT id FROM memories
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND deleted_at IS NULL""",
                row["id"],
                org,
                user,
            )
        return {
            "project": dict(row),
            "files": [dict(f) for f in files],
            "memory_ids": [str(m["id"]) for m in memory_rows],
        }


    async def set_file_project(
        self,
        file_id: str | UUID,
        project_id: str | UUID | None,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> dict[str, Any] | None:
        """File one durable file into (or out of, project_id=None) a project."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            async with conn.transaction():
                if project_id is not None:
                    await self._require_project(conn, project_id, org, user)
                row = await conn.fetchrow(
                    """UPDATE user_files
                       SET project_id = $3, updated_at = NOW()
                       WHERE id = $1 AND organization_id = $2 AND user_id = $4
                         AND deleted_at IS NULL
                       RETURNING *""",
                    _uuid(file_id),
                    org,
                    _uuid(project_id),
                    user,
                )
        return dict(row) if row else None

    async def set_files_project_bulk(
        self,
        file_ids: list[str | UUID],
        project_id: str | UUID | None,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> int:
        """Bulk file durable files into (or out of, project_id=None) a project."""
        org, user = _require_scope(org_id, user_id)
        if not file_ids:
            return 0
        file_uuids = [_uuid(f) for f in file_ids]
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            moved = await conn.fetchval(
                """WITH moved AS (
                       UPDATE user_files
                       SET project_id = $3, updated_at = NOW()
                       WHERE id = ANY($1::uuid[])
                         AND organization_id = $2
                         AND user_id = $4
                         AND deleted_at IS NULL
                         AND ($3::uuid IS NULL OR EXISTS (
                              SELECT 1 FROM projects p
                               WHERE p.id = $3
                                 AND p.organization_id = user_files.organization_id
                                 AND p.user_id = user_files.user_id))
                   RETURNING id)
                   SELECT COUNT(*) FROM moved""",
                file_uuids,
                org,
                _uuid(project_id),
                user,
            )
        return int(moved or 0)

    async def list_files_in_project(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """Durable files filed into a project, newest first."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            await self._require_project(conn, project_id, org, user)
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM user_files
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND deleted_at IS NULL""",
                _uuid(project_id),
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT * FROM user_files
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC
                   LIMIT $4 OFFSET $5""",
                _uuid(project_id),
                org,
                user,
                limit,
                offset,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total_count,
        }
    # ------------------------------------------------------------------
    # Unfiled listings — pickers for the "move into project" UI
    # ------------------------------------------------------------------

    async def list_unfiled_sessions(
        self, *, org_id: str | UUID, user_id: str | UUID, limit: int = 100,
    ) -> dict[str, Any]:
        """Active non-archived chats with project_id IS NULL, for move pickers."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM llm_sessions
                   WHERE organization_id = $1 AND user_id = $2
                     AND project_id IS NULL
                     AND status NOT IN ('archived', 'deleted')""",
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT s.id, s.title, s.model, s.status, s.created_at,
                          s.updated_at, s.last_message_at,
                          (SELECT COUNT(*) FROM llm_messages m
                             WHERE m.session_id = s.id) AS message_count
                   FROM llm_sessions s
                   WHERE s.organization_id = $1 AND s.user_id = $2
                     AND s.project_id IS NULL
                     AND s.status NOT IN ('archived', 'deleted')
                   ORDER BY COALESCE(s.last_message_at, s.updated_at, s.created_at) DESC
                   LIMIT $3""",
                org,
                user,
                limit,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": 0,
            "has_more": total_count > len(rows),
        }

    async def list_unfiled_files(
        self, *, org_id: str | UUID, user_id: str | UUID, limit: int = 100,
    ) -> dict[str, Any]:
        """Non-deleted durable files with project_id IS NULL, for move pickers."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM user_files
                   WHERE organization_id = $1 AND user_id = $2
                     AND project_id IS NULL
                     AND deleted_at IS NULL""",
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT id, filename, display_name, mime_type, size_bytes, created_at
                   FROM user_files
                   WHERE organization_id = $1 AND user_id = $2
                     AND project_id IS NULL
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC
                   LIMIT $3""",
                org,
                user,
                limit,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": 0,
            "has_more": total_count > len(rows),
        }

    # ------------------------------------------------------------------
    # Membership — memories and handoffs (user_interactions)
    # ------------------------------------------------------------------

    async def set_memories_project_bulk(
        self,
        memory_ids: list[str | UUID],
        project_id: str | UUID | None,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> int:
        """Bulk attach memories to (or detach from, project_id=None) a project.

        Same ownership contract as :meth:`set_files_project_bulk`: only rows
        owned by the caller move (memories carry their own organization_id +
        user_id since migrations 002/004), the target project is verified
        owned by the same pair, and the moved/requested counts let the caller
        report the truth without distinguishing other users' ids.
        """
        org, user = _require_scope(org_id, user_id)
        if not memory_ids:
            return 0
        memory_uuids = [_uuid(m) for m in memory_ids]
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            moved = await conn.fetchval(
                """WITH moved AS (
                       UPDATE memories
                       SET project_id = $3, updated_at = NOW()
                       WHERE id = ANY($1::uuid[])
                         AND organization_id = $2
                         AND user_id = $4
                         AND deleted_at IS NULL
                         AND ($3::uuid IS NULL OR EXISTS (
                              SELECT 1 FROM projects p
                               WHERE p.id = $3
                                 AND p.organization_id = memories.organization_id
                                 AND p.user_id = memories.user_id))
                   RETURNING id)
                   SELECT COUNT(*) FROM moved""",
                memory_uuids,
                org,
                _uuid(project_id),
                user,
            )
        return int(moved or 0)

    async def list_memories_in_project(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """Memories attached to a project, newest update first.

        The project is verified owned by the caller first, then the listing
        is (org, user)-scoped on memories itself — an individual memory is
        private to its owner, so this never crosses the caller boundary.
        """
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            await self._require_project(conn, project_id, org, user)
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM memories
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND deleted_at IS NULL""",
                _uuid(project_id),
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT id, type, content, tags, importance, metadata,
                          created_at, updated_at
                   FROM memories
                   WHERE project_id = $1 AND organization_id = $2 AND user_id = $3
                     AND deleted_at IS NULL
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC
                   LIMIT $4 OFFSET $5""",
                _uuid(project_id),
                org,
                user,
                limit,
                offset,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total_count,
        }

    async def list_unfiled_memories(
        self, *, org_id: str | UUID, user_id: str | UUID, limit: int = 100,
    ) -> dict[str, Any]:
        """Active memories with project_id IS NULL, for the attach picker."""
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM memories
                   WHERE organization_id = $1 AND user_id = $2
                     AND project_id IS NULL
                     AND deleted_at IS NULL""",
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT id, type, content, tags, importance, created_at, updated_at
                   FROM memories
                   WHERE organization_id = $1 AND user_id = $2
                     AND project_id IS NULL
                     AND deleted_at IS NULL
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC
                   LIMIT $3""",
                org,
                user,
                limit,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": 0,
            "has_more": total_count > len(rows),
        }

    async def set_interactions_project_bulk(
        self,
        interaction_ids: list[str | UUID],
        project_id: str | UUID | None,
        *,
        org_id: str | UUID,
        user_id: str | UUID,
    ) -> int:
        """Bulk file handoffs (user_interactions) into/out of a project.

        Handoffs are strictly per-user private: only the caller's own rows
        are ever touchable, and filing a handoff into a project is grouping
        only — it never changes visibility. Same ownership contract as the
        session/file/memory bulk moves: ids the caller cannot see are
        silently not-moved (never another user's data) and the moved-count
        reports the truth.
        """
        org, user = _require_scope(org_id, user_id)
        if not interaction_ids:
            return 0
        interaction_uuids = [_uuid(i) for i in interaction_ids]
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            moved = await conn.fetchval(
                """WITH moved AS (
                           UPDATE user_interactions
                           SET project_id = $3, updated_at = NOW()
                           WHERE id = ANY($1::uuid[])
                             AND organization_id = $2
                             AND user_id = $4
                             AND ($3::uuid IS NULL OR EXISTS (
                                  SELECT 1 FROM projects p
                                   WHERE p.id = $3
                                     AND p.organization_id = user_interactions.organization_id
                                     AND p.user_id = $4))
                       RETURNING id)
                       SELECT COUNT(*) FROM moved""",
                interaction_uuids,
                org,
                _uuid(project_id),
                user,
            )
        return int(moved or 0)

    async def list_interactions_in_project(
        self, project_id: str | UUID, *, org_id: str | UUID, user_id: str | UUID,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """Handoffs filed into a project, newest update first.

        Handoffs are strictly per-user private: only the caller's own
        handoffs are visible; other members' handoffs stay invisible even
        inside the same project.
        """
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            await self._require_project(conn, project_id, org, user)
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM user_interactions
                   WHERE project_id = $1 AND organization_id = $2
                     AND user_id = $3""",
                _uuid(project_id),
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT id, source, interaction_type, status, priority,
                          title, body, requires_response, user_id,
                          created_at, updated_at
                   FROM user_interactions
                   WHERE project_id = $1 AND organization_id = $2
                     AND user_id = $3
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC
                   LIMIT $4 OFFSET $5""",
                _uuid(project_id),
                org,
                user,
                limit,
                offset,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total_count,
        }

    async def list_unfiled_interactions(
        self, *, org_id: str | UUID, user_id: str | UUID, limit: int = 100,
    ) -> dict[str, Any]:
        """Handoffs with project_id IS NULL and open-enough status, for pickers.

        Handoffs are strictly per-user private: only the caller's own rows
        appear. Filing a handoff into a project is grouping only — it never
        changes visibility; the listing visibility rule above is unchanged.
        """
        org, user = _require_scope(org_id, user_id)
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM user_interactions
                   WHERE organization_id = $1
                     AND user_id = $2
                     AND project_id IS NULL
                     AND status IN ('open', 'waiting_on_user', 'responded')""",
                org,
                user,
            )
            rows = await conn.fetch(
                """SELECT id, source, interaction_type, status, priority,
                          title, body, requires_response, user_id,
                          created_at, updated_at
                   FROM user_interactions
                   WHERE organization_id = $1
                     AND user_id = $2
                     AND project_id IS NULL
                     AND status IN ('open', 'waiting_on_user', 'responded')
                   ORDER BY updated_at DESC NULLS LAST, created_at DESC
                   LIMIT $3""",
                org,
                user,
                limit,
            )
        total_count = int(total or 0)
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "limit": limit,
            "offset": 0,
            "has_more": total_count > len(rows),
        }
