"""Persistence for user-owned file metadata."""

from __future__ import annotations

import json
from typing import Any

from asyncpg import Pool
from lucent.db.pool import scoped_acquire

# Advisory-lock namespace for file-storage quota accounting ("LFQT" — Lucent
# File QuoTA). Kept distinct from every other advisory-lock family in the repo
# so a file upload never blocks an unrelated workload.
FILE_QUOTA_LOCK_NAMESPACE = 0x4C465154

# An in-flight reservation older than this no longer counts. The window only
# has to cover one provider write, so it is generous relative to an upload and
# bounded so a crashed process cannot pin quota forever.
FILE_QUOTA_RESERVATION_TTL_SECONDS = 15 * 60

# Kept in step with the storage provider's soft-delete retention. The service
# passes the org-scoped ``files.soft_delete_retention_days`` setting; this is
# only the fallback for direct repository callers.
DEFAULT_SOFT_DELETE_RETENTION_DAYS = 7


class UserFileRepository:
    def __init__(self, pool: Pool):
        self.pool = pool

    @staticmethod
    def _to_dict(
        row: Any, *, include_storage_key: bool = False
    ) -> dict[str, Any]:
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
        # ``storage_key`` is a provider-internal address, never an authorization
        # boundary; keep it out of metadata responses. Internal read paths ask
        # for it explicitly.
        if not include_storage_key:
            item.pop("storage_key", None)
        return item

    @staticmethod
    async def _enforce_quota(
        conn: Any,
        *,
        org_id: str,
        user_id: str,
        new_bytes: int,
        max_bytes_per_user: int | None,
        max_bytes_per_org: int | None,
        soft_delete_retention_days: int = DEFAULT_SOFT_DELETE_RETENTION_DAYS,
        exclude_reservation_id: str | None = None,
    ) -> None:
        """Serialize, measure, and reject an over-quota write in one transaction.

        Must run inside the *same* transaction as the INSERT it guards (or the
        reservation INSERT). A plain SUM-before-insert is racy: two concurrent
        uploads both read the pre-insert total and both pass. The advisory lock
        (transaction-scoped, released on commit/rollback) makes the
        measure-then-insert pair atomic for one owner.

        The lock key is the **organization id alone**. Both caps are checked in
        this one measure-then-insert window, and the per-org cap is shared by
        every user in the org, so an ``"<org_id>:<user_id>"`` key was not
        enough: two different users took different keys, both read the
        pre-insert org total concurrently, and could jointly overshoot the org
        ceiling by the concurrent delta. Keying on the org makes the per-org
        cap hold. A second, user-granularity lock nested inside it would buy
        nothing — both are held to commit, so the org lock already excludes
        every same-org upload — and would only add deadlock surface.

        Contention: uploads in the *same* organization briefly serialize for
        the duration of the check-and-reserve (and the final measure-and-insert
        at finalisation). That is the right trade for an org-wide resource
        ceiling — both critical sections are short: one count query plus an
        INSERT, with no network or storage I/O under the lock.

        Counted bytes are:

        * the SUM over ``user_file_revisions`` of every file that is live, or
          was deleted inside the provider's soft-delete window
          (``soft_delete_retention_days``), because soft delete frees logical
          rows while the provider still stores and bills the blobs; plus
        * the SUM of unexpired in-flight reservations for this organization
          (``exclude_reservation_id`` omits the caller's own reservation when
          the write it was reserving for is being finalised).

        A file's true footprint is the sum of every retained revision, not
        ``user_files.size_bytes`` (which is only the current one). A deletion
        lowers quota only once the retention window has passed — the same
        moment the provider actually expires the bytes.
        """
        if max_bytes_per_user is None and max_bytes_per_org is None:
            return
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1, hashtext($2)::int)",
            FILE_QUOTA_LOCK_NAMESPACE,
            str(org_id),
        )
        row = await conn.fetchrow(
            """WITH retained AS (
                   SELECT f.user_id AS user_id, r.size_bytes AS bytes
                   FROM user_file_revisions r
                   JOIN user_files f ON f.id = r.file_id
                   WHERE f.organization_id = $1::uuid
                     AND (
                         f.deleted_at IS NULL
                         OR (
                             $3 > 0
                             AND f.deleted_at > NOW() - make_interval(days => $3)
                         )
                     )
               ),
               inflight AS (
                   SELECT res.user_id AS user_id, res.reserved_bytes AS bytes
                   FROM user_file_reservations res
                   WHERE res.organization_id = $1::uuid
                     AND res.expires_at > NOW()
                     AND ($4::uuid IS NULL OR res.id <> $4::uuid)
               )
               SELECT
                   COALESCE(SUM(bytes) FILTER (WHERE user_id = $2::uuid), 0)
                       AS user_bytes,
                   COALESCE(SUM(bytes), 0) AS org_bytes
               FROM (
                   SELECT user_id, bytes FROM retained
                   UNION ALL
                   SELECT user_id, bytes FROM inflight
               ) accounted""",
            org_id,
            user_id,
            int(soft_delete_retention_days),
            exclude_reservation_id,
        )
        used_user = int(row["user_bytes"] or 0)
        used_org = int(row["org_bytes"] or 0)
        if max_bytes_per_user is not None and used_user + new_bytes > max_bytes_per_user:
            raise ValueError(
                "Storage quota exceeded: this write needs "
                f"{used_user + new_bytes} bytes, which is "
                f"{used_user + new_bytes - max_bytes_per_user} bytes over the "
                f"per-user limit of {max_bytes_per_user} bytes."
            )
        if max_bytes_per_org is not None and used_org + new_bytes > max_bytes_per_org:
            raise ValueError(
                "Storage quota exceeded: this write needs "
                f"{used_org + new_bytes} bytes, which is "
                f"{used_org + new_bytes - max_bytes_per_org} bytes over the "
                f"organization limit of {max_bytes_per_org} bytes."
            )

    async def reserve(
        self,
        *,
        org_id: str,
        user_id: str,
        new_bytes: int,
        max_bytes_per_user: int | None,
        max_bytes_per_org: int | None,
        soft_delete_retention_days: int = DEFAULT_SOFT_DELETE_RETENTION_DAYS,
    ) -> str | None:
        """Reserve quota for a write, before any bytes reach the provider.

        The check-and-reserve pair runs under the same per-organization
        advisory lock as the final insert, so concurrent uploads cannot jointly
        reserve past either cap. A live reservation counts toward quota for
        ``FILE_QUOTA_RESERVATION_TTL_SECONDS``; the caller releases it on
        failure, and expiry covers a crashed process.

        Returns the reservation id, or ``None`` when neither cap is configured
        (nothing to reserve).
        """
        if max_bytes_per_user is None and max_bytes_per_org is None:
            return None
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            async with conn.transaction():
                await self._enforce_quota(
                    conn,
                    org_id=str(org_id),
                    user_id=str(user_id),
                    new_bytes=new_bytes,
                    max_bytes_per_user=max_bytes_per_user,
                    max_bytes_per_org=max_bytes_per_org,
                    soft_delete_retention_days=soft_delete_retention_days,
                )
                # Cheap housekeeping: rows past their window no longer count,
                # so remove them while the org lock is already held.
                await conn.execute(
                    """DELETE FROM user_file_reservations
                       WHERE organization_id = $1::uuid AND expires_at <= NOW()""",
                    org_id,
                )
                row = await conn.fetchrow(
                    """INSERT INTO user_file_reservations (
                           organization_id, user_id, reserved_bytes, expires_at
                       ) VALUES (
                           $1::uuid, $2::uuid, $3,
                           NOW() + make_interval(secs => $4)
                       ) RETURNING id""",
                    org_id,
                    user_id,
                    int(new_bytes),
                    FILE_QUOTA_RESERVATION_TTL_SECONDS,
                )
        return str(row["id"])

    async def release_reservation(
        self, reservation_id: str | None, *, org_id: str, user_id: str
    ) -> None:
        """Drop an in-flight reservation after a failed write (idempotent)."""
        if not reservation_id:
            return
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            await conn.execute(
                """DELETE FROM user_file_reservations
                   WHERE id = $1::uuid AND organization_id = $2::uuid
                     AND user_id = $3::uuid""",
                reservation_id,
                org_id,
                user_id,
            )

    @staticmethod
    def _retention_days(values: dict[str, Any]) -> int:
        retention = values.get("soft_delete_retention_days")
        return (
            DEFAULT_SOFT_DELETE_RETENTION_DAYS
            if retention is None
            else int(retention)
        )

    async def create(self, **values: Any) -> dict[str, Any]:
        # The row's own org+user stamp is available in values — bind it so the
        # INSERT is structurally scoped like every other file query.
        reservation_id = values.get("reservation_id")
        async with scoped_acquire(
            organization_id=values["org_id"], user_id=values["user_id"]
        ) as conn:
            async with conn.transaction():
                # Lock + measure + insert share this transaction, so a
                # concurrent upload cannot slip past the quota check. This
                # final measurement is authoritative; the caller's own
                # reservation is excluded because the actual bytes are counted
                # here instead.
                await self._enforce_quota(
                    conn,
                    org_id=str(values["org_id"]),
                    user_id=str(values["user_id"]),
                    new_bytes=int(values["size_bytes"]),
                    max_bytes_per_user=values.get("max_bytes_per_user"),
                    max_bytes_per_org=values.get("max_bytes_per_org"),
                    soft_delete_retention_days=self._retention_days(values),
                    exclude_reservation_id=reservation_id,
                )
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
                if reservation_id:
                    await conn.execute(
                        """DELETE FROM user_file_reservations
                           WHERE id = $1::uuid AND organization_id = $2::uuid
                             AND user_id = $3::uuid""",
                        reservation_id,
                        values["org_id"],
                        values["user_id"],
                    )
        return self._to_dict(row)

    async def get_owned(
        self,
        file_id: str,
        org_id: str,
        user_id: str,
        *,
        include_storage_key: bool = False,
    ) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            row = await conn.fetchrow(
                """SELECT * FROM user_files
                   WHERE id = $1::uuid AND organization_id = $2::uuid
                     AND user_id = $3::uuid AND deleted_at IS NULL""",
                file_id,
                org_id,
                user_id,
            )
        if not row:
            return None
        return self._to_dict(row, include_storage_key=include_storage_key)

    async def find_owned_by_filename(
        self,
        filename: str,
        org_id: str,
        user_id: str,
        *,
        include_storage_key: bool = False,
    ) -> list[dict[str, Any]]:
        """Find current files by an exact owner-scoped filename."""
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
            rows = await conn.fetch(
                """SELECT * FROM user_files
                   WHERE filename = $1 AND organization_id = $2::uuid
                     AND user_id = $3::uuid AND deleted_at IS NULL
                   ORDER BY updated_at DESC
                   LIMIT 2""",
                filename,
                org_id,
                user_id,
            )
        return [
            self._to_dict(row, include_storage_key=include_storage_key)
            for row in rows
        ]

    async def append_revision(self, **values: Any) -> dict[str, Any] | None:
        # Bind the file row's org+user — the ownership SELECT ... FOR UPDATE
        # and the revision INSERT both need it.
        reservation_id = values.get("reservation_id")
        async with scoped_acquire(
            organization_id=values["org_id"], user_id=values["user_id"]
        ) as conn:
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
                # A revision adds a full new copy of the bytes, so enforce the
                # same quota under the same org-scoped advisory lock before
                # inserting the revision row (see _enforce_quota). The actual
                # revision size is authoritative; the caller's reservation is
                # excluded so it is not counted twice.
                await self._enforce_quota(
                    conn,
                    org_id=str(values["org_id"]),
                    user_id=str(values["user_id"]),
                    new_bytes=int(values["size_bytes"]),
                    max_bytes_per_user=values.get("max_bytes_per_user"),
                    max_bytes_per_org=values.get("max_bytes_per_org"),
                    soft_delete_retention_days=self._retention_days(values),
                    exclude_reservation_id=reservation_id,
                )
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
                if reservation_id:
                    await conn.execute(
                        """DELETE FROM user_file_reservations
                           WHERE id = $1::uuid AND organization_id = $2::uuid
                             AND user_id = $3::uuid""",
                        reservation_id,
                        values["org_id"],
                        values["user_id"],
                    )
        return self._revision_to_dict(revision)

    @staticmethod
    def _revision_to_dict(
        row: Any, *, include_storage_key: bool = False
    ) -> dict[str, Any]:
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
        if not include_storage_key:
            item.pop("storage_key", None)
        return item

    async def list_revisions_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> list[dict[str, Any]]:
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
        *,
        include_storage_key: bool = False,
    ) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
        if not row:
            return None
        return self._revision_to_dict(row, include_storage_key=include_storage_key)

    async def list_storage_keys_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> list[tuple[str, str]]:
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
        async with scoped_acquire(organization_id=org_id, user_id=user_id) as conn:
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
