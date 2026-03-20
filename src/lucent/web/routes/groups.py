"""Group management web routes."""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import GroupRepository, UserRepository, get_pool
from ._shared import _check_csrf, get_user_context, templates

router = APIRouter()


def _can_manage(user) -> bool:
    """Check if user is admin or owner."""
    role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
    return role_val in ("admin", "owner")


# =============================================================================
# Groups List
# =============================================================================


@router.get("/groups", response_class=HTMLResponse)
async def groups_list(request: Request):
    """List all groups in the organization."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = GroupRepository(pool)
    org_id = str(user.organization_id)

    result = await repo.list_groups(org_id=org_id, limit=100, offset=0)
    groups = []
    for group in result["items"]:
        members = await repo.list_members(str(group["id"]), org_id)
        groups.append({**group, "member_count": len(members)})

    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "user": user,
            "groups": groups,
            "can_manage": _can_manage(user),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


# =============================================================================
# Group Detail
# =============================================================================


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def group_detail(request: Request, group_id: UUID):
    """View group details with members."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = GroupRepository(pool)
    org_id = str(user.organization_id)

    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    members = await repo.list_members(str(group_id), org_id)
    is_group_admin = await repo.is_group_admin(str(user.id), str(group_id))

    # Get org users for the add-member dropdown (exclude current members)
    user_repo = UserRepository(pool)
    all_users = await user_repo.get_by_organization(user.organization_id)
    member_ids = {str(m["user_id"]) for m in members}
    available_users = [u for u in all_users if str(u["id"]) not in member_ids]

    can_edit = _can_manage(user) or is_group_admin
    can_delete = _can_manage(user)

    return templates.TemplateResponse(
        request,
        "group_detail.html",
        {
            "user": user,
            "group": group,
            "members": members,
            "available_users": available_users,
            "can_edit": can_edit,
            "can_delete": can_delete,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


# =============================================================================
# Create Group
# =============================================================================


@router.post("/groups/create")
async def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    """Create a new group (admin/owner only)."""
    await _check_csrf(request)
    user = await get_user_context(request)

    if not _can_manage(user):
        raise HTTPException(status_code=403, detail="Permission denied")

    pool = await get_pool()
    repo = GroupRepository(pool)

    try:
        group = await repo.create_group(
            name=name.strip(),
            description=description.strip(),
            org_id=str(user.organization_id),
            created_by=str(user.id),
        )
        return RedirectResponse(
            f"/groups/{group['id']}?success=Group+created", status_code=303
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or exc.__class__.__name__ == "UniqueViolationError":
            return RedirectResponse(
                f"/groups?error={quote('A group with this name already exists.')}",
                status_code=303,
            )
        raise


# =============================================================================
# Update Group
# =============================================================================


@router.post("/groups/{group_id}/edit")
async def edit_group(
    request: Request,
    group_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
):
    """Update group name/description (group admin or org admin/owner)."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = GroupRepository(pool)
    org_id = str(user.organization_id)

    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    is_group_admin = await repo.is_group_admin(str(user.id), str(group_id))
    if not _can_manage(user) and not is_group_admin:
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        await repo.update_group(
            str(group_id), org_id, name=name.strip(), description=description.strip()
        )
        return RedirectResponse(
            f"/groups/{group_id}?success=Group+updated", status_code=303
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or exc.__class__.__name__ == "UniqueViolationError":
            return RedirectResponse(
                f"/groups/{group_id}?error={quote('A group with this name already exists.')}",
                status_code=303,
            )
        raise


# =============================================================================
# Delete Group
# =============================================================================


@router.post("/groups/{group_id}/delete")
async def delete_group(request: Request, group_id: UUID):
    """Delete a group (org admin/owner only)."""
    await _check_csrf(request)
    user = await get_user_context(request)

    if not _can_manage(user):
        raise HTTPException(status_code=403, detail="Permission denied")

    pool = await get_pool()
    repo = GroupRepository(pool)
    org_id = str(user.organization_id)

    if not await repo.delete_group(str(group_id), org_id):
        raise HTTPException(status_code=404, detail="Group not found")

    return RedirectResponse("/groups?success=Group+deleted", status_code=303)


# =============================================================================
# Add Member
# =============================================================================


@router.post("/groups/{group_id}/members/add")
async def add_member(
    request: Request,
    group_id: UUID,
    user_id: str = Form(...),
    role: str = Form("member"),
):
    """Add a member to a group (group admin or org admin/owner)."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = GroupRepository(pool)
    org_id = str(user.organization_id)

    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    is_group_admin = await repo.is_group_admin(str(user.id), str(group_id))
    if not _can_manage(user) and not is_group_admin:
        raise HTTPException(status_code=403, detail="Permission denied")

    if role not in ("member", "admin"):
        role = "member"

    try:
        # Anti-spoofing V4: validate target user belongs to same org
        user_repo = UserRepository(pool)
        target_user = await user_repo.get_by_id(UUID(user_id.strip()))
        if not target_user or target_user.get("organization_id") != user.organization_id:
            return RedirectResponse(
                f"/groups/{group_id}?error={quote('User not found in this organization.')}",
                status_code=303,
            )

        await repo.add_member(str(group_id), user_id.strip(), role=role)
        return RedirectResponse(
            f"/groups/{group_id}?success=Member+added", status_code=303
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or exc.__class__.__name__ == "UniqueViolationError":
            return RedirectResponse(
                f"/groups/{group_id}?error={quote('User is already a member of this group.')}",
                status_code=303,
            )
        raise


# =============================================================================
# Remove Member
# =============================================================================


@router.post("/groups/{group_id}/members/{member_id}/remove")
async def remove_member(request: Request, group_id: UUID, member_id: UUID):
    """Remove a member from a group (group admin or org admin/owner)."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()
    repo = GroupRepository(pool)
    org_id = str(user.organization_id)

    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    is_group_admin = await repo.is_group_admin(str(user.id), str(group_id))
    if not _can_manage(user) and not is_group_admin:
        raise HTTPException(status_code=403, detail="Permission denied")

    if not await repo.remove_member(str(group_id), str(member_id)):
        return RedirectResponse(
            f"/groups/{group_id}?error={quote('Member not found.')}", status_code=303
        )

    return RedirectResponse(
        f"/groups/{group_id}?success=Member+removed", status_code=303
    )
