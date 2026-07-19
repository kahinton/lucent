"""Authoritative daemon-service identity resolution.

The daemon, sandbox manager, and organization provisioning must agree that a
real organization is served by ``daemon-service:{organization_id}``. This
module owns the self-healing lookup without invoking user-facing repository
side effects such as individual-memory creation.
"""

from __future__ import annotations

from typing import Any

from lucent.builtin_definitions import SYSTEM_ORG_NAME


async def ensure_daemon_service_user(conn, organization_id: str) -> dict[str, Any]:
    """Return the active daemon principal for an organization, creating it if absent."""
    external_id = f"daemon-service:{organization_id}"
    row = await conn.fetchrow(
        "SELECT id, organization_id FROM users "
        "WHERE external_id = $1 AND provider = 'local' AND is_active = true",
        external_id,
    )
    if row:
        return dict(row)

    row = await conn.fetchrow(
        "INSERT INTO users "
        "  (external_id, provider, organization_id, email, display_name, role) "
        "VALUES ($1, 'local', $2::uuid, 'daemon@lucent.local', "
        "  'Lucent Daemon', 'daemon') "
        "ON CONFLICT (provider, external_id) DO UPDATE "
        "  SET organization_id = EXCLUDED.organization_id "
        "RETURNING id, organization_id",
        external_id,
        organization_id,
    )
    if not row:
        raise RuntimeError(f"Unable to provision daemon service user for {organization_id}")
    return dict(row)


async def resolve_daemon_service_user(
    conn,
    organization_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a real organization and return its daemon-service principal.

    The optional fallback exists for legacy sandbox callers. New daemon flows
    always supply an explicit organization and never bind to the hidden system
    organization.
    """
    resolved_org_id = organization_id
    if not resolved_org_id:
        row = await conn.fetchrow(
            "SELECT id FROM organizations WHERE name <> $1 ORDER BY created_at LIMIT 1",
            SYSTEM_ORG_NAME,
        )
        if not row:
            raise RuntimeError("No real organization available for daemon service identity")
        resolved_org_id = str(row["id"])
    return await ensure_daemon_service_user(conn, resolved_org_id)
