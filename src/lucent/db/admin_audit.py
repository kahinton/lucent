"""Admin audit log repository.

Tracks administrative and security-sensitive actions that don't fit the
memory-centric ``memory_audit_log`` (which has a NOT NULL ``memory_id``).

Use ``AdminAuditRepository.log()`` from any handler that mutates users,
organizations, API keys, groups, sessions, or settings.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from asyncpg import Pool
from lucent.db.pool import scoped_acquire

if TYPE_CHECKING:
    from lucent.api.deps import CurrentUser

# ---------------------------------------------------------------------------
# Action constants — keep in one place so call sites stay consistent.
# ---------------------------------------------------------------------------

# User lifecycle
USER_CREATE = "user.create"
USER_UPDATE = "user.update"
USER_DELETE = "user.delete"
USER_ROLE_CHANGE = "user.role_change"
USER_DEACTIVATE = "user.deactivate"
USER_REACTIVATE = "user.reactivate"
USER_PASSWORD_RESET = "user.password_reset"  # admin-initiated

# Self-service security
PASSWORD_CHANGE = "auth.password_change"  # user changed their own
API_KEY_CREATE = "api_key.create"
API_KEY_REVOKE = "api_key.revoke"

# Sessions / impersonation
IMPERSONATION_START = "impersonation.start"
IMPERSONATION_STOP = "impersonation.stop"
SESSION_REVOKE_ALL = "session.revoke_all"

# Organization
ORG_UPDATE = "org.update"
ORG_DELETE = "org.delete"
ORG_TRANSFER_OWNERSHIP = "org.transfer_ownership"

# Groups
GROUP_CREATE = "group.create"
GROUP_DELETE = "group.delete"
GROUP_MEMBER_ADD = "group.member_add"
GROUP_MEMBER_REMOVE = "group.member_remove"

# Connections / integrations / secrets
CONNECTION_CREATE = "connection.create"
CONNECTION_DELETE = "connection.delete"
SECRET_CREATE = "secret.create"
SECRET_UPDATE = "secret.update"
SECRET_DELETE = "secret.delete"

# Models / definitions
MODEL_TOGGLE = "model.toggle"
MODEL_UPDATE = "model.update"
MODEL_CREATE = "model.create"
MODEL_DELETE = "model.delete"

# Definitions (agents, skills, hooks, managed tools, MCP servers)
DEFINITION_CREATE = "definition.create"
DEFINITION_UPDATE = "definition.update"
DEFINITION_APPROVE = "definition.approve"
DEFINITION_REJECT = "definition.reject"
DEFINITION_DELETE = "definition.delete"
DEFINITION_GRANT = "definition.grant"
DEFINITION_REVOKE = "definition.revoke"

# Sandbox templates
TEMPLATE_CREATE = "sandbox_template.create"
TEMPLATE_UPDATE = "sandbox_template.update"
TEMPLATE_DELETE = "sandbox_template.delete"
TEMPLATE_STATUS_CHANGE = "sandbox_template.status_change"

# Runtime settings
SETTING_UPDATE = "settings.update"
SETTING_RESET = "settings.reset"


def resolve_audit_actor(
    explicit_user_id: str | UUID | None = None,
) -> tuple[UUID | None, UUID | None, dict[str, Any]]:
    """Resolve the actor for an admin audit row.

    Resolution order: an explicitly passed user id wins; otherwise the
    request-scoped auth context (``lucent.auth.get_current_user``) supplies
    the actor. This lets the MCP-tool/daemon paths — which construct
    repositories without an explicit actor — still attribute mutations to
    the authenticated caller. Unauthenticated (system-originated) mutations
    resolve to a NULL actor with ``actor_source=system`` in the context.

    Returns:
        ``(actor_user_id, impersonator_user_id, auth_context)`` where
        ``auth_context`` carries only attribution markers (``auth_source``,
        ``actor_source``, ``actor_source_invalid``, ``impersonation``) to be
        merged into the audit row's context. Never raises: any attribution
        failure degrades to the system actor.

    Note:
        ``lucent.auth`` is imported at call time on purpose — it imports
        ``lucent.db`` at module level, so a module-level import here would
        be circular.
    """
    from lucent.auth import get_current_user  # function-level: avoids import cycle

    auth_ctx: dict[str, Any] = {}
    actor: UUID | None = None
    impersonator: UUID | None = None

    resolved = explicit_user_id
    if resolved is None:
        user = get_current_user()
        if user and user.get("id") is not None:
            try:
                resolved = str(user["id"])
            except Exception:
                resolved = None
        else:
            user = None
    else:
        user = None

    if resolved is not None:
        try:
            actor = UUID(str(resolved))
            auth_ctx["auth_source"] = "request_context"
        except (ValueError, AttributeError, TypeError):
            actor = None
            auth_ctx["actor_source_invalid"] = True
    if actor is None:
        auth_ctx["actor_source"] = "system"

    # Impersonation only matters when the actor came from the auth context;
    # an explicit user id is already the precise actor.
    if explicit_user_id is None and user:
        imp = user.get("impersonator_id")
        if imp:
            try:
                impersonator = UUID(imp)
                auth_ctx["impersonation"] = True
            except (ValueError, AttributeError, TypeError):
                impersonator = None

    return actor, impersonator, auth_ctx


class AdminAuditRepository:
    """Repository for admin/security audit log entries."""

    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    async def log(
        self,
        *,
        organization_id: UUID,
        action: str,
        entity_type: str,
        entity_id: UUID | str | None = None,
        entity_label: str | None = None,
        actor_user_id: UUID | None = None,
        impersonator_user_id: UUID | None = None,
        changed_fields: list[str] | None = None,
        old_values: dict[str, Any] | None = None,
        new_values: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
        outcome: str = "success",
    ) -> dict[str, Any]:
        """Insert a single audit row.

        Most callers should prefer :meth:`log_for_user`, which extracts the
        actor/impersonator/context bits from a ``CurrentUser`` and ``Request``.
        """
        if outcome not in {"success", "denied", "failed"}:
            outcome = "success"

        query = """
            INSERT INTO admin_audit_log
                (organization_id, actor_user_id, impersonator_user_id,
                 entity_type, entity_id, entity_label, action,
                 changed_fields, old_values, new_values,
                 context, notes, outcome)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING id, organization_id, actor_user_id, impersonator_user_id,
                      entity_type, entity_id, entity_label, action,
                      changed_fields, old_values, new_values,
                      context, notes, outcome, created_at
        """
        async with scoped_acquire(organization_id=organization_id, user_id=actor_user_id) as conn:
            row = await conn.fetchrow(
                query,
                str(organization_id),
                str(actor_user_id) if actor_user_id else None,
                str(impersonator_user_id) if impersonator_user_id else None,
                entity_type,
                str(entity_id) if entity_id else None,
                entity_label,
                action,
                changed_fields,
                json.dumps(old_values) if old_values is not None else None,
                json.dumps(new_values) if new_values is not None else None,
                json.dumps(context or {}),
                notes,
                outcome,
            )
        return dict(row) if row else {}

    async def log_definition_event(
        self,
        event_type: str,
        organization_id: UUID,
        user_id: UUID | None = None,
        impersonator_user_id: UUID | None = None,
        definition_type: str | None = None,
        definition_id: UUID | None = None,
        context: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Log a definition lifecycle event (agent/skill/hook/managed tool/MCP server).

        Mirrors ``AuditRepository.log_definition_event`` so the admin audit
        table can accept the same definition lifecycle events alongside the
        memory-centric audit stream. Definition events are stored with
        ``entity_type`` = definition_type and the dotted ``definition.*``
        action name derived from the event type.

        Event types are accepted in either form — the dotted admin action
        (``definition.update``) or the underscore memory-audit constant
        (``definition_update``) — since call sites pass both shapes.

        Args:
            event_type: One of the DEFINITION_EVENT_TYPES constants
                (e.g. ``definition_update``) or its dotted admin-audit form
                (e.g. ``definition.update``).
            organization_id: The organization this event belongs to.
            user_id: The user who triggered the event (if applicable).
            definition_type: 'agent', 'skill', 'hook', 'managed_tool', or
                'mcp_server'.
            definition_id: The UUID of the definition involved.
            context: Additional event-specific metadata.
            notes: Optional human-readable notes.

        Returns:
            The created audit log entry.
        """
        # Accept both the dotted admin action (definition.update) and the
        # underscore memory-audit constant (definition_update); the dotted
        # form is what call sites carry after the action map, the underscore
        # form what direct callers pass from DEFINITION_EVENT_TYPES.
        raw = str(event_type)
        dotted = raw if "." in raw else raw.replace("_", ".", 1)
        prefix, _, suffix = dotted.partition(".")
        if not suffix or prefix != "definition":
            raise ValueError(f"Unknown definition event type: {event_type}")
        ctx = dict(context) if context else {}
        if definition_type:
            ctx["definition_type"] = definition_type
        if definition_id:
            ctx["definition_id"] = str(definition_id)
        return await self.log(
            organization_id=organization_id,
            action=f"definition.{suffix}",
            entity_type=definition_type or "definition",
            entity_id=definition_id,
            actor_user_id=user_id,
            impersonator_user_id=impersonator_user_id,
            context=ctx,
            notes=notes,
        )

    async def log_for_user(
        self,
        user: "CurrentUser",
        request: Any,
        *,
        action: str,
        entity_type: str,
        entity_id: UUID | str | None = None,
        entity_label: str | None = None,
        changed_fields: list[str] | None = None,
        old_values: dict[str, Any] | None = None,
        new_values: dict[str, Any] | None = None,
        notes: str | None = None,
        outcome: str = "success",
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper that pulls audit context from request + user."""
        ctx: dict[str, Any] = {}
        try:
            client = getattr(request, "client", None)
            if client and getattr(client, "host", None):
                ctx["ip"] = client.host
            ua = request.headers.get("user-agent") if hasattr(request, "headers") else None
            if ua:
                ctx["user_agent"] = ua[:512]
        except Exception:
            pass
        if extra_context:
            ctx.update(extra_context)

        return await self.log(
            organization_id=user.organization_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=entity_label,
            actor_user_id=user.id,
            impersonator_user_id=user.impersonator_id,
            changed_fields=changed_fields,
            old_values=old_values,
            new_values=new_values,
            context=ctx,
            notes=notes,
            outcome=outcome,
        )

    async def list_for_org(
        self,
        organization_id: UUID,
        *,
        action: str | None = None,
        entity_type: str | None = None,
        actor_user_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Browse audit entries with optional filters.

        Returns ``{"entries": [...], "total_count": N, "page_size": L, "offset": O}``.
        """
        where = ["a.organization_id = $1"]
        params: list[Any] = [str(organization_id)]
        idx = 2
        if action:
            where.append(f"a.action = ${idx}")
            params.append(action)
            idx += 1
        if entity_type:
            where.append(f"a.entity_type = ${idx}")
            params.append(entity_type)
            idx += 1
        if actor_user_id:
            where.append(f"a.actor_user_id = ${idx}")
            params.append(str(actor_user_id))
            idx += 1
        where_sql = " AND ".join(where)

        list_q = f"""
            SELECT a.id, a.organization_id, a.actor_user_id, a.impersonator_user_id,
                   a.entity_type, a.entity_id, a.entity_label, a.action,
                   a.changed_fields, a.old_values, a.new_values,
                   a.context, a.notes, a.outcome, a.created_at,
                   au.display_name AS actor_display_name,
                   au.email        AS actor_email,
                   iu.display_name AS impersonator_display_name
            FROM admin_audit_log a
            LEFT JOIN users au ON au.id = a.actor_user_id
            LEFT JOIN users iu ON iu.id = a.impersonator_user_id
            WHERE {where_sql}
            ORDER BY a.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        count_q = f"SELECT COUNT(*) AS total FROM admin_audit_log a WHERE {where_sql}"

        async with scoped_acquire(organization_id=organization_id) as conn:
            total_row = await conn.fetchrow(count_q, *params)
            rows = await conn.fetch(list_q, *params, limit, offset)

        return {
            "entries": [dict(r) for r in rows],
            "total_count": (total_row["total"] if total_row else 0),
            "page_size": limit,
            "offset": offset,
        }

    async def list_actions(self, organization_id: UUID) -> list[str]:
        """Distinct action types currently present in the org's audit log."""
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT action FROM admin_audit_log "
                "WHERE organization_id = $1 ORDER BY action",
                str(organization_id),
            )
        return [r["action"] for r in rows]
