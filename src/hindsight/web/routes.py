"""Web routes for Hindsight admin dashboard using Jinja2 + HTMX."""

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hindsight.api.deps import get_current_user, CurrentUser
from hindsight.db.client import (
    MemoryRepository, 
    AuditRepository, 
    AccessRepository,
    UserRepository,
    OrganizationRepository,
    get_pool,
)


# Set up templates
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


# Custom template filters
def format_datetime(value: datetime | None) -> str:
    """Format datetime for display."""
    if value is None:
        return "Never"
    return value.strftime("%Y-%m-%d %H:%M")


def truncate(value: str, length: int = 100) -> str:
    """Truncate string to length."""
    if len(value) <= length:
        return value
    return value[:length] + "..."


# Register filters
templates.env.filters["datetime"] = format_datetime
templates.env.filters["truncate"] = truncate


async def get_user_context(request: Request) -> CurrentUser:
    """Get the current user for web routes."""
    return await get_current_user()


# =============================================================================
# Dashboard
# =============================================================================

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    user = await get_user_context(request)
    pool = await get_pool()
    
    # Get stats
    memory_repo = MemoryRepository(pool)
    access_repo = AccessRepository(pool)
    
    # Recent memories
    recent = await memory_repo.search(
        limit=5,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    
    # Most accessed
    most_accessed = await access_repo.get_most_accessed(
        user_id=user.id,
        limit=5,
    )
    
    # Get tag stats (with access control)
    tags = await memory_repo.get_existing_tags(
        limit=10,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "recent_memories": recent["memories"],
            "most_accessed": most_accessed,
            "top_tags": tags,
            "total_memories": recent["total_count"],
        },
    )


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
            "partials/memory_list.html",
            {
                "request": request,
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
        "memories.html",
        {
            "request": request,
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
    
    tags = await repo.get_existing_tags(
        limit=30,
        requesting_user_id=user.id,
        requesting_org_id=user.organization_id,
    )
    
    return templates.TemplateResponse(
        "memory_new.html",
        {
            "request": request,
            "user": user,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
            "existing_tags": tags,
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
    pool = await get_pool()
    
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)
    
    # Use the logged-in user's display name as username
    username = user.display_name or user.external_id or "unknown"
    
    # Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    
    # Build type-specific metadata
    metadata: dict = {}
    
    if type == "experience":
        if meta_context:
            metadata["context"] = meta_context
        if meta_outcome:
            metadata["outcome"] = meta_outcome
        if meta_lessons_learned:
            metadata["lessons_learned"] = [l.strip() for l in meta_lessons_learned.split(",") if l.strip()]
        if meta_related_entities:
            metadata["related_entities"] = [e.strip() for e in meta_related_entities.split(",") if e.strip()]
    
    elif type == "technical":
        if meta_category:
            metadata["category"] = meta_category
        if meta_language:
            metadata["language"] = meta_language
        if meta_version_info:
            metadata["version_info"] = meta_version_info
        if meta_repo:
            metadata["repo"] = meta_repo
        if meta_filename:
            metadata["filename"] = meta_filename
        if meta_code_snippet:
            metadata["code_snippet"] = meta_code_snippet
        if meta_references:
            metadata["references"] = [r.strip() for r in meta_references.split(",") if r.strip()]
    
    elif type == "procedural":
        if meta_estimated_time:
            metadata["estimated_time"] = meta_estimated_time
        if meta_success_criteria:
            metadata["success_criteria"] = meta_success_criteria
        if meta_prerequisites:
            metadata["prerequisites"] = [p.strip() for p in meta_prerequisites.split(",") if p.strip()]
        if meta_common_pitfalls:
            metadata["common_pitfalls"] = [p.strip() for p in meta_common_pitfalls.split(",") if p.strip()]
        if meta_steps:
            steps = []
            for i, line in enumerate(meta_steps.strip().split("\n"), 1):
                if line.strip():
                    parts = line.split("|", 1)
                    step = {"order": i, "description": parts[0].strip()}
                    if len(parts) > 1 and parts[1].strip():
                        step["notes"] = parts[1].strip()
                    steps.append(step)
            if steps:
                metadata["steps"] = steps
    
    elif type == "goal":
        metadata["status"] = meta_status
        metadata["priority"] = meta_priority
        if meta_deadline:
            metadata["deadline"] = meta_deadline
        if meta_blockers:
            metadata["blockers"] = [b.strip() for b in meta_blockers.split(",") if b.strip()]
        if meta_milestones:
            metadata["milestones"] = [
                {"description": m.strip(), "status": "active"} 
                for m in meta_milestones.split(",") if m.strip()
            ]
    
    elif type == "individual":
        if meta_name:
            metadata["name"] = meta_name
        if meta_relationship:
            metadata["relationship"] = meta_relationship
        if meta_organization:
            metadata["organization"] = meta_organization
        if meta_role:
            metadata["role"] = meta_role
        contact_info = {}
        if meta_email:
            contact_info["email"] = meta_email
        if meta_phone:
            contact_info["phone"] = meta_phone
        if meta_linkedin:
            contact_info["linkedin"] = meta_linkedin
        if meta_github:
            contact_info["github"] = meta_github
        if contact_info:
            metadata["contact_info"] = contact_info
        if meta_preferences:
            metadata["preferences"] = [p.strip() for p in meta_preferences.split(",") if p.strip()]
    
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
    
    # Get access history
    access = await access_repo.get_access_history(memory_id, limit=10)
    
    is_owner = memory.get("user_id") == user.id
    
    return templates.TemplateResponse(
        "memory_detail.html",
        {
            "request": request,
            "user": user,
            "memory": memory,
            "audit_entries": audit["entries"],
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
    
    memory = await repo.get_accessible(memory_id, user.id, user.organization_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    
    if memory.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own memories")
    
    return templates.TemplateResponse(
        "memory_edit.html",
        {
            "request": request,
            "user": user,
            "memory": memory,
            "memory_types": ["experience", "technical", "procedural", "goal", "individual"],
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
    user = await get_user_context(request)
    pool = await get_pool()
    
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)
    
    # Get existing to check ownership
    existing = await repo.get(memory_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    
    if existing.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own memories")
    
    # Parse tags
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
    
    # Build type-specific metadata based on the memory's type
    memory_type = existing.get("type")
    metadata: dict = {}
    
    if memory_type == "experience":
        if meta_context:
            metadata["context"] = meta_context
        if meta_outcome:
            metadata["outcome"] = meta_outcome
        if meta_lessons_learned:
            metadata["lessons_learned"] = [l.strip() for l in meta_lessons_learned.split(",") if l.strip()]
        if meta_related_entities:
            metadata["related_entities"] = [e.strip() for e in meta_related_entities.split(",") if e.strip()]
    
    elif memory_type == "technical":
        if meta_category:
            metadata["category"] = meta_category
        if meta_language:
            metadata["language"] = meta_language
        if meta_version_info:
            metadata["version_info"] = meta_version_info
        if meta_repo:
            metadata["repo"] = meta_repo
        if meta_filename:
            metadata["filename"] = meta_filename
        if meta_code_snippet:
            metadata["code_snippet"] = meta_code_snippet
        if meta_references:
            metadata["references"] = [r.strip() for r in meta_references.split(",") if r.strip()]
    
    elif memory_type == "procedural":
        if meta_estimated_time:
            metadata["estimated_time"] = meta_estimated_time
        if meta_success_criteria:
            metadata["success_criteria"] = meta_success_criteria
        if meta_prerequisites:
            metadata["prerequisites"] = [p.strip() for p in meta_prerequisites.split(",") if p.strip()]
        if meta_common_pitfalls:
            metadata["common_pitfalls"] = [p.strip() for p in meta_common_pitfalls.split(",") if p.strip()]
        if meta_steps:
            steps = []
            for i, line in enumerate(meta_steps.strip().split("\n"), 1):
                if line.strip():
                    parts = line.split("|", 1)
                    step = {"order": i, "description": parts[0].strip()}
                    if len(parts) > 1 and parts[1].strip():
                        step["notes"] = parts[1].strip()
                    steps.append(step)
            if steps:
                metadata["steps"] = steps
    
    elif memory_type == "goal":
        metadata["status"] = meta_status
        metadata["priority"] = meta_priority
        if meta_deadline:
            metadata["deadline"] = meta_deadline
        if meta_blockers:
            metadata["blockers"] = [b.strip() for b in meta_blockers.split(",") if b.strip()]
        if meta_milestones:
            metadata["milestones"] = [
                {"description": m.strip(), "status": "active"} 
                for m in meta_milestones.split(",") if m.strip()
            ]
    
    elif memory_type == "individual":
        if meta_name:
            metadata["name"] = meta_name
        if meta_relationship:
            metadata["relationship"] = meta_relationship
        if meta_organization:
            metadata["organization"] = meta_organization
        if meta_role:
            metadata["role"] = meta_role
        contact_info = {}
        if meta_email:
            contact_info["email"] = meta_email
        if meta_phone:
            contact_info["phone"] = meta_phone
        if meta_linkedin:
            contact_info["linkedin"] = meta_linkedin
        if meta_github:
            contact_info["github"] = meta_github
        if contact_info:
            metadata["contact_info"] = contact_info
        if meta_preferences:
            metadata["preferences"] = [p.strip() for p in meta_preferences.split(",") if p.strip()]
    
    # Update
    result = await repo.update(
        memory_id=memory_id,
        content=content,
        tags=tag_list if tag_list else None,
        importance=importance,
        metadata=metadata if metadata else None,
    )
    
    # Log the update
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
    )
    
    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.post("/memories/{memory_id}/share", response_class=HTMLResponse)
async def memory_share(request: Request, memory_id: UUID):
    """Toggle memory sharing."""
    user = await get_user_context(request)
    pool = await get_pool()
    
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)
    
    memory = await repo.get(memory_id)
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
            f'''<button 
                hx-post="/memories/{memory_id}/share" 
                hx-swap="outerHTML"
                class="btn {'btn-warning' if new_shared else 'btn-primary'}">
                {'Unshare' if new_shared else 'Share'}
            </button>'''
        )
    
    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.post("/memories/{memory_id}/delete", response_class=HTMLResponse)
async def memory_delete(request: Request, memory_id: UUID):
    """Delete a memory."""
    user = await get_user_context(request)
    pool = await get_pool()
    
    repo = MemoryRepository(pool)
    audit_repo = AuditRepository(pool)
    
    memory = await repo.get(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    
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
    )
    
    return RedirectResponse("/memories", status_code=303)


# =============================================================================
# Audit Logs
# =============================================================================

@router.get("/audit", response_class=HTMLResponse)
async def audit_logs(
    request: Request,
    page: int = 1,
    action_type: str | None = None,
):
    """View audit logs."""
    user = await get_user_context(request)
    pool = await get_pool()
    audit_repo = AuditRepository(pool)
    
    limit = 50
    offset = (page - 1) * limit
    
    result = await audit_repo.get_by_organization_id(
        organization_id=user.organization_id,
        action_type=action_type,
        offset=offset,
        limit=limit,
    )
    
    total_pages = (result["total_count"] + limit - 1) // limit
    
    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request,
            "user": user,
            "entries": result["entries"],
            "total_count": result["total_count"],
            "page": page,
            "total_pages": total_pages,
            "action_type": action_type,
            "action_types": ["create", "update", "delete", "share", "unshare"],
        },
    )


# =============================================================================
# Users (Admin)
# =============================================================================

@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request):
    """List organization users."""
    user = await get_user_context(request)
    pool = await get_pool()
    user_repo = UserRepository(pool)
    
    users = await user_repo.get_by_organization(user.organization_id)
    
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "user": user,
            "users": users,
        },
    )


# =============================================================================
# Settings
# =============================================================================

@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    """User and organization settings."""
    user = await get_user_context(request)
    pool = await get_pool()
    org_repo = OrganizationRepository(pool)
    
    org = await org_repo.get_by_id(user.organization_id)
    
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "organization": org,
        },
    )
