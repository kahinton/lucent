"""API router for review management.

Provides REST endpoints for creating, listing, and querying reviews.
Reviews are first-class objects representing approval/rejection decisions
on requests and tasks.

Side effects on review creation:
- APPROVAL: Transitions request to 'completed' and creates a tracked request
  from the approved content so it enters the processing pipeline.
  Deduplicated by source_review_id to prevent duplicate requests on retry.
- REJECTION: Transitions request to 'needs_rework' and creates a memory
  record tagged with 'rejection-lesson' and 'learning-extraction' for the
  learning extraction system.
"""

import hashlib
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from lucent.api.deps import AdminUser, AuthenticatedUser, get_pool

logger = logging.getLogger("lucent.api.reviews")

router = APIRouter(prefix="/reviews", tags=["reviews"])


# ── Models ────────────────────────────────────────────────────────────────


class ReviewCreate(BaseModel):
    """Create a new review."""

    request_id: str = Field(..., description="UUID of the request being reviewed")
    task_id: str | None = Field(
        default=None, description="Optional UUID of the specific task being reviewed"
    )
    status: str = Field(
        ..., pattern=r"^(approved|rejected)$", description="Review decision"
    )
    comments: str | None = Field(
        default=None, description="Review comments/feedback"
    )
    source: str = Field(
        default="human",
        pattern=r"^(human|daemon|agent)$",
        description="Origin of the review",
    )


class ReviewResponse(BaseModel):
    """Review response model."""

    id: str
    request_id: str
    task_id: str | None = None
    organization_id: str
    reviewer_user_id: str | None = None
    reviewer_display_name: str | None = None
    status: str
    comments: str | None = None
    source: str
    created_at: str
    request_title: str | None = None


def _review_fingerprint(review_id: str) -> str:
    """Compute a deduplication fingerprint from a review ID.

    Used to prevent duplicate tracked requests when the same approval
    is retried (idempotency).
    """
    return hashlib.md5(f"review-approved-{review_id}".encode()).hexdigest()


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_review(
    body: ReviewCreate,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Create a new review for a request or task.

    Validates that the request exists and belongs to the user's organization.
    On rejection, comments are required.

    Side effects:
    - Approval on a request in 'review' status:
        1. Transitions request to 'completed'.
        2. Creates a tracked request from the approved content so it enters
           the processing pipeline. Deduplicated by review ID fingerprint.
    - Rejection on a request in 'review' status:
        1. Transitions request to 'needs_rework', increments review_count.
        2. Creates a memory tagged 'rejection-lesson' + 'learning-extraction'
           + 'daemon' so the learning extraction system can learn from it.
    """
    from lucent.db.requests import RequestRepository
    from lucent.db.reviews import ReviewRepository

    org_id = str(user.organization_id)

    # Validate request exists in this org
    req_repo = RequestRepository(pool)
    request = await req_repo.get_request(body.request_id, org_id)
    if not request:
        raise HTTPException(404, "Request not found")

    # Validate task exists if specified
    if body.task_id:
        task = await req_repo.get_task(body.task_id, org_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if str(task["request_id"]) != body.request_id:
            raise HTTPException(
                422, "Task does not belong to the specified request"
            )

    # Require comments on rejection
    if body.status == "rejected" and not body.comments:
        raise HTTPException(422, "Comments are required when rejecting")

    repo = ReviewRepository(pool)
    review = await repo.create_review(
        request_id=body.request_id,
        organization_id=org_id,
        status=body.status,
        task_id=body.task_id,
        reviewer_user_id=str(user.id),
        reviewer_display_name=user.display_name or user.email,
        comments=body.comments,
        source=body.source,
    )

    review_id = str(review["id"])

    # Side effects based on review status
    if body.status == "approved" and request["status"] == "review":
        # 1. Auto-transition request to completed on approval
        await req_repo.update_request_status(
            body.request_id, "completed", org_id=org_id
        )

        # 2. Create a tracked request from the approved content so it enters
        #    the processing pipeline. Uses a fingerprint derived from the
        #    review ID so retrying the same approval is idempotent.
        await _create_approved_request(
            req_repo, request, review_id, org_id, user
        )

    elif body.status == "rejected" and request["status"] == "review":
        # 1. Transition request to needs_rework on rejection
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE requests
                   SET status = 'needs_rework',
                       review_feedback = $2,
                       review_count = review_count + 1,
                       reviewed_at = NOW(),
                       updated_at = NOW()
                   WHERE id = $1 AND organization_id = $3""",
                UUID(body.request_id),
                body.comments,
                user.organization_id,
            )

        # 2. Create a learning memory from the rejection so the learning
        #    extraction system can find and process it.
        await _create_rejection_memory(
            pool, request, body.comments, org_id, user
        )

    return _serialize_review(review)


async def _create_approved_request(
    req_repo, request: dict, review_id: str, org_id: str, user
) -> dict | None:
    """Create a tracked request from an approved review.

    The fingerprint is derived from the review ID, so retrying the same
    approval idempotently returns the existing request (ON CONFLICT).

    Returns the created/existing request dict, or None on error.
    """
    title = f"Approved: {request.get('title', 'Untitled')}"
    description = (
        f"Auto-created from approved review {review_id} "
        f"of request {request.get('id', '')}.\n\n"
        f"Original request: {request.get('title', '')}\n"
        f"{request.get('description') or ''}"
    )
    try:
        result = await req_repo.create_request(
            title=title,
            org_id=org_id,
            description=description,
            source="api",
            priority=request.get("priority", "medium"),
            created_by=str(user.id),
        )
        logger.info(
            "Created tracked request %s from approved review %s",
            result.get("id"), review_id,
        )
        return result
    except Exception:
        logger.exception(
            "Failed to create tracked request from approved review %s",
            review_id,
        )
        return None


async def _create_rejection_memory(
    pool, request: dict, comments: str | None, org_id: str, user
) -> dict | None:
    """Create a memory capturing the rejection reason for learning extraction.

    Tagged with 'rejection-lesson', 'learning-extraction', and 'daemon'
    so the learning extraction system can discover and process it.

    Returns the created memory dict, or None on error.
    """
    from lucent.db.memory import MemoryRepository

    content = (
        f"Review rejection for request '{request.get('title', 'Untitled')}'.\n\n"
        f"Rejection reason: {comments or 'No reason provided'}\n\n"
        f"Request ID: {request.get('id', '')}\n"
        f"Request description: {request.get('description') or 'N/A'}"
    )

    try:
        repo = MemoryRepository(pool)
        memory = await repo.create(
            username=user.display_name or user.email or "system",
            type="experience",
            content=content,
            tags=["rejection-lesson", "learning-extraction", "daemon"],
            importance=6,
            metadata={
                "context": "review_rejection",
                "outcome": "rejected",
                "lessons_learned": [comments] if comments else [],
                "related_entities": [
                    str(request.get("id", "")),
                    request.get("title", ""),
                ],
            },
            user_id=user.id,
            organization_id=UUID(org_id),
        )
        logger.info(
            "Created rejection-lesson memory %s for request %s",
            memory.get("id"), request.get("id"),
        )
        return memory
    except Exception:
        logger.exception(
            "Failed to create rejection-lesson memory for request %s",
            request.get("id"),
        )
        return None


@router.get("")
async def list_reviews(
    user: AuthenticatedUser,
    request_id: str | None = Query(default=None, description="Filter by request"),
    task_id: str | None = Query(default=None, description="Filter by task"),
    status: str | None = Query(default=None, pattern=r"^(approved|rejected)$"),
    source: str | None = Query(default=None, pattern=r"^(human|daemon|agent)$"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    pool=Depends(get_pool),
):
    """List reviews with optional filters, scoped to the user's organization."""
    from lucent.db.reviews import ReviewRepository

    repo = ReviewRepository(pool)
    result = await repo.list_reviews(
        str(user.organization_id),
        request_id=request_id,
        task_id=task_id,
        status=status,
        source=source,
        limit=limit,
        offset=offset,
    )

    result["items"] = [_serialize_review(r) for r in result["items"]]
    return result


@router.get("/summary")
async def review_summary(
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Get aggregate review statistics for the organization."""
    from lucent.db.reviews import ReviewRepository

    repo = ReviewRepository(pool)
    return await repo.get_review_summary(str(user.organization_id))


@router.get("/{review_id}")
async def get_review(
    review_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Get a single review by ID."""
    from lucent.db.reviews import ReviewRepository

    repo = ReviewRepository(pool)
    review = await repo.get_review(str(review_id), str(user.organization_id))
    if not review:
        raise HTTPException(404, "Review not found")
    return _serialize_review(review)


@router.get("/by-request/{request_id}")
async def get_reviews_for_request(
    request_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Get all reviews for a specific request."""
    from lucent.db.reviews import ReviewRepository

    repo = ReviewRepository(pool)
    reviews = await repo.get_reviews_for_request(
        str(request_id), str(user.organization_id)
    )
    return [_serialize_review(r) for r in reviews]


@router.get("/by-task/{task_id}")
async def get_reviews_for_task(
    task_id: UUID,
    user: AuthenticatedUser,
    pool=Depends(get_pool),
):
    """Get all reviews for a specific task."""
    from lucent.db.reviews import ReviewRepository

    repo = ReviewRepository(pool)
    reviews = await repo.get_reviews_for_task(
        str(task_id), str(user.organization_id)
    )
    return [_serialize_review(r) for r in reviews]


# ── Helpers ───────────────────────────────────────────────────────────────


def _serialize_review(review: dict) -> dict:
    """Serialize a review record for JSON response."""
    result = {}
    for key, value in review.items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result
