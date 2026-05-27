"""Repository for review CRUD operations.

Reviews are first-class objects representing approval/rejection decisions
on requests and tasks. All queries enforce organization_id scoping for
multi-tenant isolation.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from asyncpg import Connection
from asyncpg import Pool


class ReviewRepository:
    """Manages review records for requests and tasks."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def create_review(
        self,
        request_id: str,
        organization_id: str,
        status: str,
        *,
        task_id: str | None = None,
        reviewer_user_id: str | None = None,
        reviewer_display_name: str | None = None,
        comments: str | None = None,
        source: str = "human",
        conn: Connection | None = None,
    ) -> dict:
        """Create a new review record.

        Args:
            request_id: The request being reviewed.
            organization_id: Organization scope (required for multi-tenant safety).
            status: 'approved' or 'rejected'.
            task_id: Optional specific task being reviewed.
            reviewer_user_id: FK to users table for the reviewer.
            reviewer_display_name: Display name snapshot for the reviewer.
            comments: Review comments/feedback.
            source: Origin of the review — 'human', 'daemon', or 'agent'.

        Returns:
            The created review record as a dict.
        """
        if status not in ("approved", "rejected"):
            raise ValueError(f"Invalid review status '{status}'. Must be 'approved' or 'rejected'.")
        if source not in ("human", "daemon", "agent"):
            raise ValueError(f"Invalid review source '{source}'. Must be 'human', 'daemon', or 'agent'.")

        if conn is None:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(
                    """INSERT INTO reviews
                       (request_id, task_id, organization_id, reviewer_user_id,
                        reviewer_display_name, status, comments, source)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                       RETURNING *""",
                    UUID(request_id),
                    UUID(task_id) if task_id else None,
                    UUID(organization_id),
                    UUID(reviewer_user_id) if reviewer_user_id else None,
                    reviewer_display_name,
                    status,
                    comments,
                    source,
                )
        else:
            row = await conn.fetchrow(
                """INSERT INTO reviews
                   (request_id, task_id, organization_id, reviewer_user_id,
                    reviewer_display_name, status, comments, source)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   RETURNING *""",
                UUID(request_id),
                UUID(task_id) if task_id else None,
                UUID(organization_id),
                UUID(reviewer_user_id) if reviewer_user_id else None,
                reviewer_display_name,
                status,
                comments,
                source,
            )
        return dict(row)

    async def mark_request_needs_rework(
        self,
        request_id: str,
        organization_id: str,
        feedback: str | None,
        *,
        conn: Connection | None = None,
    ) -> dict | None:
        """Transition a request to needs_rework and persist review feedback."""
        params = (
            UUID(request_id),
            feedback,
            UUID(organization_id),
            datetime.now(timezone.utc),
        )
        query = """UPDATE requests
                   SET status = 'needs_rework',
                       review_feedback = $2,
                       review_count = review_count + 1,
                       reviewed_at = $4,
                       updated_at = $4
                   WHERE id = $1 AND organization_id = $3 AND status = 'review'
                   RETURNING *"""
        if conn is None:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(query, *params)
        else:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def mark_request_completed(
        self,
        request_id: str,
        organization_id: str,
        *,
        conn: Connection | None = None,
    ) -> dict | None:
        """Transition a request from review to completed."""
        now = datetime.now(timezone.utc)
        params = (
            UUID(request_id),
            UUID(organization_id),
            now,
        )
        query = """UPDATE requests
                   SET status = 'completed',
                       completed_at = COALESCE(completed_at, $3),
                       updated_at = $3
                   WHERE id = $1 AND organization_id = $2 AND status = 'review'
                   RETURNING *"""
        if conn is None:
            async with self.pool.acquire() as acquired:
                row = await acquired.fetchrow(query, *params)
        else:
            row = await conn.fetchrow(query, *params)
        return dict(row) if row else None

    async def get_review(
        self, review_id: str, organization_id: str
    ) -> dict | None:
        """Get a single review by ID, scoped to organization."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT r.*, req.title as request_title
                   FROM reviews r
                   JOIN requests req ON r.request_id = req.id
                   WHERE r.id = $1 AND r.organization_id = $2""",
                UUID(review_id),
                UUID(organization_id),
            )
        return dict(row) if row else None

    async def list_reviews(
        self,
        organization_id: str,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """List reviews with optional filters, scoped to organization.

        Returns paginated results matching the standard format:
        {items, total_count, offset, limit, has_more}
        """
        base = """FROM reviews r
                   JOIN requests req ON r.request_id = req.id
                   WHERE r.organization_id = $1"""
        params: list[Any] = [UUID(organization_id)]

        if request_id:
            params.append(UUID(request_id))
            base += f" AND r.request_id = ${len(params)}"
        if task_id:
            params.append(UUID(task_id))
            base += f" AND r.task_id = ${len(params)}"
        if status:
            params.append(status)
            base += f" AND r.status = ${len(params)}"
        if source:
            params.append(source)
            base += f" AND r.source = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = (
            f"SELECT r.*, req.title as request_title {base} "
            f"ORDER BY r.created_at DESC "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        params_with_page = [*params, limit, offset]

        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)

        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_reviews_for_request(
        self, request_id: str, organization_id: str
    ) -> list[dict]:
        """Get all reviews for a request, ordered by creation time."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM reviews
                   WHERE request_id = $1 AND organization_id = $2
                   ORDER BY created_at DESC""",
                UUID(request_id),
                UUID(organization_id),
            )
        return [dict(r) for r in rows]

    async def get_reviews_for_task(
        self, task_id: str, organization_id: str
    ) -> list[dict]:
        """Get all reviews for a specific task."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM reviews
                   WHERE task_id = $1 AND organization_id = $2
                   ORDER BY created_at DESC""",
                UUID(task_id),
                UUID(organization_id),
            )
        return [dict(r) for r in rows]

    async def get_review_summary(
        self, organization_id: str
    ) -> dict:
        """Get aggregate review statistics for the organization."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                     COUNT(*) AS total,
                     COUNT(*) FILTER (WHERE status = 'approved') AS approved,
                     COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                     COUNT(*) FILTER (WHERE source = 'human') AS human_reviews,
                     COUNT(*) FILTER (WHERE source = 'daemon') AS daemon_reviews,
                     COUNT(*) FILTER (WHERE source = 'agent') AS agent_reviews
                   FROM reviews
                   WHERE organization_id = $1""",
                UUID(organization_id),
            )
        return dict(row) if row else {
            "total": 0, "approved": 0, "rejected": 0,
            "human_reviews": 0, "daemon_reviews": 0, "agent_reviews": 0,
        }
