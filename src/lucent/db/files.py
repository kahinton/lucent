"""Persistence for user-owned file metadata."""

from __future__ import annotations

import json
from typing import Any

from asyncpg import Pool


class UserFileRepository:
    def __init__(self, pool: Pool):
        self.pool = pool

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        item = dict(row)
        metadata = item.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        item["metadata"] = metadata
        item["url"] = f"/files/{item['id']}"
        item["content_url"] = f"/files/{item['id']}/content"
        return item

    async def create(self, **values: Any) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """INSERT INTO user_files (
                           organization_id, user_id, created_by, request_id, task_id,
                           origin_session_id, origin_turn_id, origin_message_id,
                           provider, storage_key, filename, display_name, mime_type,
                           size_bytes, checksum_sha256, metadata
                       ) VALUES (
                           $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                           $6::uuid, $7::uuid, $8::uuid,
                           $9, $10, $11, $12, $13, $14, $15, $16::jsonb
                       ) RETURNING *""",
                    values["org_id"],
                    values["user_id"],
                    values.get("created_by"),
                    values.get("request_id"),
                    values.get("task_id"),
                    values.get("session_id"),
                    values.get("turn_id"),
                    values.get("message_id"),
                    values["provider"],
                    values["storage_key"],
                    values["filename"],
                    values["display_name"],
                    values["mime_type"],
                    values["size_bytes"],
                    values["checksum_sha256"],
                    json.dumps(values.get("metadata") or {}),
                )
                await conn.execute(
                    """INSERT INTO user_file_revisions (
                           file_id, organization_id, user_id, revision_number,
                           edited_by, provider, storage_key, size_bytes,
                           checksum_sha256, change_summary, session_id, turn_id,
                           message_id, request_id, task_id
                       ) VALUES (
                           $1::uuid, $2::uuid, $3::uuid, 1, $4::uuid, $5, $6,
                           $7, $8, $9, $10::uuid, $11::uuid, $12::uuid,
                           $13::uuid, $14::uuid
                       )""",
                    str(row["id"]),
                    values["org_id"],
                    values["user_id"],
                    values.get("created_by"),
                    values["provider"],
                    values["storage_key"],
                    values["size_bytes"],
                    values["checksum_sha256"],
                    values.get("change_summary") or "Created file",
                    values.get("session_id"),
                    values.get("turn_id"),
                    values.get("message_id"),
                    values.get("request_id"),
                    values.get("task_id"),
                )
        return self._to_dict(row)

    async def get_owned(self, file_id: str, org_id: str, user_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM user_files
                   WHERE id = $1::uuid AND organization_id = $2::uuid
                     AND user_id = $3::uuid AND deleted_at IS NULL""",
                file_id,
                org_id,
                user_id,
            )
        return self._to_dict(row) if row else None

    async def append_revision(self, **values: Any) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                file_row = await conn.fetchrow(
                    """SELECT * FROM user_files
                       WHERE id = $1::uuid AND organization_id = $2::uuid
                         AND user_id = $3::uuid AND deleted_at IS NULL
                       FOR UPDATE""",
                    values["file_id"],
                    values["org_id"],
                    values["user_id"],
                )
                if not file_row:
                    return None
                revision_number = int(file_row["current_revision"]) + 1
                revision = await conn.fetchrow(
                    """INSERT INTO user_file_revisions (
                           file_id, organization_id, user_id, revision_number,
                           edited_by, provider, storage_key, size_bytes,
                           checksum_sha256, change_summary, session_id, turn_id,
                           message_id, request_id, task_id
                       ) VALUES (
                           $1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6, $7,
                           $8, $9, $10, $11::uuid, $12::uuid, $13::uuid,
                           $14::uuid, $15::uuid
                       ) RETURNING *""",
                    values["file_id"],
                    values["org_id"],
                    values["user_id"],
                    revision_number,
                    values.get("edited_by"),
                    values["provider"],
                    values["storage_key"],
                    values["size_bytes"],
                    values["checksum_sha256"],
                    values.get("change_summary"),
                    values.get("session_id"),
                    values.get("turn_id"),
                    values.get("message_id"),
                    values.get("request_id") or file_row["request_id"],
                    values.get("task_id") or file_row["task_id"],
                )
                await conn.execute(
                    """UPDATE user_files
                       SET provider = $2, storage_key = $3, size_bytes = $4,
                           checksum_sha256 = $5, current_revision = $6,
                           updated_at = NOW()
                       WHERE id = $1::uuid""",
                    values["file_id"],
                    values["provider"],
                    values["storage_key"],
                    values["size_bytes"],
                    values["checksum_sha256"],
                    revision_number,
                )
        return self._revision_to_dict(revision)

    @staticmethod
    def _revision_to_dict(row: Any) -> dict[str, Any]:
        item = dict(row)
        session_id = item.get("session_id")
        request_id = item.get("request_id")
        task_id = item.get("task_id")
        item["context_url"] = (
            f"/chat/{session_id}"
            if session_id
            else f"/activity/{request_id}#task-{task_id}"
            if request_id and task_id
            else f"/activity/{request_id}"
            if request_id
            else None
        )
        return item

    async def list_revisions_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT r.*, s.title AS session_title,
                          req.title AS request_title, t.title AS task_title
                   FROM user_file_revisions r
                   JOIN user_files f ON f.id = r.file_id
                   LEFT JOIN llm_sessions s ON s.id = r.session_id
                   LEFT JOIN requests req ON req.id = r.request_id
                   LEFT JOIN tasks t ON t.id = r.task_id
                   WHERE r.file_id = $1::uuid
                     AND f.organization_id = $2::uuid AND f.user_id = $3::uuid
                     AND f.deleted_at IS NULL
                   ORDER BY r.revision_number DESC""",
                file_id,
                org_id,
                user_id,
            )
        return [self._revision_to_dict(row) for row in rows]

    async def get_revision_owned(
        self,
        file_id: str,
        revision_number: int,
        org_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT r.*, s.title AS session_title,
                          req.title AS request_title, t.title AS task_title
                   FROM user_file_revisions r
                   JOIN user_files f ON f.id = r.file_id
                   LEFT JOIN llm_sessions s ON s.id = r.session_id
                   LEFT JOIN requests req ON req.id = r.request_id
                   LEFT JOIN tasks t ON t.id = r.task_id
                   WHERE r.file_id = $1::uuid AND r.revision_number = $2
                     AND f.organization_id = $3::uuid AND f.user_id = $4::uuid
                     AND f.deleted_at IS NULL""",
                file_id,
                revision_number,
                org_id,
                user_id,
            )
        return self._revision_to_dict(row) if row else None

    async def list_storage_keys_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> list[tuple[str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT DISTINCT r.provider, r.storage_key
                   FROM user_file_revisions r
                   JOIN user_files f ON f.id = r.file_id
                   WHERE r.file_id = $1::uuid
                     AND f.organization_id = $2::uuid AND f.user_id = $3::uuid""",
                file_id,
                org_id,
                user_id,
            )
        return [(row["provider"], row["storage_key"]) for row in rows]

    async def list_owned(
        self, org_id: str, user_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                """SELECT COUNT(*) FROM user_files
                   WHERE organization_id = $1::uuid AND user_id = $2::uuid
                     AND deleted_at IS NULL""",
                org_id,
                user_id,
            )
            unseen_count = await conn.fetchval(
                                """SELECT COUNT(*)
                                     FROM user_files f
                                     LEFT JOIN user_file_views v
                                         ON v.file_id = f.id
                                        AND v.organization_id = f.organization_id
                                        AND v.user_id = f.user_id
                                     WHERE f.organization_id = $1::uuid AND f.user_id = $2::uuid
                                         AND f.deleted_at IS NULL
                                         AND f.current_revision > COALESCE(v.last_viewed_revision, 0)""",
                                org_id,
                                user_id,
                        )
            rows = await conn.fetch(
                                """SELECT f.*,
                                                    f.current_revision > COALESCE(v.last_viewed_revision, 0)
                                                            AS has_unseen_revision
                                     FROM user_files f
                                     LEFT JOIN user_file_views v
                                         ON v.file_id = f.id
                                        AND v.organization_id = f.organization_id
                                        AND v.user_id = f.user_id
                                     WHERE f.organization_id = $1::uuid AND f.user_id = $2::uuid
                                         AND f.deleted_at IS NULL
                                     ORDER BY has_unseen_revision DESC, f.updated_at DESC
                                     LIMIT $3 OFFSET $4""",
                org_id,
                user_id,
                limit,
                offset,
            )
        return {
            "items": [self._to_dict(row) for row in rows],
            "total_count": int(total or 0),
            "unseen_count": int(unseen_count or 0),
            "limit": limit,
            "offset": offset,
        }

    async def count_unseen_current_revisions(self, org_id: str, user_id: str) -> int:
        """Count owner-visible files whose latest revision has not been viewed."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """SELECT COUNT(*)
                   FROM user_files f
                   LEFT JOIN user_file_views v
                     ON v.file_id = f.id
                    AND v.organization_id = f.organization_id
                    AND v.user_id = f.user_id
                   WHERE f.organization_id = $1::uuid AND f.user_id = $2::uuid
                     AND f.deleted_at IS NULL
                     AND f.current_revision > COALESCE(v.last_viewed_revision, 0)""",
                org_id,
                user_id,
            )
        return int(count or 0)

    async def mark_current_revision_viewed_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> bool:
        """Record that the owner viewed the current revision, if the file is accessible."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO user_file_views (
                       file_id, organization_id, user_id, last_viewed_revision
                   )
                   SELECT id, organization_id, user_id, current_revision
                   FROM user_files
                   WHERE id = $1::uuid AND organization_id = $2::uuid
                     AND user_id = $3::uuid AND deleted_at IS NULL
                   ON CONFLICT (file_id, user_id) DO UPDATE
                   SET organization_id = EXCLUDED.organization_id,
                       last_viewed_revision = GREATEST(
                           user_file_views.last_viewed_revision,
                           EXCLUDED.last_viewed_revision
                       ),
                       last_viewed_at = NOW()
                   RETURNING file_id""",
                file_id,
                org_id,
                user_id,
            )
        return row is not None

    async def soft_delete(self, file_id: str, org_id: str, user_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE user_files SET deleted_at = NOW(), updated_at = NOW()
                   WHERE id = $1::uuid AND organization_id = $2::uuid
                     AND user_id = $3::uuid AND deleted_at IS NULL
                   RETURNING *""",
                file_id,
                org_id,
                user_id,
            )
        return self._to_dict(row) if row else None
