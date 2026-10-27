"""Daemon activity routes — the human review action surface."""

import html
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from lucent.db import MemoryRepository, get_pool
from lucent.logging import get_logger

from ._shared import _check_csrf, get_user_context
from .requests_routes import can_review_request, request_visibility_context

logger = get_logger("web.routes.daemon")

router = APIRouter()


# =============================================================================
# Daemon Activity
# =============================================================================


@router.post("/daemon/review/{request_id}/action", response_class=HTMLResponse)
async def daemon_review_action(
    request: Request,
    request_id: UUID,
    action: str = Form(...),
    comment: str = Form(""),
):
    """Handle approve/reject for both pre-work approvals and post-completion reviews."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    if len(comment) > 10000:
        raise HTTPException(
            status_code=422, detail="Comments must be at most 10000 characters"
        )
    if action == "reject" and not comment.strip():
        raise HTTPException(status_code=400, detail="Comments are required when rejecting")

    from lucent.db.requests import RequestRepository

    req_repo = RequestRepository(pool)
    org_id = str(user.organization_id)

    # Fetch the request to determine which flow we're in
    requester_user_id, include_system = request_visibility_context(user)
    req = await req_repo.get_request(
        str(request_id),
        org_id,
        requester_user_id=requester_user_id,
        include_system=include_system,
    )
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if not can_review_request(user, req):
        raise HTTPException(status_code=403, detail="You cannot review this request")

    is_approval_gate = req.get("approval_status") == "pending_approval"
    is_post_review = req.get("status") == "review"

    if not is_approval_gate and not is_post_review:
        raise HTTPException(
            status_code=409,
            detail="Request is not awaiting approval or review",
        )

    if is_approval_gate:
        # Pre-work approval: approve → allow work to start, reject → cancel
        if action == "approve":
            result = await req_repo.approve_request(
                str(request_id), org_id, str(user.id), comment.strip() or None
            )
        else:
            result = await req_repo.reject_request(
                str(request_id), org_id, str(user.id), comment.strip()
            )
        if not result:
            raise HTTPException(status_code=409, detail="Request already processed")

        # Notify daemon
        try:
            await RequestRepository(pool).notify_ready(
                org_id=org_id,
                payload=f'{{"type": "approval", "action": "{action}", "request_id": "{request_id}"}}',
            )
        except Exception:
            pass

        # Create learning memory from rejection
        if action == "reject":
            memo_repo = MemoryRepository(pool)
            await memo_repo.create(
                username=user.display_name or user.email or "reviewer",
                type="experience",
                content=(
                    f"Request rejected before work began: '{req.get('title', '')}'\n"
                    f"Reason: {comment.strip()}\n"
                    f"Description: {req.get('description', 'N/A')}"
                ),
                tags=[
                    "rejection-lesson",
                    "approval-rejected",
                    "feedback-rejected",
                    "learning-extraction",
                ],
                metadata={
                    "request_id": str(request_id),
                    "reviewer": user.display_name or user.email,
                    "source": req.get("source", "unknown"),
                },
                user_id=user.id,
                organization_id=user.organization_id,
            )

    else:
        # Post-completion review (existing flow)
        from lucent.db.reviews import ReviewRepository

        review_repo = ReviewRepository(pool)

        status = "approved" if action == "approve" else "rejected"
        updated = await review_repo.process_status_review(
            request_id=str(request_id),
            organization_id=org_id,
            status=status,
            reviewer_user_id=str(user.id),
            reviewer_display_name=user.display_name or user.email,
            comments=comment.strip() or None,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="Request is not in review state")

        # Create learning memory from rejection
        if action == "reject":
            memo_repo = MemoryRepository(pool)
            await memo_repo.create(
                username=user.display_name or user.email or "reviewer",
                type="experience",
                content=f"Review rejection for '{req.get('title', '')}': {comment.strip()}",
                tags=["rejection-lesson", "learning-extraction", "daemon"],
                metadata={
                    "request_id": str(request_id),
                    "reviewer": user.display_name or user.email,
                },
                user_id=user.id,
                organization_id=user.organization_id,
            )

        # Notify daemon
        try:
            await RequestRepository(pool).notify_ready(
                org_id=org_id,
                payload=f'{{"type": "review", "action": "{action}", "request_id": "{request_id}"}}',
            )
        except Exception:
            pass

    # Return HTMX partial showing the result
    if action == "approve":
        return HTMLResponse(
            '<div class="flex items-center gap-2 p-3 bg-emerald-50 rounded-lg '
            'border border-emerald-200">'
            '<svg class="w-5 h-5 text-emerald-600" fill="none" viewBox="0 0 24 24" '
            'stroke-width="1.5" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" '
            'd="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>'
            '<span class="text-sm font-medium text-emerald-800">Approved</span></div>'
        )
    else:
        escaped_comment = html.escape(comment.strip())
        return HTMLResponse(
            '<div class="flex items-center gap-2 p-3 bg-red-50 rounded-lg border '
            'border-red-200">'
            '<svg class="w-5 h-5 text-red-600" fill="none" viewBox="0 0 24 24" '
            'stroke-width="1.5" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" '
            'd="M9.75 9.75l4.5 4.5m0-4.5l-4.5 4.5M21 12a9 9 0 11-18 0 9 9 0 0118 0z" '
            '/></svg>'
            '<span class="text-sm font-medium text-red-800">'
            f"Rejected — {escaped_comment}</span></div>"
        )
