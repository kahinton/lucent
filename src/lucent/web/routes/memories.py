"""Memory CRUD, search, share, and detail routes."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lucent.db import (
    AccessRepository,
    AuditRepository,
    MemoryRepository,
    UserRepository,
    get_pool,
)
from lucent.mode import is_team_mode

from ._shared import _build_metadata_from_form, _check_csrf, get_user_context, templates

router = APIRouter()


# =============================================================================
# Memories
# =============================================================================


@router.get("/memories", response_class=HTMLResponse)
async def memories_list(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    tag: str | None = None,
    page: int = 1,
):
    """List and search memories."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)
    access_repo = AccessRepository(pool)

    # Treat empty strings as None
    q = q if q else None
    type = type if type else None
    tag = tag if tag else None

    # Convert single tag to list for the search
    tag_list = [tag] if tag else None

    limit = 20
    offset = (page - 1) * limit

    result = await repo.search(
        query=q,
        type=type,
        tags=tag_list,
        offset=offset,
        limit=limit,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Attach access counts for list display
    memory_ids = [m["id"] for m in result["memories"]]
    access_counts = await access_repo.get_access_counts(memory_ids)
    for memory in result["memories"]:
        memory["access_count"] = access_counts.get(memory["id"], 0)

    # Get tags for filter (with access control)
    tags = await repo.get_existing_tags(
        limit=20,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    total_pages = (result["total_count"] + limit - 1) // limit

    # For HTMX partial updates
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "partials/memory_list.html",
            {
                "memories": result["memories"],
                "total_count": result["total_count"],
                "page": page,
                "total_pages": total_pages,
                "query": q,
                "type_filter": type,
                "tag_filter": tag,
            },
        )

    return templates.TemplateResponse(
        request,
        "memories.html",
        {
            "user": user,
            "memories": result["memories"],
            "total_count": result["total_count"],
            "page": page,
            "total_pages": total_pages,
            "query": q,
            "type_filter": type,
            "tag_filter": tag,
            "tags": tags,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
        },
    )


# New memory routes - MUST be before /memories/{memory_id} to avoid route conflicts
@router.get("/memories/new", response_class=HTMLResponse)
async def memory_new_form(request: Request):
    """New memory form."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)
    user_repo = UserRepository(pool)

    tags = await repo.get_existing_tags(
        limit=30,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )

    # Get users in the organization for linking individual memories
    org_users = (
        await user_repo.get_by_organization(user.organization_id) if user.organization_id else []
    )

    return templates.TemplateResponse(
        request,
        "memory_new.html",
        {
            "user": user,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
            "existing_tags": tags,
            "org_users": org_users,
        },
    )


@router.post("/memories/new", response_class=HTMLResponse)
async def memory_new_submit(
    request: Request,
    type: str = Form(...),
    content: str = Form(...),
    tags: str = Form(""),
    importance: int = Form(5),
    # Experience metadata
    meta_context: str = Form(""),
    meta_outcome: str = Form(""),
    meta_lessons_learned: str = Form(""),
    meta_related_entities: str = Form(""),
    # Technical metadata
    meta_category: str = Form(""),
    meta_language: str = Form(""),
    meta_version_info: str = Form(""),
    meta_repo: str = Form(""),
    meta_filename: str = Form(""),
    meta_code_snippet: str = Form(""),
    meta_references: str = Form(""),
    # Procedural metadata
    meta_estimated_time: str = Form(""),
    meta_success_criteria: str = Form(""),
    meta_prerequisites: str = Form(""),
    meta_common_pitfalls: str = Form(""),
    meta_steps: str = Form(""),
    # Goal metadata
    meta_status: str = Form("active"),
    meta_priority: int = Form(3),
    meta_deadline: str = Form(""),
    meta_blockers: str = Form(""),
    meta_milestones: str = Form(""),
    # Individual metadata
    meta_user_id: str = Form(""),
    meta_name: str = Form(""),
    meta_relationship: str = Form(""),
    meta_organization: str = Form(""),
    meta_role: str = Form(""),
    meta_email: str = Form(""),
    meta_phone: str = Form(""),
    meta_linkedin: str = Form(""),
    meta_github: str = Form(""),
    meta_preferences: str = Form(""),
):
    """Handle new memory form submission."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Use the logged-in user's display name as username
    username = user.display_name or "unknown"

    # Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]

    # Individual memories cannot be created via web interface
    # - they are auto-created when users join
    if type == "individual":
        raise HTTPException(
            status_code=400,
            detail=(
                "Individual memories cannot be created directly."
                " They are automatically created when users are"
                " added to the system."
            ),
        )

    # Build type-specific metadata
    metadata = _build_metadata_from_form(
        type,
        meta_context=meta_context,
        meta_outcome=meta_outcome,
        meta_lessons_learned=meta_lessons_learned,
        meta_related_entities=meta_related_entities,
        meta_category=meta_category,
        meta_language=meta_language,
        meta_version_info=meta_version_info,
        meta_repo=meta_repo,
        meta_filename=meta_filename,
        meta_code_snippet=meta_code_snippet,
        meta_references=meta_references,
        meta_estimated_time=meta_estimated_time,
        meta_success_criteria=meta_success_criteria,
        meta_prerequisites=meta_prerequisites,
        meta_common_pitfalls=meta_common_pitfalls,
        meta_steps=meta_steps,
        meta_status=meta_status,
        meta_priority=meta_priority,
        meta_deadline=meta_deadline,
        meta_blockers=meta_blockers,
        meta_milestones=meta_milestones,
    )

    # Create memory
    result = await repo.create(
        username=username,
        type=type,
        content=content,
        tags=tag_list if tag_list else None,
        importance=importance,
        metadata=metadata if metadata else None,
        user_id=user.id,
        organization_id=user.organization_id,
    )

    # Log creation
    await audit_repo.log(
        memory_id=result["id"],
        action_type="create",
        user_id=user.id,
        organization_id=user.organization_id,
        new_values={
            "username": username,
            "type": type,
            "content": content,
            "tags": tag_list,
            "importance": importance,
            "metadata": metadata,
        },
    )

    return RedirectResponse(f"/memories/{result['id']}", status_code=303)


# Memory by ID routes
@router.get("/memories/{memory_id}", response_class=HTMLResponse)
async def memory_detail(request: Request, memory_id: UUID):
    """View memory details."""
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)
    access_repo = AccessRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Log access
    await access_repo.log_access(
        memory_id=memory_id,
        access_type="view",
        user_id=user.id,
        organization_id=user.organization_id,
    )

    # Get audit history
    audit = await audit_repo.get_by_memory_id(memory_id, limit=10)

    # Get version history
    versions = await audit_repo.get_versions(memory_id, limit=20)

    # Get access history
    access = await access_repo.get_access_history(memory_id, limit=10)

    # Access count (includes this view)
    counts = await access_repo.get_access_counts([memory_id])
    memory["access_count"] = counts.get(memory_id, 0)

    is_owner = memory.get("user_id") == user.id

    return templates.TemplateResponse(
        request,
        "memory_detail.html",
        {
            "user": user,
            "memory": memory,
            "audit_entries": audit["entries"],
            "version_entries": versions["versions"],
            "access_entries": access["entries"],
            "is_owner": is_owner,
        },
    )


@router.get("/memories/{memory_id}/edit", response_class=HTMLResponse)
async def memory_edit_form(request: Request, memory_id: UUID):
    """Edit memory form."""
    user = await get_user_context(request)
    pool = await get_pool()
    repo = MemoryRepository(pool)
    user_repo = UserRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own memories")

    # Get users in the organization for linking individual memories
    org_users = (
        await user_repo.get_by_organization(user.organization_id) if user.organization_id else []
    )

    return templates.TemplateResponse(
        request,
        "memory_edit.html",
        {
            "user": user,
            "memory": memory,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
            "org_users": org_users,
        },
    )


@router.post("/memories/{memory_id}/edit", response_class=HTMLResponse)
async def memory_edit_submit(
    request: Request,
    memory_id: UUID,
    content: str = Form(...),
    tags: str = Form(""),
    importance: int = Form(5),
    # Experience metadata
    meta_context: str = Form(""),
    meta_outcome: str = Form(""),
    meta_lessons_learned: str = Form(""),
    meta_related_entities: str = Form(""),
    # Technical metadata
    meta_category: str = Form(""),
    meta_language: str = Form(""),
    meta_version_info: str = Form(""),
    meta_repo: str = Form(""),
    meta_filename: str = Form(""),
    meta_code_snippet: str = Form(""),
    meta_references: str = Form(""),
    # Procedural metadata
    meta_estimated_time: str = Form(""),
    meta_success_criteria: str = Form(""),
    meta_prerequisites: str = Form(""),
    meta_common_pitfalls: str = Form(""),
    meta_steps: str = Form(""),
    # Goal metadata
    meta_status: str = Form("active"),
    meta_priority: int = Form(3),
    meta_deadline: str = Form(""),
    meta_blockers: str = Form(""),
    meta_milestones: str = Form(""),
    # Individual metadata
    meta_user_id: str = Form(""),
    meta_name: str = Form(""),
    meta_relationship: str = Form(""),
    meta_organization: str = Form(""),
    meta_role: str = Form(""),
    meta_email: str = Form(""),
    meta_phone: str = Form(""),
    meta_linkedin: str = Form(""),
    meta_github: str = Form(""),
    meta_preferences: str = Form(""),
):
    """Handle memory edit form submission."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    # Get existing to check ownership
    existing = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if existing.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own memories")

    # Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]

    # Build type-specific metadata based on the memory's type
    memory_type = existing.get("type")
    metadata = _build_metadata_from_form(
        memory_type,
        meta_context=meta_context,
        meta_outcome=meta_outcome,
        meta_lessons_learned=meta_lessons_learned,
        meta_related_entities=meta_related_entities,
        meta_category=meta_category,
        meta_language=meta_language,
        meta_version_info=meta_version_info,
        meta_repo=meta_repo,
        meta_filename=meta_filename,
        meta_code_snippet=meta_code_snippet,
        meta_references=meta_references,
        meta_estimated_time=meta_estimated_time,
        meta_success_criteria=meta_success_criteria,
        meta_prerequisites=meta_prerequisites,
        meta_common_pitfalls=meta_common_pitfalls,
        meta_steps=meta_steps,
        meta_status=meta_status,
        meta_priority=meta_priority,
        meta_deadline=meta_deadline,
        meta_blockers=meta_blockers,
        meta_milestones=meta_milestones,
        meta_user_id=meta_user_id,
        meta_name=meta_name,
        meta_relationship=meta_relationship,
        meta_organization=meta_organization,
        meta_role=meta_role,
        meta_email=meta_email,
        meta_phone=meta_phone,
        meta_linkedin=meta_linkedin,
        meta_github=meta_github,
        meta_preferences=meta_preferences,
    )

    # Update
    result = await repo.update(
        memory_id=memory_id,
        content=content,
        tags=tag_list if tag_list else None,
        importance=importance,
        metadata=metadata if metadata else None,
    )

    # Log the update with version snapshot
    await audit_repo.log(
        memory_id=memory_id,
        action_type="update",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["content", "tags", "importance", "metadata"],
        old_values={
            "content": existing["content"],
            "tags": existing["tags"],
            "importance": existing["importance"],
            "metadata": existing.get("metadata"),
        },
        new_values={
            "content": content,
            "tags": tag_list,
            "importance": importance,
            "metadata": metadata,
        },
        version=result["version"] if result else None,
        snapshot={
            "content": result["content"],
            "tags": result["tags"],
            "importance": result["importance"],
            "metadata": result["metadata"],
            "related_memory_ids": [str(uid) for uid in result.get("related_memory_ids", [])],
            "shared": result.get("shared", False),
        }
        if result
        else None,
    )

    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.post("/memories/{memory_id}/share", response_class=HTMLResponse)
async def memory_share(request: Request, memory_id: UUID):
    """Toggle memory sharing (team mode only)."""
    if not is_team_mode():
        raise HTTPException(status_code=404, detail="Sharing requires team mode")
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only share your own memories")

    new_shared = not memory.get("shared", False)
    await repo.set_shared(memory_id, user.id, new_shared)

    await audit_repo.log(
        memory_id=memory_id,
        action_type="share" if new_shared else "unshare",
        user_id=user.id,
        organization_id=user.organization_id,
        changed_fields=["shared"],
        old_values={"shared": not new_shared},
        new_values={"shared": new_shared},
    )

    # Return updated button for HTMX
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f"""<button
                hx-post="/memories/{memory_id}/share"
                hx-swap="outerHTML"
                class="btn {"btn-warning" if new_shared else "btn-primary"}">
                {"Unshare" if new_shared else "Share"}
            </button>"""
        )

    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.post("/memories/{memory_id}/delete", response_class=HTMLResponse)
async def memory_delete(request: Request, memory_id: UUID):
    """Delete a memory."""
    await _check_csrf(request)
    user = await get_user_context(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Individual memories cannot be deleted via web interface
    # - they are deleted when users are removed
    if memory.get("type") == "individual":
        raise HTTPException(
            status_code=400,
            detail=(
                "Individual memories cannot be deleted directly."
                " They are automatically deleted when users are"
                " removed from the system."
            ),
        )

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own memories")

    await repo.delete(memory_id)

    await audit_repo.log(
        memory_id=memory_id,
        action_type="delete",
        user_id=user.id,
        organization_id=user.organization_id,
        old_values={
            "content": memory["content"],
            "tags": memory["tags"],
        },
        snapshot={
            "content": memory["content"],
            "tags": memory["tags"],
            "importance": memory["importance"],
            "metadata": memory["metadata"],
            "related_memory_ids": [str(uid) for uid in memory.get("related_memory_ids", [])],
            "shared": memory.get("shared", False),
        },
    )

    return RedirectResponse("/memories", status_code=303)


@router.post("/memories/{memory_id}/restore/{version}", response_class=HTMLResponse)
async def memory_restore(request: Request, memory_id: UUID, version: int):
    """Restore a memory to a previous version."""
    user = await get_user_context(request)
    await _check_csrf(request)
    pool = await get_pool()

    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)

    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only restore your own memories")

    if memory["version"] == version:
        return RedirectResponse(f"/memories/{memory_id}", status_code=303)

    # Get the snapshot for the target version
    version_entry = await audit_repo.get_version_snapshot(memory_id, version)
    if version_entry is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")

    snapshot = version_entry.get("snapshot")
    if snapshot is None:
        raise HTTPException(
            status_code=400,
            detail=f"Version {version} does not have a restorable snapshot",
        )

    # Build old snapshot for audit
    old_snapshot = {
        "content": memory["content"],
        "tags": memory["tags"],
        "importance": memory["importance"],
        "metadata": memory["metadata"],
        "related_memory_ids": [str(uid) for uid in memory.get("related_memory_ids", [])],
        "shared": memory.get("shared", False),
    }

    # Apply the restore
    result = await repo.update(
        memory_id=memory_id,
        content=snapshot.get("content"),
        tags=snapshot.get("tags"),
        importance=snapshot.get("importance"),
        metadata=snapshot.get("metadata"),
        related_memory_ids=[UUID(uid) for uid in snapshot.get("related_memory_ids", [])],
    )

    if result is None:
        raise HTTPException(status_code=500, detail="Failed to apply restore")

    # Log the restore
    await audit_repo.log(
        memory_id=memory_id,
        action_type="restore",
        user_id=user.id,
        organization_id=user.organization_id,
        old_values=old_snapshot,
        new_values=snapshot,
        notes=f"Restored to version {version}",
        version=result["version"],
        snapshot={
            "content": result["content"],
            "tags": result["tags"],
            "importance": result["importance"],
            "metadata": result["metadata"],
            "related_memory_ids": [str(uid) for uid in result.get("related_memory_ids", [])],
            "shared": result.get("shared", False),
        },
    )

    return RedirectResponse(f"/memories/{memory_id}", status_code=303)
