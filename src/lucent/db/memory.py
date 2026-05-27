"""Memory repository for Lucent.

Handles CRUD operations for memories including search functionality.
"""

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from time import monotonic
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool

from lucent.metrics import metrics
from lucent.models.repo_names import normalize_repository_full_name
from lucent.settings import (
    search_exclude_archived_enabled,
    search_vitality_boost_alpha,
    search_vitality_boost_enabled,
    shadow_forget_enabled,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class MemoryShadowScoreModel:
    """Typed row model for memory_shadow_scores sidecar entries."""

    memory_id: UUID
    strategy: str
    score: float | None
    shadow_action: str | None
    signals: dict[str, Any]
    computed_at: datetime
    divergence_tag: str | None


class VersionConflictError(Exception):
    """Raised when an optimistic locking version check fails."""

    def __init__(self, memory_id: UUID, expected_version: int, actual_version: int):
        self.memory_id = memory_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Version conflict for memory {memory_id}: "
            f"expected {expected_version}, actual {actual_version}"
        )


class DuplicateTechnicalMemoryError(Exception):
    """Raised when a file-scoped technical memory already exists."""

    def __init__(self, existing_memory: dict[str, Any], repo: str, filename: str):
        self.existing_memory = existing_memory
        self.repo = repo
        self.filename = filename
        self.memory_id = existing_memory["id"]
        self.can_update = existing_memory.get("user_id") is not None
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return (
            "A technical memory already exists for "
            f"{self.repo}:{self.filename} with ID {self.memory_id}. "
            "Update that memory instead of creating a duplicate. "
            "Use update_memory with that ID and intelligently combine the new information "
            "with the existing memory: preserve durable facts, merge non-overlapping details, "
            "reconcile conflicts explicitly, carry forward useful tags/references/metadata, "
            "and do not simply overwrite the existing content. If you do not have permission "
            "to update that shared memory, do not create a duplicate; ask the owner, an admin, "
            "or a daemon-managed workflow to merge the new information."
        )


class MemoryRepository:
    """Repository for memory CRUD operations."""

    TRUNCATE_LENGTH = 1000

    # Mapping from goal metadata.status → lifecycle_stage.
    # Only applied for goal-type memories.  Other types may have different
    # lifecycle semantics and are left untouched.
    _GOAL_STATUS_TO_LIFECYCLE: dict[str, str] = {
        "active": "active",
        "paused": "active",       # planner filters paused goals via metadata.status
        "completed": "archived",
        "done": "archived",
        "abandoned": "archived",
        "cancelled": "archived",
    }
    _ACTIVE_REQUEST_STATUSES: tuple[str, ...] = ("pending", "planned", "in_progress")

    # Shared column lists to avoid repetition across queries
    _FULL_COLUMNS = (
        "id, username, type, content, tags, importance, related_memory_ids, metadata, "
        "created_at, updated_at, deleted_at, user_id, "
        "organization_id, shared, last_accessed_at, version, "
        "lifecycle_stage, vitality_score, vitality_computed_at"
    )
    _SEARCH_COLUMNS = (
        "id, username, type, content, tags, importance, related_memory_ids, "
        "metadata, created_at, updated_at, user_id, organization_id, shared, last_accessed_at, "
        "lifecycle_stage, vitality_score"
    )
    _SHADOW_SCORE_COLUMNS = (
        "memory_id, strategy, score, shadow_action, signals, computed_at, divergence_tag"
    )

    def __init__(self, pool: Pool):
        self.pool = pool

    @staticmethod
    def _normalize_vitality_score(vitality_score: float | None) -> float:
        """Normalize vitality into a bounded [0.0, 1.0] interval."""
        if vitality_score is None:
            return 0.5
        return max(0.0, min(1.0, float(vitality_score)))

    @classmethod
    def _vitality_boosted_rank(
        cls,
        *,
        similarity_score: float,
        vitality_score: float | None,
        alpha: float,
    ) -> float:
        """Apply phase-2 vitality boost to a base similarity score."""
        centered_vitality = cls._normalize_vitality_score(vitality_score) - 0.5
        return similarity_score + (alpha * centered_vitality)

    @staticmethod
    def _row_field(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
        """Read a field from dict-like rows (dict/asyncpg.Record)."""
        if isinstance(row, Mapping):
            return row.get(key, default)
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
        return default

    @staticmethod
    def _canonical_created_at(created_at: Any) -> datetime:
        """Normalize created_at for deterministic canonical sorting."""
        if isinstance(created_at, datetime):
            dt = created_at
        elif isinstance(created_at, str):
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                return datetime.max.replace(tzinfo=UTC)
        else:
            return datetime.max.replace(tzinfo=UTC)

        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    @classmethod
    def select_canonical(
        cls, rows: list[Mapping[str, Any] | Any]
    ) -> Mapping[str, Any] | Any | None:
        """Pick canonical consolidation target by vitality, then completeness, then age.

        Ordering:
        1. Highest vitality_score first (NULL treated as 0.5)
        2. Highest importance first
        3. Oldest created_at first
        4. Lowest stringified id for deterministic final tie-break
        """
        if not rows:
            return None

        ranked = sorted(
            rows,
            key=lambda row: (
                -cls._normalize_vitality_score(cls._row_field(row, "vitality_score")),
                -int(cls._row_field(row, "importance", 0) or 0),
                cls._canonical_created_at(cls._row_field(row, "created_at")),
                str(cls._row_field(row, "id", "")),
            ),
        )
        return ranked[0]

    async def create(
        self,
        username: str,
        type: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 5,
        related_memory_ids: list[UUID] | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        shared: bool = False,
    ) -> dict[str, Any]:
        """Create a new memory.

        Args:
            username: The username of the user creating the memory.
            type: The type of memory.
            content: The main content of the memory.
            tags: Optional list of tags.
            importance: Importance rating (1-10).
            related_memory_ids: Optional list of related memory UUIDs.
            metadata: Optional type-specific metadata.
            user_id: Optional user ID (foreign key to users table).
            organization_id: Optional organization ID (for efficient org-scoped queries).
            shared: Whether the memory is visible to other org members.

        Returns:
            The created memory record.
        """
        metadata = self.normalize_metadata_for_storage(type, metadata)
        duplicate = await self.find_duplicate_technical_file_memory(
            metadata=metadata,
            requesting_user_id=user_id,
            requesting_org_id=organization_id,
        )
        if duplicate is not None:
            scope = self._technical_file_scope(metadata)
            if scope is not None:
                raise DuplicateTechnicalMemoryError(
                    existing_memory=duplicate,
                    repo=scope["repo"],
                    filename=scope["filename"],
                )

        # Auto-sync lifecycle_stage for goal memories created with an
        # initial metadata.status (e.g. importing a completed goal).
        initial_stage = self._resolve_goal_lifecycle_stage(type, metadata)
        if initial_stage and initial_stage != "active":
            query = f"""
                INSERT INTO memories (username, type, content, tags,
                    importance, related_memory_ids, metadata,
                    user_id, organization_id, shared, lifecycle_stage)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING {self._FULL_COLUMNS}
            """
            logger.info(
                "lifecycle auto-sync on create: goal memory with status=%s → lifecycle_stage=%s",
                metadata.get("status") if metadata else None,
                initial_stage,
            )
        else:
            query = f"""
                INSERT INTO memories (username, type, content, tags,
                    importance, related_memory_ids, metadata,
                    user_id, organization_id, shared)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING {self._FULL_COLUMNS}
            """

        # Sharing is now managed by the tool layer with type-based defaults.
        # Goals are no longer force-shared here — the daemon processes goals
        # per-user with scoped keys, so it doesn't need org-wide visibility.
        effective_shared = shared

        async with self.pool.acquire() as conn:
            # Validate related memory IDs exist and are not deleted
            if related_memory_ids:
                await self._validate_related_ids(related_memory_ids, conn=conn)

            create_params: list[Any] = [
                username,
                type,
                content,
                tags or [],
                importance,
                [str(uid) for uid in (related_memory_ids or [])],
                metadata or {},
                str(user_id) if user_id else None,
                str(organization_id) if organization_id else None,
                effective_shared,
            ]
            if initial_stage and initial_stage != "active":
                create_params.append(initial_stage)

            row = await conn.fetchrow(query, *create_params)

        return self._row_to_dict(row)

    @classmethod
    def normalize_metadata_for_storage(
        cls,
        memory_type: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Normalize metadata fields used for technical file dedupe.

        File-scoped technical memories use ``metadata.repo`` and
        ``metadata.filename`` as their duplicate key. Keep the stored metadata
        consistent enough that callers do not need a later consolidation pass
        just to derive ``metadata.directory`` from the filename.
        """
        normalized = dict(metadata or {})
        if memory_type != "technical":
            return normalized

        repo = cls._clean_repo(normalized.get("repo"), strict=True)
        filename = cls._clean_path(normalized.get("filename"))
        directory = cls._normalize_directory(normalized.get("directory"))

        if repo:
            normalized["repo"] = repo
        if filename:
            normalized["filename"] = filename
            normalized["directory"] = cls._parent_directory(filename)
        elif directory is not None:
            normalized["directory"] = directory

        return normalized

    async def find_duplicate_technical_file_memory(
        self,
        *,
        metadata: dict[str, Any] | None,
        requesting_user_id: UUID | None,
        requesting_org_id: UUID | None,
        memory_scope: str | None = None,
        exclude_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Find an accessible existing technical memory for the same file.

        The duplicate boundary intentionally mirrors ordinary memory access:
        the caller's own memories plus shared memories in the same org. Private
        memories owned by other users do not block creation because their
        existence is not visible to the caller.
        """
        scope = self._technical_file_scope(metadata)
        if scope is None or requesting_user_id is None or requesting_org_id is None:
            return None

        conditions = [
            "type = 'technical'",
            "deleted_at IS NULL",
            "COALESCE(lifecycle_stage, 'active') != 'forgotten'",
            "lower(metadata->>'repo') = $1",
            "lower(metadata->>'filename') = $2",
        ]
        params: list[Any] = [scope["repo"], scope["filename"]]
        param_idx = 3

        normalized_scope = memory_scope if memory_scope in {"user", "org_shared_only"} else None
        if normalized_scope == "user":
            conditions.append(f"user_id = ${param_idx}")
            params.append(str(requesting_user_id))
            param_idx += 1
        elif normalized_scope == "org_shared_only":
            conditions.append(
                f"(organization_id = ${param_idx}::uuid AND shared IS TRUE)"
            )
            params.append(str(requesting_org_id))
            param_idx += 1
        else:
            conditions.append(
                f"(user_id = ${param_idx}::uuid OR "
                f"(organization_id = ${param_idx + 1}::uuid AND shared IS TRUE))"
            )
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2

        if exclude_id is not None:
            conditions.append(f"id != ${param_idx}::uuid")
            params.append(str(exclude_id))
            param_idx += 1

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT {self._FULL_COLUMNS}
            FROM memories
            WHERE {where_clause}
            ORDER BY
                CASE WHEN user_id = ${param_idx}::uuid THEN 0 ELSE 1 END,
                shared DESC,
                updated_at DESC,
                created_at ASC
            LIMIT 1
        """
        params.append(str(requesting_user_id))

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)

        return self._row_to_dict(row) if row else None

    @classmethod
    def _technical_file_scope(cls, metadata: dict[str, Any] | None) -> dict[str, str] | None:
        if not isinstance(metadata, dict):
            return None
        repo = cls._clean_repo(metadata.get("repo"))
        filename = cls._clean_path(metadata.get("filename"))
        if not repo or not filename:
            return None
        return {"repo": repo.lower(), "filename": filename.lower()}

    @staticmethod
    def _clean_repo(value: Any, *, strict: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip().strip("`'\" ")
        if not cleaned:
            return None
        try:
            return normalize_repository_full_name(cleaned)
        except ValueError:
            if strict:
                raise
            return None

    @staticmethod
    def _clean_path(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip().strip("`'\" ").replace("\\", "/")
        cleaned = cleaned.removeprefix("./")
        return cleaned or None

    @classmethod
    def _normalize_directory(cls, value: Any) -> str | None:
        cleaned = cls._clean_path(value)
        if not cleaned:
            return None
        if cls._looks_like_file_path(cleaned):
            return cls._parent_directory(cleaned)
        return cleaned.rstrip("/") + "/"

    @staticmethod
    def _looks_like_file_path(path: str) -> bool:
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return "." in name and not path.endswith("/")

    @staticmethod
    def _parent_directory(filename: str) -> str | None:
        if "/" not in filename:
            return None
        return filename.rsplit("/", 1)[0] + "/"

    async def get(self, memory_id: UUID) -> dict[str, Any] | None:
        """Get a memory by ID (no access control).

        Args:
            memory_id: The UUID of the memory to retrieve.

        Returns:
            The memory record, or None if not found or deleted.
        """
        query = f"""
            SELECT {self._FULL_COLUMNS}
            FROM memories
            WHERE id = $1 AND deleted_at IS NULL
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(memory_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def get_accessible(
        self,
        memory_id: UUID,
        user_id: UUID,
        organization_id: UUID,
        memory_scope: str | None = None,
    ) -> dict[str, Any] | None:
        """Get a memory by ID with access control.

        Returns the memory only if:
        - The user owns the memory, OR
        - The memory is shared within the user's organization

        When memory_scope is 'user', only the user's own memories are returned.
        When memory_scope is 'org_shared_only', only shared org memories are returned.

        Args:
            memory_id: The UUID of the memory to retrieve.
            user_id: The ID of the requesting user.
            organization_id: The organization of the requesting user.
            memory_scope: Optional memory scope restriction ('user' or 'org_shared_only').

        Returns:
            The memory record, or None if not found, deleted, or not accessible.
        """
        normalized_scope = (
            memory_scope if memory_scope in {"user", "org_shared_only"} else None
        )

        # Keep placeholder numbering stable to avoid scope-dependent
        # bind count mismatches in prepared statement execution paths.
        query = f"""
            SELECT {self._FULL_COLUMNS}
            FROM memories
            WHERE id = $1
              AND deleted_at IS NULL
              AND (
                    ($4 = 'user' AND user_id = $2)
                 OR ($4 = 'org_shared_only' AND organization_id = $3 AND shared = true)
                 OR ($4 IS NULL AND (user_id = $2 OR (organization_id = $3 AND shared = true)))
              )
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                str(memory_id),
                str(user_id),
                str(organization_id),
                normalized_scope,
            )

        if row is None:
            return None

        return self._row_to_dict(row)

    async def get_individual_memory_for_user(self, user_id: UUID) -> dict[str, Any] | None:
        """Get the individual memory associated with a user.

        Args:
            user_id: The user's UUID.

        Returns:
            The memory record, or None if not found.
        """
        query = f"""
            SELECT {self._FULL_COLUMNS}
            FROM memories
            WHERE type = 'individual'
              AND deleted_at IS NULL
              AND user_id = $1
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(user_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def set_shared(
        self,
        memory_id: UUID,
        user_id: UUID,
        shared: bool,
    ) -> dict[str, Any] | None:
        """Set the shared status of a memory.

        Only the owner of the memory can change its shared status.

        Args:
            memory_id: The UUID of the memory to update.
            user_id: The ID of the requesting user (must be owner).
            shared: Whether to share (True) or unshare (False) the memory.

        Returns:
            The updated memory record, or None if not found or not owned by user.
        """
        query = f"""
            UPDATE memories
            SET shared = $1
            WHERE id = $2 AND user_id = $3 AND deleted_at IS NULL
            RETURNING {self._FULL_COLUMNS}
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, shared, str(memory_id), str(user_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def update(
        self,
        memory_id: UUID,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: int | None = None,
        related_memory_ids: list[UUID] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing memory.

        Args:
            memory_id: The UUID of the memory to update.
            content: Optional new content.
            tags: Optional new tags.
            importance: Optional new importance rating.
            related_memory_ids: Optional new related memory IDs.
            metadata: Optional new metadata.
            expected_version: If provided, the update only succeeds if the memory's
                current version matches. Raises VersionConflictError on mismatch.

        Returns:
            The updated memory record, or None if not found.

        Raises:
            VersionConflictError: If expected_version is provided and doesn't match.
        """
        # Build dynamic update query
        updates = []
        params = []
        param_idx = 1

        if content is not None:
            updates.append(f"content = ${param_idx}")
            params.append(content)
            param_idx += 1

        if tags is not None:
            updates.append(f"tags = ${param_idx}")
            params.append(tags)
            param_idx += 1

        if importance is not None:
            updates.append(f"importance = ${param_idx}")
            params.append(importance)
            param_idx += 1

        if related_memory_ids is not None:
            updates.append(f"related_memory_ids = ${param_idx}")
            params.append([str(uid) for uid in related_memory_ids])
            param_idx += 1

        if metadata is not None:
            updates.append(f"metadata = ${param_idx}")
            params.append(metadata)
            param_idx += 1

        # Auto-sync lifecycle_stage for goal-type memories when
        # metadata.status maps to a known lifecycle stage.  Uses a SQL
        # CASE so non-goal rows are never touched.
        #
        # Reconsolidation-on-update (M9 Phase 2, goal 82b41acd): any update
        # that touches a memory currently in 'consolidating' or 'archived'
        # promotes it back to 'active'. 'forgotten' rows are awaiting hard
        # delete and must NOT be reactivated. The goal-sync target wins when
        # both apply (CASE branches are evaluated top-to-bottom).
        # Only apply when the caller actually asked for a real field update —
        # a no-op call still short-circuits to a plain get().
        goal_target_stage: str | None = None
        if metadata is not None:
            goal_target_stage = self._resolve_goal_lifecycle_stage("goal", metadata)

        if updates and goal_target_stage is not None:
            updates.append(
                f"lifecycle_stage = CASE "
                f"WHEN type = 'goal' THEN ${param_idx} "
                f"WHEN lifecycle_stage IN ('consolidating', 'archived') THEN 'active' "
                f"ELSE lifecycle_stage END"
            )
            params.append(goal_target_stage)
            param_idx += 1
            logger.info(
                "lifecycle auto-sync: memory %s metadata.status=%s → lifecycle_stage=%s "
                "(applied only if type=goal)",
                memory_id,
                metadata.get("status") if metadata else None,
                goal_target_stage,
            )
        elif updates:
            updates.append(
                "lifecycle_stage = CASE "
                "WHEN lifecycle_stage IN ('consolidating', 'archived') THEN 'active' "
                "ELSE lifecycle_stage END"
            )

        if not updates:
            return await self.get(memory_id)

        # Always increment version on update
        updates.append("version = version + 1")

        # Build WHERE clause
        where_parts = [f"id = ${param_idx}"]
        params.append(str(memory_id))
        param_idx += 1

        where_parts.append("deleted_at IS NULL")

        if expected_version is not None:
            where_parts.append(f"version = ${param_idx}")
            params.append(expected_version)
            param_idx += 1

        query = f"""
            UPDATE memories
            SET {", ".join(updates)}
            WHERE {" AND ".join(where_parts)}
            RETURNING {self._FULL_COLUMNS}
        """

        # Use a single connection for validation and update
        async with self.pool.acquire() as conn:
            if related_memory_ids is not None:
                await self._validate_related_ids(
                    related_memory_ids, exclude_id=memory_id, conn=conn
                )

            row = await conn.fetchrow(query, *params)

        if row is None:
            # Distinguish between "not found" and "version mismatch"
            if expected_version is not None:
                existing = await self.get(memory_id)
                if existing is not None:
                    raise VersionConflictError(
                        memory_id=memory_id,
                        expected_version=expected_version,
                        actual_version=existing["version"],
                    )
            return None

        return self._row_to_dict(row)

    async def claim_task(
        self,
        memory_id: UUID,
        instance_id: str,
    ) -> dict[str, Any] | None:
        """Atomically claim a pending daemon task for a specific instance.

        Uses SELECT FOR UPDATE to prevent race conditions between instances.
        Only succeeds if the memory has a 'pending' tag and no existing claim.
        Replaces 'pending' with 'claimed-by-{instance_id}' in the tags array.

        Args:
            memory_id: The UUID of the task memory to claim.
            instance_id: The unique identifier of the claiming daemon instance.

        Returns:
            The updated memory record if claimed successfully, or None if the
            task was already claimed or is not in a pending state.
        """
        claim_tag = f"claimed-by-{instance_id}"

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Lock the row and verify it's still pending
                row = await conn.fetchrow(
                    f"""
                    SELECT {self._FULL_COLUMNS}
                    FROM memories
                    WHERE id = $1
                      AND deleted_at IS NULL
                      AND 'pending' = ANY(tags)
                      AND NOT EXISTS (
                          SELECT 1 FROM unnest(tags) t
                          WHERE t LIKE 'claimed-by-%'
                      )
                    FOR UPDATE SKIP LOCKED
                    """,
                    str(memory_id),
                )

                if row is None:
                    return None

                # Swap 'pending' → claim tag
                new_tags = [t for t in row["tags"] if t != "pending"]
                new_tags.append(claim_tag)

                updated = await conn.fetchrow(
                    f"""
                    UPDATE memories
                    SET tags = $1, version = version + 1
                    WHERE id = $2
                    RETURNING {self._FULL_COLUMNS}
                    """,
                    new_tags,
                    str(memory_id),
                )

                if updated is None:
                    return None

                return self._row_to_dict(updated)

    async def release_claim(
        self,
        memory_id: UUID,
        instance_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Release a claimed task back to pending state.

        If instance_id is provided, only releases if the task is claimed by that
        specific instance. If None, releases any claim.

        Args:
            memory_id: The UUID of the task memory to release.
            instance_id: Optional — only release if claimed by this instance.

        Returns:
            The updated memory record, or None if not found/not claimed.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if instance_id:
                    claim_tag = f"claimed-by-{instance_id}"
                    row = await conn.fetchrow(
                        f"""
                        SELECT {self._FULL_COLUMNS}
                        FROM memories
                        WHERE id = $1
                          AND deleted_at IS NULL
                          AND $2 = ANY(tags)
                        FOR UPDATE
                        """,
                        str(memory_id),
                        claim_tag,
                    )
                else:
                    row = await conn.fetchrow(
                        f"""
                        SELECT {self._FULL_COLUMNS}
                        FROM memories
                        WHERE id = $1
                          AND deleted_at IS NULL
                          AND EXISTS (
                              SELECT 1 FROM unnest(tags) t
                              WHERE t LIKE 'claimed-by-%%'
                          )
                        FOR UPDATE
                        """,
                        str(memory_id),
                    )

                if row is None:
                    return None

                # Remove claim tag, restore 'pending'
                new_tags = [t for t in row["tags"] if not t.startswith("claimed-by-")]
                new_tags.append("pending")

                updated = await conn.fetchrow(
                    f"""
                    UPDATE memories
                    SET tags = $1, version = version + 1
                    WHERE id = $2
                    RETURNING {self._FULL_COLUMNS}
                    """,
                    new_tags,
                    str(memory_id),
                )

                if updated is None:
                    return None

                return self._row_to_dict(updated)

    async def delete(
        self,
        memory_id: UUID,
        *,
        ldr_canonical_id: UUID | None = None,
        force_delete_compliance: bool = False,
    ) -> bool:
        """Soft delete a memory by setting deleted_at timestamp.

        Args:
            memory_id: The UUID of the memory to delete.
            ldr_canonical_id: Optional canonical replacement edge target for
                Candidate-C observation rows.
            force_delete_compliance: Whether delete is compliance-required and
                must remain hard-delete even with LDR.

        Returns:
            True if the memory was deleted, False if not found.
        """
        query = """
            UPDATE memories
            SET deleted_at = NOW()
            WHERE id = $1 AND deleted_at IS NULL
            RETURNING id
        """

        async with self.pool.acquire() as conn:
            await self._record_ldr_observation(
                source_id=memory_id,
                canonical_id=ldr_canonical_id,
                force_delete_compliance=force_delete_compliance,
                conn=conn,
            )
            result = await conn.fetchrow(query, str(memory_id))

        return result is not None

    async def _record_ldr_observation(
        self,
        *,
        source_id: UUID,
        canonical_id: UUID | None,
        force_delete_compliance: bool,
        conn: asyncpg.Connection,
    ) -> None:
        """Record a Candidate-C LDR observation row before a delete attempt.

        Strictly observation-only. Any error must not affect delete behavior.
        """
        if not shadow_forget_enabled():
            return

        try:
            exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM memories
                    WHERE id = $1
                      AND deleted_at IS NULL
                )
                """,
                str(source_id),
            )
            if not exists:
                return

            edges_at_risk = await conn.fetchval(
                """
                SELECT COUNT(*)::BIGINT
                FROM memories
                WHERE deleted_at IS NULL
                  AND id != $1
                  AND $1 = ANY(related_memory_ids)
                """,
                str(source_id),
            )
            signals = {
                "would_demote_source_id": str(source_id),
                "would_link_canonical_id": str(canonical_id) if canonical_id else None,
                "would_break_edges": int(edges_at_risk or 0),
                "force_delete_compliance": force_delete_compliance,
            }
            await conn.execute(
                """
                INSERT INTO memory_shadow_scores (
                    memory_id, strategy, score, shadow_action, signals, computed_at, divergence_tag
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                str(source_id),
                "ldr-obs-v1",
                None,
                "would_demote",
                signals,
                datetime.now(UTC),
                None,
            )
        except Exception:
            logger.warning(
                "LDR observation insert failed for memory delete: source_id=%s",
                source_id,
                exc_info=True,
            )

    async def search(
        self,
        query: str | None = None,
        username: str | None = None,
        type: str | None = None,
        tags: list[str] | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        memory_ids: list[UUID] | None = None,
        offset: int = 0,
        limit: int = 5,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
        memory_scope: str | None = None,
        accessible_repos: list[str] | None = None,
        vitality_boost: bool | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Search for memories with fuzzy matching and filters.

        If access control parameters are provided, only returns:
        - Memories owned by the requesting user, OR
        - Memories shared within the requesting user's organization

        Args:
            query: Optional fuzzy search query for content.
            username: Optional filter by username.
            type: Optional filter by memory type.
            tags: Optional filter by tags (all must match).
            importance_min: Optional minimum importance.
            importance_max: Optional maximum importance.
            created_after: Optional filter for memories created after this date.
            created_before: Optional filter for memories created before this date.
            memory_ids: Optional filter by specific memory IDs.
            offset: Pagination offset.
            limit: Maximum results to return.
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            include_archived: When False (default) and the
                ``LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED`` rollout flag is on,
                memories with ``lifecycle_stage`` in ``('archived', 'forgotten')``
                are excluded via a WHERE-clause addition. When True, archived
                and forgotten memories are returned alongside active ones —
                ``lifecycle_stage`` is already on each result so callers can
                surface the distinction. With the env flag off (default),
                this parameter is accepted but has no effect on the emitted
                SQL — preserving byte-identical baseline behavior.

        Returns:
            Search result with memories, total count, and pagination info.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1

        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            if memory_scope == "user":
                conditions.append(f"user_id = ${param_idx}")
                params.append(str(requesting_user_id))
                param_idx += 1
            elif memory_scope == "org_shared_only":
                conditions.append(
                    f"(organization_id = ${param_idx} AND shared = true)"
                )
                params.append(str(requesting_org_id))
                param_idx += 1
            else:
                conditions.append(
                    f"(user_id = ${param_idx} OR "
                    f"(organization_id = ${param_idx + 1} AND shared = true))"
                )
                params.append(str(requesting_user_id))
                params.append(str(requesting_org_id))
                param_idx += 2

        # GitHub repo ACL filter (None means caller is admin/owner — no filter).
        if accessible_repos is not None:
            conditions.append(
                f"(metadata IS NULL "
                f"OR NOT (metadata ? 'repo') "
                f"OR LOWER(metadata->>'repo') = ANY(${param_idx}::text[]))"
            )
            params.append([r.lower() for r in accessible_repos])
            param_idx += 1

        # Build WHERE conditions
        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1

        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1

        if tags:
            conditions.append(f"tags @> ${param_idx}")
            params.append(tags)
            param_idx += 1

        if importance_min is not None:
            conditions.append(f"importance >= ${param_idx}")
            params.append(importance_min)
            param_idx += 1

        if importance_max is not None:
            conditions.append(f"importance <= ${param_idx}")
            params.append(importance_max)
            param_idx += 1

        if created_after is not None:
            conditions.append(f"created_at >= ${param_idx}")
            params.append(created_after)
            param_idx += 1

        if created_before is not None:
            conditions.append(f"created_at <= ${param_idx}")
            params.append(created_before)
            param_idx += 1

        if memory_ids:
            placeholders = ", ".join(f"${i}" for i in range(param_idx, param_idx + len(memory_ids)))
            conditions.append(f"id IN ({placeholders})")
            params.extend(str(uid) for uid in memory_ids)
            param_idx += len(memory_ids)

        # M9 Phase-2: lifecycle exclusion is gated behind a rollout flag so
        # the default emitted SQL stays byte-identical to the pre-M9 baseline
        # until an operator opts in. Only when the flag is ON and the caller
        # did NOT request archived rows do we add the WHERE-clause addition.
        if search_exclude_archived_enabled() and not include_archived:
            conditions.append(
                "(lifecycle_stage IS NULL "
                "OR lifecycle_stage NOT IN ('archived', 'forgotten'))"
            )

        where_clause = " AND ".join(conditions)

        boost_enabled = (
            search_vitality_boost_enabled() if vitality_boost is None else vitality_boost
        )
        boost_alpha = search_vitality_boost_alpha()

        # Build the query with optional fuzzy matching
        if query:
            # Use pg_trgm similarity for fuzzy search
            similarity_param = param_idx
            params.append(query)
            param_idx += 1

            if boost_enabled:
                boost_alpha_param = param_idx
                params.append(boost_alpha)
                param_idx += 1
                search_query = f"""
                    SELECT {self._SEARCH_COLUMNS},
                           similarity(content, ${similarity_param}) as sim_score,
                           similarity(content, ${similarity_param})
                             + (${boost_alpha_param}
                                * (COALESCE(vitality_score, 0.5) - 0.5)) AS final_rank
                    FROM memories
                    WHERE {where_clause}
                      AND (content % ${similarity_param}
                           OR content ILIKE '%' || ${similarity_param} || '%')
                    ORDER BY final_rank DESC, importance DESC, created_at DESC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}
                """
            else:
                search_query = f"""
                    SELECT {self._SEARCH_COLUMNS},
                           similarity(content, ${similarity_param}) as sim_score
                    FROM memories
                    WHERE {where_clause}
                      AND (content % ${similarity_param}
                           OR content ILIKE '%' || ${similarity_param} || '%')
                    ORDER BY sim_score DESC, importance DESC, created_at DESC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}
                """

            count_query = f"""
                SELECT COUNT(*) as total
                FROM memories
                WHERE {where_clause}
                  AND (content % ${similarity_param}
                       OR content ILIKE '%' || ${similarity_param} || '%')
            """
        else:
            search_query = f"""
                SELECT {self._SEARCH_COLUMNS},
                       NULL::float as sim_score
                FROM memories
                WHERE {where_clause}
                ORDER BY importance DESC, created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """

            count_query = f"""
                SELECT COUNT(*) as total
                FROM memories
                WHERE {where_clause}
            """

        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            # Get total count
            count_params = params[:-2]  # Exclude limit and offset
            count_row = await conn.fetchrow(count_query, *count_params)
            total_count = count_row["total"] if count_row else 0

            # Get results
            rows = await conn.fetch(search_query, *params)

        memories = [self._row_to_search_dict(row) for row in rows]

        return {
            "memories": memories,
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(memories) < total_count,
        }

    async def search_full(
        self,
        query: str,
        username: str | None = None,
        type: str | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        offset: int = 0,
        limit: int = 5,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
        memory_scope: str | None = None,
        accessible_repos: list[str] | None = None,
        vitality_boost: bool | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Search across all text fields: content, tags, and metadata.

        This is a broader search that looks at all text in a memory,
        useful when you're not sure which field contains the information.

        If access control parameters are provided, only returns:
        - Memories owned by the requesting user, OR
        - Memories shared within the requesting user's organization

        Args:
            query: Search query to match against content, tags, and metadata.
            username: Optional filter by username.
            type: Optional filter by memory type.
            importance_min: Optional minimum importance.
            importance_max: Optional maximum importance.
            offset: Pagination offset.
            limit: Maximum results to return.
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            include_archived: See ``search``. Default-False with the rollout
                flag off is a no-op (preserves baseline SQL); flag-on excludes
                ``archived``/``forgotten`` rows unless explicitly included.

        Returns:
            Search result with memories, total count, and pagination info.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1

        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            if memory_scope == "user":
                conditions.append(f"user_id = ${param_idx}")
                params.append(str(requesting_user_id))
                param_idx += 1
            elif memory_scope == "org_shared_only":
                conditions.append(
                    f"(organization_id = ${param_idx} AND shared = true)"
                )
                params.append(str(requesting_org_id))
                param_idx += 1
            else:
                conditions.append(
                    f"(user_id = ${param_idx} OR "
                    f"(organization_id = ${param_idx + 1} AND shared = true))"
                )
                params.append(str(requesting_user_id))
                params.append(str(requesting_org_id))
                param_idx += 2

        # GitHub repo ACL filter (None means caller is admin/owner — no filter).
        if accessible_repos is not None:
            conditions.append(
                f"(metadata IS NULL "
                f"OR NOT (metadata ? 'repo') "
                f"OR LOWER(metadata->>'repo') = ANY(${param_idx}::text[]))"
            )
            params.append([r.lower() for r in accessible_repos])
            param_idx += 1

        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1

        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1

        if importance_min is not None:
            conditions.append(f"importance >= ${param_idx}")
            params.append(importance_min)
            param_idx += 1

        if importance_max is not None:
            conditions.append(f"importance <= ${param_idx}")
            params.append(importance_max)
            param_idx += 1

        # M9 Phase-2: see ``search`` — gated lifecycle exclusion preserves the
        # pre-M9 SQL baseline until ``LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED``
        # is enabled.
        if search_exclude_archived_enabled() and not include_archived:
            conditions.append(
                "(lifecycle_stage IS NULL "
                "OR lifecycle_stage NOT IN ('archived', 'forgotten'))"
            )

        where_clause = " AND ".join(conditions)

        boost_enabled = (
            search_vitality_boost_enabled() if vitality_boost is None else vitality_boost
        )
        boost_alpha = search_vitality_boost_alpha()

        # Build a combined text field for searching: content + tags + metadata
        query_param = param_idx
        params.append(query)
        param_idx += 1

        # Search across content, array_to_string(tags), and metadata::text
        if boost_enabled:
            boost_alpha_param = param_idx
            params.append(boost_alpha)
            param_idx += 1
            search_query = f"""
                SELECT {self._SEARCH_COLUMNS},
                       GREATEST(
                           similarity(content, ${query_param}),
                           similarity(array_to_string(tags, ' '), ${query_param}),
                           similarity(metadata::text, ${query_param})
                       ) as sim_score,
                       GREATEST(
                           similarity(content, ${query_param}),
                           similarity(array_to_string(tags, ' '), ${query_param}),
                           similarity(metadata::text, ${query_param})
                       ) + (${boost_alpha_param}
                            * (COALESCE(vitality_score, 0.5) - 0.5)) AS final_rank
                FROM memories
                WHERE {where_clause}
                  AND (
                      content % ${query_param} OR content ILIKE '%' || ${query_param} || '%'
                      OR array_to_string(tags, ' ') % ${query_param}
                      OR array_to_string(tags, ' ') ILIKE '%' || ${query_param} || '%'
                      OR metadata::text % ${query_param}
                      OR metadata::text ILIKE '%' || ${query_param} || '%'
                  )
                ORDER BY final_rank DESC, importance DESC, created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """
        else:
            search_query = f"""
                SELECT {self._SEARCH_COLUMNS},
                       GREATEST(
                           similarity(content, ${query_param}),
                           similarity(array_to_string(tags, ' '), ${query_param}),
                           similarity(metadata::text, ${query_param})
                       ) as sim_score
                FROM memories
                WHERE {where_clause}
                  AND (
                      content % ${query_param} OR content ILIKE '%' || ${query_param} || '%'
                      OR array_to_string(tags, ' ') % ${query_param}
                      OR array_to_string(tags, ' ') ILIKE '%' || ${query_param} || '%'
                      OR metadata::text % ${query_param}
                      OR metadata::text ILIKE '%' || ${query_param} || '%'
                  )
                ORDER BY sim_score DESC, importance DESC, created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """

        count_query = f"""
            SELECT COUNT(*) as total
            FROM memories
            WHERE {where_clause}
              AND (
                  content % ${query_param} OR content ILIKE '%' || ${query_param} || '%'
                  OR array_to_string(tags, ' ') % ${query_param}
                  OR array_to_string(tags, ' ') ILIKE '%' || ${query_param} || '%'
                  OR metadata::text % ${query_param}
                  OR metadata::text ILIKE '%' || ${query_param} || '%'
              )
        """

        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            count_params = params[:-2]
            count_row = await conn.fetchrow(count_query, *count_params)
            total_count = count_row["total"] if count_row else 0

            rows = await conn.fetch(search_query, *params)

        memories = [self._row_to_search_dict(row) for row in rows]

        return {
            "memories": memories,
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(memories) < total_count,
        }

    async def get_existing_tags(
        self,
        username: str | None = None,
        type: str | None = None,
        limit: int = 50,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
        accessible_repos: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing tags with usage counts.

        Args:
            username: Optional filter by username.
            type: Optional filter by memory type.
            limit: Maximum number of tags to return (default 50).
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            accessible_repos: Optional GitHub repo allowlist (lowercased). When
                provided, tag counts only include memories whose
                ``metadata.repo`` is null/missing or appears in this list.
                Pass ``None`` to skip the filter (admin/owner).

        Returns:
            List of {tag, count} sorted by count descending.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1

        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            conditions.append(
                f"(user_id = ${param_idx} OR "
                f"(organization_id = ${param_idx + 1} AND shared = true))"
            )
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2

        if accessible_repos is not None:
            conditions.append(
                f"(metadata IS NULL "
                f"OR NOT (metadata ? 'repo') "
                f"OR LOWER(metadata->>'repo') = ANY(${param_idx}::text[]))"
            )
            params.append([r.lower() for r in accessible_repos])
            param_idx += 1

        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1

        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT tag, COUNT(*) as count
            FROM memories, UNNEST(tags) as tag
            WHERE {where_clause}
            GROUP BY tag
            ORDER BY count DESC, tag ASC
            LIMIT ${param_idx}
        """
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [{"tag": row["tag"], "count": row["count"]} for row in rows]

    async def get_tag_suggestions(
        self,
        query: str,
        username: str | None = None,
        limit: int = 10,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
        accessible_repos: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get tag suggestions based on fuzzy matching.

        Args:
            query: The partial tag to search for.
            username: Optional filter by username.
            limit: Maximum number of suggestions (default 10).
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            accessible_repos: Optional GitHub repo allowlist; see ``get_existing_tags``.

        Returns:
            List of {tag, count, similarity} sorted by similarity descending.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1

        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            conditions.append(
                f"(user_id = ${param_idx} OR "
                f"(organization_id = ${param_idx + 1} AND shared = true))"
            )
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2

        if accessible_repos is not None:
            conditions.append(
                f"(metadata IS NULL "
                f"OR NOT (metadata ? 'repo') "
                f"OR LOWER(metadata->>'repo') = ANY(${param_idx}::text[]))"
            )
            params.append([r.lower() for r in accessible_repos])
            param_idx += 1

        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        query_param = param_idx
        params.append(query.lower())
        param_idx += 1

        # Use trigram similarity for fuzzy matching on tags
        sql = f"""
            SELECT tag, COUNT(*) as count, similarity(tag, ${query_param}) as sim
            FROM memories, UNNEST(tags) as tag
            WHERE {where_clause}
              AND (tag % ${query_param} OR tag ILIKE '%' || ${query_param} || '%')
            GROUP BY tag
            ORDER BY sim DESC, count DESC, tag ASC
            LIMIT ${param_idx}
        """
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            {"tag": row["tag"], "count": row["count"], "similarity": row["sim"]} for row in rows
        ]

    async def export(
        self,
        type: str | None = None,
        tags: list[str] | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
        memory_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Export memories with full content and metadata.

        Returns all matching memories without truncation or pagination limits.
        Access-controlled: only returns memories the user owns or that are
        shared within their organization.

        Args:
            type: Optional filter by memory type.
            tags: Optional filter by tags (all must match).
            importance_min: Optional minimum importance.
            importance_max: Optional maximum importance.
            created_after: Filter memories created after this date.
            created_before: Filter memories created before this date.
            requesting_user_id: User ID for access control.
            requesting_org_id: Organization ID for access control.

        Returns:
            List of full memory records.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1

        if requesting_user_id is not None and requesting_org_id is not None:
            if memory_scope == "user":
                conditions.append(f"user_id = ${param_idx}")
                params.append(str(requesting_user_id))
                param_idx += 1
            elif memory_scope == "org_shared_only":
                conditions.append(
                    f"(organization_id = ${param_idx} AND shared = true)"
                )
                params.append(str(requesting_org_id))
                param_idx += 1
            else:
                conditions.append(
                    f"(user_id = ${param_idx} OR "
                    f"(organization_id = ${param_idx + 1} AND shared = true))"
                )
                params.append(str(requesting_user_id))
                params.append(str(requesting_org_id))
                param_idx += 2

        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1

        if tags:
            conditions.append(f"tags @> ${param_idx}")
            params.append(tags)
            param_idx += 1

        if importance_min is not None:
            conditions.append(f"importance >= ${param_idx}")
            params.append(importance_min)
            param_idx += 1

        if importance_max is not None:
            conditions.append(f"importance <= ${param_idx}")
            params.append(importance_max)
            param_idx += 1

        if created_after is not None:
            conditions.append(f"created_at >= ${param_idx}")
            params.append(created_after)
            param_idx += 1

        if created_before is not None:
            conditions.append(f"created_at <= ${param_idx}")
            params.append(created_before)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT {self._FULL_COLUMNS}
            FROM memories
            WHERE {where_clause}
            ORDER BY created_at ASC
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return [self._row_to_dict(row) for row in rows]

    async def import_memories(
        self,
        memories: list[dict[str, Any]],
        requesting_user_id: UUID,
        requesting_org_id: UUID | None = None,
        requesting_username: str | None = None,
    ) -> dict[str, Any]:
        """Import memories from an export payload.

        Deduplicates by content hash (content + type + username). Skips
        memories whose content already exists for this user. Preserves
        original timestamps when provided.

        Args:
            memories: List of memory dicts (matching export MemoryResponse format).
            requesting_user_id: The authenticated user's ID — all imports are owned by this user.
            requesting_org_id: The authenticated user's organization ID.
            requesting_username: Fallback username if memory dict lacks one.

        Returns:
            Dict with imported, skipped, and errors counts plus error details.
        """
        import hashlib

        valid_types = {"experience", "technical", "procedural", "goal", "individual"}
        imported = 0
        skipped = 0
        errors: list[dict[str, str]] = []

        async with self.pool.acquire() as conn:
            # Build set of existing content hashes for this user
            existing_rows = await conn.fetch(
                "SELECT md5(content || type || username) AS hash FROM memories "
                "WHERE user_id = $1 AND deleted_at IS NULL",
                str(requesting_user_id),
            )
            existing_hashes: set[str] = {r["hash"] for r in existing_rows}

            for idx, mem in enumerate(memories):
                try:
                    # --- Validate required fields ---
                    content = mem.get("content")
                    mem_type = mem.get("type")
                    if not content or not isinstance(content, str) or not content.strip():
                        errors.append({"index": str(idx), "error": "Missing or empty content"})
                        continue
                    if len(content) > 100_000:
                        errors.append(
                            {"index": str(idx), "error": "Content exceeds 100,000 character limit"}
                        )
                        continue
                    if mem_type not in valid_types:
                        errors.append({"index": str(idx), "error": f"Invalid type: {mem_type}"})
                        continue

                    username = mem.get("username") or requesting_username or "imported"
                    importance = mem.get("importance", 5)
                    if not isinstance(importance, int) or importance < 1 or importance > 10:
                        importance = 5
                    tags = mem.get("tags") or []
                    if not isinstance(tags, list):
                        tags = []
                    tags = [str(t).lower().strip() for t in tags if t]
                    metadata = mem.get("metadata") or {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    related_ids = [str(uid) for uid in (mem.get("related_memory_ids") or [])]

                    # --- Dedup check ---
                    content_hash = hashlib.md5((content + mem_type + username).encode()).hexdigest()
                    if content_hash in existing_hashes:
                        skipped += 1
                        continue

                    # --- Preserve timestamps if provided ---
                    created_at = None
                    updated_at = None
                    if mem.get("created_at"):
                        try:
                            created_at = (
                                mem["created_at"]
                                if isinstance(mem["created_at"], datetime)
                                else datetime.fromisoformat(str(mem["created_at"]))
                            )
                        except (ValueError, TypeError):
                            created_at = None
                    if mem.get("updated_at"):
                        try:
                            updated_at = (
                                mem["updated_at"]
                                if isinstance(mem["updated_at"], datetime)
                                else datetime.fromisoformat(str(mem["updated_at"]))
                            )
                        except (ValueError, TypeError):
                            updated_at = None

                    if created_at and updated_at:
                        query = f"""
                            INSERT INTO memories
                                (username, type, content, tags, importance,
                                 related_memory_ids, metadata, user_id, organization_id,
                                 shared, created_at, updated_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,false,$10,$11)
                            RETURNING {self._FULL_COLUMNS}
                        """
                        await conn.fetchrow(
                            query,
                            username,
                            mem_type,
                            content,
                            tags,
                            importance,
                            related_ids,
                            metadata,
                            str(requesting_user_id),
                            str(requesting_org_id) if requesting_org_id else None,
                            created_at,
                            updated_at,
                        )
                    else:
                        query = f"""
                            INSERT INTO memories
                                (username, type, content, tags, importance,
                                 related_memory_ids, metadata, user_id, organization_id, shared)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,false)
                            RETURNING {self._FULL_COLUMNS}
                        """
                        await conn.fetchrow(
                            query,
                            username,
                            mem_type,
                            content,
                            tags,
                            importance,
                            related_ids,
                            metadata,
                            str(requesting_user_id),
                            str(requesting_org_id) if requesting_org_id else None,
                        )

                    existing_hashes.add(content_hash)
                    imported += 1

                except Exception as e:
                    logger.error("Failed to import memory at index %d", idx, exc_info=e)
                    errors.append({"index": str(idx), "error": str(e)})

        return {
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "total": len(memories),
        }

    async def compute_vitality_scores(self, batch_size: int = 500) -> dict[str, Any]:
        """Compute and persist vitality scores for all non-deleted, non-forgotten memories."""
        from lucent.memory.decay import DecayConfig, MemoryDecayInput, compute_memory_vitality

        cfg = DecayConfig.from_env()
        now = datetime.now(UTC)
        processed = 0
        updated = 0
        stage_transitions = 0
        offset = 0

        while True:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT {self._FULL_COLUMNS}
                    FROM memories
                    WHERE deleted_at IS NULL
                      AND lifecycle_stage != 'forgotten'
                    ORDER BY created_at ASC
                    LIMIT $1 OFFSET $2
                    """,
                    batch_size,
                    offset,
                )

            if not rows:
                break

            memory_dicts = [self._row_to_dict(row) for row in rows]
            memory_ids = [item["id"] for item in memory_dicts]
            access_counts = await self._get_access_counts_last_n_days(memory_ids, days=90, now=now)
            frequency_baseline = self._compute_p75_baseline(
                counts=[access_counts.get(memory_id, 0) for memory_id in memory_ids],
                default=cfg.frequency_baseline,
            )

            for mem in memory_dicts:
                profile = MemoryDecayInput(
                    memory_id=mem["id"],
                    memory_type=mem["type"],
                    importance=mem["importance"],
                    created_at=self._utc(mem["created_at"]),
                    updated_at=self._utc(mem["updated_at"]),
                    last_accessed_at=self._utc(mem["last_accessed_at"])
                    if mem.get("last_accessed_at")
                    else None,
                    access_count=access_counts.get(mem["id"], 0),
                    tags=mem.get("tags") or [],
                    metadata=mem.get("metadata") or {},
                )
                score_result = compute_memory_vitality(
                    profile,
                    config=cfg,
                    now=now,
                    frequency_baseline=frequency_baseline,
                )
                computed_stage = self._map_action_to_lifecycle_stage(score_result.action)

                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE memories
                        SET vitality_score = $2,
                            vitality_computed_at = $3,
                            lifecycle_stage = $4
                        WHERE id = $1
                          AND deleted_at IS NULL
                        """,
                        str(mem["id"]),
                        score_result.score,
                        now,
                        computed_stage,
                    )

                processed += 1
                updated += 1
                if (mem.get("lifecycle_stage") or "active") != computed_stage:
                    stage_transitions += 1

            offset += batch_size

        return {
            "processed": processed,
            "updated": updated,
            "stage_transitions": stage_transitions,
            "computed_at": now,
        }

    async def compute_shadow_forget_scores(
        self,
        *,
        strategy: str = "gcp-v1",
        batch_size: int = 500,
    ) -> dict[str, Any]:
        """Compute Candidate-A shadow scores and write sidecar rows only."""
        from lucent.memory.decay import (
            GcpConfig,
            GraphConnectednessInput,
            compute_graph_connectedness,
        )

        if not shadow_forget_enabled():
            return {
                "enabled": False,
                "strategy": strategy,
                "processed": 0,
                "inserted": 0,
                "computed_at": datetime.now(UTC),
                "comparison_metrics": {},
                "duration_seconds": 0.0,
            }
        if strategy != "gcp-v1":
            raise ValueError("Unsupported shadow strategy. Supported: gcp-v1")

        started = monotonic()
        now = datetime.now(UTC)
        processed = 0
        inserted = 0
        offset = 0
        cfg = GcpConfig()

        while True:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT {self._FULL_COLUMNS}
                    FROM memories
                    WHERE deleted_at IS NULL
                      AND lifecycle_stage != 'forgotten'
                    ORDER BY created_at ASC
                    LIMIT $1 OFFSET $2
                    """,
                    batch_size,
                    offset,
                )
            if not rows:
                break

            memory_dicts = [self._row_to_dict(row) for row in rows]
            memory_ids = [item["id"] for item in memory_dicts]
            graph_signals = await self._get_graph_connectedness_signals(
                memory_ids=memory_ids,
                now=now,
            )

            async with self.pool.acquire() as conn:
                for mem in memory_dicts:
                    signals = graph_signals.get(mem["id"], {})
                    signals["out_degree"] = len(mem.get("related_memory_ids") or [])
                    created_at = self._utc(mem["created_at"])
                    age_days = max(0, (now - created_at).days)
                    profile = GraphConnectednessInput(
                        memory_id=mem["id"],
                        importance=int(mem["importance"]),
                        age_days=age_days,
                        in_degree=int(signals.get("in_degree", 0)),
                        out_degree=int(signals.get("out_degree", 0)),
                        active_request_links=int(signals.get("active_request_links", 0)),
                        version_depth=max(1, int(mem.get("version", 1))),
                        distinct_readers_90d=int(signals.get("distinct_readers_90d", 0)),
                        tags=mem.get("tags") or [],
                        metadata=mem.get("metadata") or {},
                    )
                    gcp_result = compute_graph_connectedness(profile, config=cfg)
                    divergence = self._classify_shadow_divergence(
                        shadow_action=gcp_result.action,
                        lifecycle_stage=mem.get("lifecycle_stage"),
                    )

                    await conn.execute(
                        """
                        INSERT INTO memory_shadow_scores (
                            memory_id, strategy, score, shadow_action,
                            signals, computed_at, divergence_tag
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        str(mem["id"]),
                        strategy,
                        gcp_result.score,
                        gcp_result.action,
                        gcp_result.signals,
                        now,
                        divergence,
                    )
                    processed += 1
                    inserted += 1

            offset += batch_size

        comparison = await self.get_shadow_forget_comparison(
            strategy=strategy,
            window_hours=168,
            limit=100,
        )
        duration_seconds = monotonic() - started
        self._emit_shadow_forget_metrics(
            strategy=strategy,
            comparison=comparison,
            duration_seconds=duration_seconds,
        )

        return {
            "enabled": True,
            "strategy": strategy,
            "processed": processed,
            "inserted": inserted,
            "computed_at": now,
            "comparison_metrics": comparison.get("metrics", {}),
            "duration_seconds": duration_seconds,
        }

    async def get_shadow_forget_comparison(
        self,
        *,
        strategy: str = "gcp-v1",
        window_hours: int = 168,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return aggregate shadow-vs-vitality comparison statistics."""
        if strategy != "gcp-v1":
            raise ValueError("Unsupported shadow strategy. Supported: gcp-v1")

        window_hours = max(1, min(window_hours, 24 * 30))
        limit = max(1, min(limit, 500))
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)

        async with self.pool.acquire() as conn:
            latest_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (ms.memory_id)
                       ms.memory_id,
                       ms.score,
                       ms.shadow_action,
                       ms.divergence_tag,
                       ms.signals,
                       ms.computed_at,
                       m.lifecycle_stage,
                       m.vitality_score
                FROM memory_shadow_scores ms
                JOIN memories m ON m.id = ms.memory_id
                WHERE ms.strategy = $1
                  AND ms.computed_at >= $2
                  AND m.deleted_at IS NULL
                ORDER BY ms.memory_id, ms.computed_at DESC
                """,
                strategy,
                cutoff,
            )
            ldr_rows = await conn.fetch(
                """
                SELECT signals
                FROM memory_shadow_scores
                WHERE strategy = 'ldr-obs-v1'
                  AND computed_at >= $1
                """,
                cutoff,
            )

        latest = [dict(row) for row in latest_rows]
        total = len(latest)
        k = max(1, ceil(total * 0.01)) if total else 0

        vitality_top = sorted(
            [row for row in latest if row.get("vitality_score") is not None],
            key=lambda row: (float(row["vitality_score"]), str(row["memory_id"])),
        )[:k]
        gcp_top = sorted(
            [row for row in latest if row.get("shadow_action") == "forgetting_candidate"],
            key=lambda row: (float(row.get("score") or 0.0), str(row["memory_id"])),
        )[:k]

        vitality_ids = {str(row["memory_id"]) for row in vitality_top}
        gcp_ids = {str(row["memory_id"]) for row in gcp_top}
        agreement_n = len(vitality_ids & gcp_ids)
        top_k_agreement = (agreement_n / k) if k else 1.0

        orphan_reclaims = [
            row
            for row in latest
            if row.get("shadow_action") == "forgetting_candidate"
            and str(row.get("lifecycle_stage") or "active") == "active"
        ]
        orphan_n = len(orphan_reclaims)
        orphan_reclaim_rate = (
            orphan_n
            / max(
                1,
                sum(
                    1
                    for row in latest
                    if row.get("shadow_action") == "forgetting_candidate"
                ),
            )
        )
        orphan_avg_in_degree = (
            sum(int((row.get("signals") or {}).get("in_degree", 0)) for row in orphan_reclaims)
            / orphan_n
            if orphan_n
            else 0.0
        )

        load_bearing = [
            row
            for row in latest
            if int((row.get("signals") or {}).get("in_degree", 0)) >= 5
            or int((row.get("signals") or {}).get("active_request_links", 0)) >= 1
        ]
        load_bearing_archived = [
            row
            for row in load_bearing
            if str(row.get("lifecycle_stage") or "") in {"consolidating", "archived"}
        ]
        load_bearing_protection_rate = (
            len(load_bearing_archived) / len(load_bearing) if load_bearing else 0.0
        )

        ldr_edges_values = [
            int(((row["signals"] or {}).get("would_break_edges", 0)))
            for row in ldr_rows
        ]
        ldr_edges_sum = int(sum(ldr_edges_values))
        ldr_edges_mean = (ldr_edges_sum / len(ldr_edges_values)) if ldr_edges_values else 0.0
        ldr_edges_max = max(ldr_edges_values) if ldr_edges_values else 0

        disagreements = [
            row
            for row in latest
            if row.get("divergence_tag")
            in {"gcp-protects-vitality-archives", "gcp-forgets-vitality-keeps"}
        ]
        top_disagreements = sorted(
            disagreements,
            key=lambda row: abs(float(row.get("score") or 0.0)),
            reverse=True,
        )[:limit]

        return {
            "strategy": strategy,
            "window_hours": window_hours,
            "sample_size": total,
            "k": k,
            "metrics": {
                "top_k_agreement": top_k_agreement,
                "orphan_reclaim_rate": orphan_reclaim_rate,
                "orphan_reclaim_count": orphan_n,
                "orphan_reclaim_avg_in_degree": orphan_avg_in_degree,
                "load_bearing_protection_rate": load_bearing_protection_rate,
                "load_bearing_total": len(load_bearing),
                "ldr_edges_at_risk_sum": ldr_edges_sum,
                "ldr_edges_at_risk_mean": ldr_edges_mean,
                "ldr_edges_at_risk_max": ldr_edges_max,
            },
            "divergence_counts": {
                "agree": sum(1 for row in latest if row.get("divergence_tag") == "agree"),
                "gcp-protects-vitality-archives": sum(
                    1
                    for row in latest
                    if row.get("divergence_tag") == "gcp-protects-vitality-archives"
                ),
                "gcp-forgets-vitality-keeps": sum(
                    1
                    for row in latest
                    if row.get("divergence_tag") == "gcp-forgets-vitality-keeps"
                ),
            },
            "top_disagreements": [
                {
                    "memory_id": str(row["memory_id"]),
                    "score": float(row.get("score") or 0.0),
                    "shadow_action": row.get("shadow_action"),
                    "lifecycle_stage": row.get("lifecycle_stage"),
                    "divergence_tag": row.get("divergence_tag"),
                    "signals": row.get("signals") or {},
                    "computed_at": row["computed_at"],
                }
                for row in top_disagreements
            ],
        }

    async def _get_graph_connectedness_signals(
        self,
        *,
        memory_ids: list[UUID],
        now: datetime,
    ) -> dict[UUID, dict[str, int]]:
        """Collect graph and usage signals for Candidate-A scoring."""
        if not memory_ids:
            return {}

        cutoff = now - timedelta(days=90)

        async with self.pool.acquire() as conn:
            in_degree_rows = await conn.fetch(
                """
                SELECT rel_id::uuid AS memory_id, COUNT(*)::BIGINT AS in_degree
                FROM memories m,
                     unnest(m.related_memory_ids) AS rel_id
                WHERE m.deleted_at IS NULL
                  AND rel_id = ANY($1::uuid[])
                GROUP BY rel_id
                """,
                memory_ids,
            )
            request_rows = await conn.fetch(
                """
                SELECT rm.memory_id, COUNT(*)::BIGINT AS active_request_links
                FROM request_memories rm
                JOIN requests r ON r.id = rm.request_id
                WHERE rm.memory_id = ANY($1::uuid[])
                  AND r.status = ANY($2::text[])
                GROUP BY rm.memory_id
                """,
                memory_ids,
                list(self._ACTIVE_REQUEST_STATUSES),
            )
            reader_rows = await conn.fetch(
                """
                SELECT memory_id, COUNT(DISTINCT user_id)::BIGINT AS distinct_readers_90d
                FROM memory_access_log
                WHERE memory_id = ANY($1::uuid[])
                  AND user_id IS NOT NULL
                  AND accessed_at >= $2
                GROUP BY memory_id
                """,
                memory_ids,
                cutoff,
            )

        in_degree = {UUID(str(row["memory_id"])): int(row["in_degree"]) for row in in_degree_rows}
        active_links = {
            UUID(str(row["memory_id"])): int(row["active_request_links"]) for row in request_rows
        }
        readers = {
            UUID(str(row["memory_id"])): int(row["distinct_readers_90d"]) for row in reader_rows
        }

        return {
            memory_id: {
                "in_degree": in_degree.get(memory_id, 0),
                "out_degree": 0,
                "active_request_links": active_links.get(memory_id, 0),
                "distinct_readers_90d": readers.get(memory_id, 0),
            }
            for memory_id in memory_ids
        }

    def _classify_shadow_divergence(
        self,
        *,
        shadow_action: str,
        lifecycle_stage: str | None,
    ) -> str:
        """Classify agreement/divergence tag versus current vitality lifecycle stage."""
        stage = str(lifecycle_stage or "active")
        vitality_archives = stage in {"consolidating", "archived"}
        shadow_forgets = shadow_action == "forgetting_candidate"

        if not vitality_archives and shadow_forgets:
            return "gcp-forgets-vitality-keeps"
        if vitality_archives and shadow_action in {"keep", "protected_hub"}:
            return "gcp-protects-vitality-archives"
        return "agree"

    def _emit_shadow_forget_metrics(
        self,
        *,
        strategy: str,
        comparison: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        """Emit shadow comparison metrics through OTEL."""
        metric_values = comparison.get("metrics", {})
        attrs = {"strategy": strategy, "window_hours": str(comparison.get("window_hours", 168))}
        metrics.shadow_forget_top_k_agreement.record(
            float(metric_values.get("top_k_agreement", 0.0)),
            attrs,
        )
        metrics.shadow_forget_orphan_reclaim.record(
            float(metric_values.get("orphan_reclaim_rate", 0.0)),
            attrs,
        )
        metrics.shadow_forget_load_bearing_protection.record(
            float(metric_values.get("load_bearing_protection_rate", 0.0)),
            attrs,
        )
        metrics.shadow_forget_ldr_edges_at_risk.record(
            float(metric_values.get("ldr_edges_at_risk_sum", 0.0)),
            attrs,
        )
        metrics.shadow_forget_compute_overhead.record(max(0.0, duration_seconds), attrs)

    async def get_lifecycle_stats(
        self,
        organization_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Return lifecycle distribution and vitality histogram for observability."""
        params: list[Any] = []
        org_filter = ""
        if organization_id:
            org_filter = "AND organization_id = $1"
            params.append(str(organization_id))

        async with self.pool.acquire() as conn:
            stage_rows = await conn.fetch(
                f"""
                SELECT lifecycle_stage, COUNT(*) AS count
                FROM memories
                WHERE deleted_at IS NULL {org_filter}
                GROUP BY lifecycle_stage
                """,
                *params,
            )
            histogram_rows = await conn.fetch(
                f"""
                SELECT
                    CASE
                        WHEN vitality_score IS NULL THEN 'unscored'
                        WHEN vitality_score < 0.1 THEN '0.0-0.1'
                        WHEN vitality_score < 0.2 THEN '0.1-0.2'
                        WHEN vitality_score < 0.3 THEN '0.2-0.3'
                        WHEN vitality_score < 0.4 THEN '0.3-0.4'
                        WHEN vitality_score < 0.5 THEN '0.4-0.5'
                        WHEN vitality_score < 0.6 THEN '0.5-0.6'
                        WHEN vitality_score < 0.7 THEN '0.6-0.7'
                        WHEN vitality_score < 0.8 THEN '0.7-0.8'
                        WHEN vitality_score < 0.9 THEN '0.8-0.9'
                        ELSE '0.9-1.0'
                    END AS bucket,
                    COUNT(*) AS count
                FROM memories
                WHERE deleted_at IS NULL {org_filter}
                GROUP BY bucket
                """,
                *params,
            )
            total_count_row = await conn.fetchrow(
                f"""
                SELECT COUNT(*) AS total
                FROM memories
                WHERE deleted_at IS NULL {org_filter}
                """,
                *params,
            )

        default_stages = {
            "active": 0,
            "consolidating": 0,
            "archived": 0,
            "forgotten": 0,
        }
        stage_distribution = {
            row["lifecycle_stage"] or "active": row["count"] for row in stage_rows
        }
        stage_distribution = {**default_stages, **stage_distribution}

        default_histogram = {
            "unscored": 0,
            "0.0-0.1": 0,
            "0.1-0.2": 0,
            "0.2-0.3": 0,
            "0.3-0.4": 0,
            "0.4-0.5": 0,
            "0.5-0.6": 0,
            "0.6-0.7": 0,
            "0.7-0.8": 0,
            "0.8-0.9": 0,
            "0.9-1.0": 0,
        }
        vitality_histogram = {row["bucket"]: row["count"] for row in histogram_rows}
        vitality_histogram = {**default_histogram, **vitality_histogram}

        return {
            "stage_distribution": stage_distribution,
            "vitality_histogram": vitality_histogram,
            "total_memories": total_count_row["total"] if total_count_row else 0,
        }

    async def insert_shadow_score(
        self,
        *,
        memory_id: UUID,
        strategy: str,
        signals: dict[str, Any],
        score: float | None = None,
        shadow_action: str | None = None,
        computed_at: datetime | None = None,
        divergence_tag: str | None = None,
    ) -> dict[str, Any] | None:
        """Insert a single shadow score sidecar row (feature-flag gated)."""
        if not shadow_forget_enabled():
            return None

        effective_computed_at = self._utc(computed_at) if computed_at else datetime.now(UTC)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO memory_shadow_scores (
                    memory_id, strategy, score, shadow_action, signals, computed_at, divergence_tag
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING {self._SHADOW_SCORE_COLUMNS}
                """,
                str(memory_id),
                strategy,
                score,
                shadow_action,
                signals,
                effective_computed_at,
                divergence_tag,
            )

        if row is None:
            return None
        return self._shadow_row_to_dict(row)

    async def upsert_shadow_score(
        self,
        *,
        memory_id: UUID,
        strategy: str,
        signals: dict[str, Any],
        score: float | None = None,
        shadow_action: str | None = None,
        computed_at: datetime | None = None,
        divergence_tag: str | None = None,
    ) -> dict[str, Any] | None:
        """Upsert a shadow score sidecar row keyed by (memory_id, strategy, computed_at)."""
        if not shadow_forget_enabled():
            return None

        effective_computed_at = self._utc(computed_at) if computed_at else datetime.now(UTC)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO memory_shadow_scores (
                    memory_id, strategy, score, shadow_action, signals, computed_at, divergence_tag
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (memory_id, strategy, computed_at)
                DO UPDATE SET
                    score = EXCLUDED.score,
                    shadow_action = EXCLUDED.shadow_action,
                    signals = EXCLUDED.signals,
                    divergence_tag = EXCLUDED.divergence_tag
                RETURNING {self._SHADOW_SCORE_COLUMNS}
                """,
                str(memory_id),
                strategy,
                score,
                shadow_action,
                signals,
                effective_computed_at,
                divergence_tag,
            )

        if row is None:
            return None
        return self._shadow_row_to_dict(row)

    async def get_latest_shadow_score(
        self,
        *,
        memory_id: UUID,
        strategy: str,
    ) -> dict[str, Any] | None:
        """Fetch latest sidecar row for a memory+strategy (feature-flag gated)."""
        if not shadow_forget_enabled():
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {self._SHADOW_SCORE_COLUMNS}
                FROM memory_shadow_scores
                WHERE memory_id = $1
                  AND strategy = $2
                ORDER BY computed_at DESC
                LIMIT 1
                """,
                str(memory_id),
                strategy,
            )
        if row is None:
            return None
        return self._shadow_row_to_dict(row)

    async def _validate_related_ids(
        self,
        related_ids: list[UUID],
        exclude_id: UUID | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        """Validate that related memory IDs exist and are not deleted.

        Args:
            related_ids: List of UUIDs to validate.
            exclude_id: Optional ID to exclude from check (for self-reference prevention).
            conn: Optional existing connection to reuse (avoids extra pool acquisition).

        Raises:
            ValueError: If any IDs are invalid, deleted, or self-referencing.
        """
        if not related_ids:
            return

        # Check for self-reference
        if exclude_id and exclude_id in related_ids:
            raise ValueError("A memory cannot reference itself")

        placeholders = ", ".join(f"${i + 1}" for i in range(len(related_ids)))
        query = f"""
            SELECT id FROM memories
            WHERE id IN ({placeholders}) AND deleted_at IS NULL
        """

        str_ids = [str(uid) for uid in related_ids]

        if conn is not None:
            rows = await conn.fetch(query, *str_ids)
        else:
            async with self.pool.acquire() as pool_conn:
                rows = await pool_conn.fetch(query, *str_ids)

        # Convert found IDs to strings for comparison
        found_ids = {str(row["id"]) for row in rows}
        requested_ids = {str(uid) for uid in related_ids}
        missing_ids = requested_ids - found_ids

        if missing_ids:
            raise ValueError(f"Related memory IDs not found or deleted: {missing_ids}")

    def _row_to_search_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a search result row to a dictionary with truncation."""
        content = row["content"]
        truncated = len(content) > self.TRUNCATE_LENGTH
        if truncated:
            content = content[: self.TRUNCATE_LENGTH] + "..."

        related_ids = []
        if row["related_memory_ids"]:
            for uid in row["related_memory_ids"]:
                related_ids.append(uid if isinstance(uid, UUID) else UUID(uid))

        user_id = None
        if row["user_id"]:
            user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])

        org_id = None
        if row["organization_id"]:
            org_id = (
                row["organization_id"]
                if isinstance(row["organization_id"], UUID)
                else UUID(row["organization_id"])
            )

        return {
            "id": row["id"],
            "username": row["username"],
            "type": row["type"],
            "content": content,
            "content_truncated": truncated,
            "tags": row["tags"],
            "importance": row["importance"],
            "related_memory_ids": related_ids,
            "metadata": row["metadata"] if "metadata" in row.keys() else {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "similarity_score": row["sim_score"],
            "user_id": user_id,
            "organization_id": org_id,
            "shared": row["shared"],
            "last_accessed_at": row["last_accessed_at"],
            "lifecycle_stage": (
                row["lifecycle_stage"] if "lifecycle_stage" in row.keys() else "active"
            ),
            "vitality_score": row["vitality_score"] if "vitality_score" in row.keys() else None,
        }

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        # Handle related_memory_ids which may be strings or UUIDs
        related_ids = []
        if row["related_memory_ids"]:
            for uid in row["related_memory_ids"]:
                if isinstance(uid, UUID):
                    related_ids.append(uid)
                else:
                    related_ids.append(UUID(uid))

        # Handle user_id which may not be present in all queries
        user_id = None
        if "user_id" in row.keys() and row["user_id"]:
            user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])

        # Handle organization_id which may not be present in all queries
        org_id = None
        if "organization_id" in row.keys() and row["organization_id"]:
            org_id = (
                row["organization_id"]
                if isinstance(row["organization_id"], UUID)
                else UUID(row["organization_id"])
            )

        # Handle shared flag
        shared = row["shared"] if "shared" in row.keys() else False

        # Handle last_accessed_at
        last_accessed_at = row["last_accessed_at"] if "last_accessed_at" in row.keys() else None

        return {
            "id": row["id"],
            "username": row["username"],
            "type": row["type"],
            "content": row["content"],
            "tags": row["tags"],
            "importance": row["importance"],
            "related_memory_ids": related_ids,
            "metadata": row["metadata"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
            "user_id": user_id,
            "organization_id": org_id,
            "shared": shared,
            "last_accessed_at": last_accessed_at,
            "version": row["version"] if "version" in row.keys() else 1,
            "lifecycle_stage": (
                row["lifecycle_stage"] if "lifecycle_stage" in row.keys() else "active"
            ),
            "vitality_score": row["vitality_score"] if "vitality_score" in row.keys() else None,
            "vitality_computed_at": (
                row["vitality_computed_at"] if "vitality_computed_at" in row.keys() else None
            ),
        }

    def _shadow_row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        memory_id = (
            row["memory_id"]
            if isinstance(row["memory_id"], UUID)
            else UUID(row["memory_id"])
        )
        model = MemoryShadowScoreModel(
            memory_id=memory_id,
            strategy=row["strategy"],
            score=row["score"],
            shadow_action=row["shadow_action"],
            signals=row["signals"] or {},
            computed_at=row["computed_at"],
            divergence_tag=row["divergence_tag"],
        )
        return asdict(model)

    async def _get_access_counts_last_n_days(
        self,
        memory_ids: list[UUID],
        *,
        days: int,
        now: datetime,
    ) -> dict[UUID, int]:
        if not memory_ids:
            return {}

        cutoff = now - timedelta(days=days)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(memory_ids)))
        cutoff_param = len(memory_ids) + 1
        query = f"""
            SELECT memory_id, COUNT(*) AS access_count
            FROM memory_access_log
            WHERE memory_id IN ({placeholders})
              AND accessed_at >= ${cutoff_param}
            GROUP BY memory_id
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *[str(mid) for mid in memory_ids], cutoff)

        counts: dict[UUID, int] = {memory_id: 0 for memory_id in memory_ids}
        for row in rows:
            memory_id = (
                row["memory_id"] if isinstance(row["memory_id"], UUID) else UUID(row["memory_id"])
            )
            counts[memory_id] = row["access_count"]
        return counts

    @staticmethod
    def _compute_p75_baseline(*, counts: list[int], default: int) -> int:
        non_negative = sorted(max(0, c) for c in counts)
        if not non_negative:
            return max(1, default)

        rank = max(1, ceil(0.75 * len(non_negative)))
        p75 = non_negative[rank - 1]
        return max(1, p75 if p75 > 0 else default)

    @staticmethod
    def _map_action_to_lifecycle_stage(action: str) -> str:
        if action == "archive-candidate":
            return "archived"
        if action == "suggest-cleanup":
            return "consolidating"
        return "active"

    @classmethod
    def _resolve_goal_lifecycle_stage(
        cls,
        memory_type: str,
        metadata: dict[str, Any] | None,
    ) -> str | None:
        """Return the lifecycle_stage implied by a goal's metadata.status.

        Returns None when no sync is needed (non-goal type, no metadata,
        no status key, or status not in the mapping).
        """
        if memory_type != "goal" or not metadata:
            return None
        status = metadata.get("status")
        if not isinstance(status, str):
            return None
        return cls._GOAL_STATUS_TO_LIFECYCLE.get(status.lower().strip())

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
