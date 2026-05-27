"""Group management API endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from lucent.api.deps import AuthenticatedUser
from lucent.api.models import (
    GroupCreate,
    GroupMemberAdd,
    GroupMemberResponse,
    GroupResponse,
    GroupUpdate,
)
from lucent.db import GroupRepository, UserRepository, get_pool
from lucent.rbac import Permission, Role

router = APIRouter(prefix="/groups", tags=["groups"])


async def _require_org(user: AuthenticatedUser) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not part of an organization",
        )
    return str(user.organization_id)


def _to_group_response(group: dict[str, Any], member_count: int) -> GroupResponse:
    return GroupResponse(
        id=group["id"],
        name=group["name"],
        description=group.get("description"),
        org_id=group["organization_id"],
        member_count=member_count,
        created_at=group["created_at"],
        updated_at=group["updated_at"],
    )


def _to_member_response(member: dict[str, Any]) -> GroupMemberResponse:
    return GroupMemberResponse(
        user_id=member["user_id"],
        display_name=member.get("display_name"),
        email=member.get("email"),
        role=member["role"],
        joined_at=member["created_at"],
    )


async def _require_group_admin_or_org_admin(
    repo: GroupRepository, group_id: str, user: AuthenticatedUser
) -> None:
    if user.role >= Role.ADMIN:
        return
    if not await repo.is_group_admin(str(user.id), group_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires group admin or org admin role",
        )


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(data: GroupCreate, user: AuthenticatedUser) -> GroupResponse:
    user.require_permission(Permission.USERS_MANAGE)
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    try:
        group = await repo.create_group(
            name=data.name,
            description=data.description or "",
            org_id=org_id,
            created_by=str(user.id),
        )
    except Exception as exc:
        if exc.__class__.__name__ == "UniqueViolationError":
            raise HTTPException(
                status_code=409, detail="Group with this name already exists"
            ) from exc
        raise
    return _to_group_response(group, member_count=0)


@router.get("")
async def list_groups(
    user: AuthenticatedUser,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    result = await repo.list_groups(org_id=org_id, limit=limit, offset=offset)
    items = []
    for group in result["items"]:
        members = await repo.list_members(str(group["id"]), org_id)
        items.append(_to_group_response(group, member_count=len(members)))
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "total_count": result["total_count"],
        "offset": result["offset"],
        "limit": result["limit"],
        "has_more": result["has_more"],
    }


@router.get("/{group_id}")
async def get_group(group_id: UUID, user: AuthenticatedUser):
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    members = await repo.list_members(str(group_id), org_id)
    return {
        "group": _to_group_response(group, member_count=len(members)).model_dump(mode="json"),
        "members": [_to_member_response(m).model_dump(mode="json") for m in members],
    }


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group(group_id: UUID, data: GroupUpdate, user: AuthenticatedUser) -> GroupResponse:
    org_id = await _require_org(user)
    if data.name is None and data.description is None:
        raise HTTPException(status_code=422, detail="No fields to update")

    pool = await get_pool()
    repo = GroupRepository(pool)
    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    await _require_group_admin_or_org_admin(repo, str(group_id), user)
    try:
        updated = await repo.update_group(
            str(group_id), org_id, **data.model_dump(exclude_none=True)
        )
    except Exception as exc:
        if exc.__class__.__name__ == "UniqueViolationError":
            raise HTTPException(
                status_code=409, detail="Group with this name already exists"
            ) from exc
        raise
    if not updated:
        raise HTTPException(status_code=404, detail="Group not found")
    members = await repo.list_members(str(group_id), org_id)
    return _to_group_response(updated, member_count=len(members))


@router.delete("/{group_id}")
async def delete_group(group_id: UUID, user: AuthenticatedUser):
    if user.role < Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role or higher",
        )
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    if not await repo.delete_group(str(group_id), org_id):
        raise HTTPException(status_code=404, detail="Group not found")
    return {"success": True}


@router.post(
    "/{group_id}/members",
    response_model=GroupMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_group_member(
    group_id: UUID, data: GroupMemberAdd, user: AuthenticatedUser
) -> GroupMemberResponse:
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    user_repo = UserRepository(pool)

    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await _require_group_admin_or_org_admin(repo, str(group_id), user)

    target_user = await user_repo.get_by_id(data.user_id)
    if not target_user or str(target_user.get("organization_id")) != org_id:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        await repo.add_member(str(group_id), str(data.user_id), role=data.role)
    except Exception as exc:
        if exc.__class__.__name__ == "UniqueViolationError":
            raise HTTPException(status_code=409, detail="User is already a group member") from exc
        raise

    members = await repo.list_members(str(group_id), org_id)
    member = next((m for m in members if str(m["user_id"]) == str(data.user_id)), None)
    if not member:
        raise HTTPException(status_code=500, detail="Failed to load created member")
    return _to_member_response(member)


@router.delete("/{group_id}/members/{user_id}")
async def remove_group_member(group_id: UUID, user_id: UUID, user: AuthenticatedUser):
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await _require_group_admin_or_org_admin(repo, str(group_id), user)

    if not await repo.remove_member(str(group_id), str(user_id)):
        raise HTTPException(status_code=404, detail="Group member not found")
    return {"success": True}


@router.get("/{group_id}/members")
async def list_group_members(group_id: UUID, user: AuthenticatedUser):
    org_id = await _require_org(user)
    pool = await get_pool()
    repo = GroupRepository(pool)
    group = await repo.get_group(str(group_id), org_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    members = await repo.list_members(str(group_id), org_id)
    return {
        "group_id": str(group_id),
        "members": [_to_member_response(m).model_dump(mode="json") for m in members],
        "total_count": len(members),
    }
