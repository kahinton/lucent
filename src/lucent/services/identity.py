"""Identity services — invitations, signup, and SSO orchestration.

The route handlers stay thin; this module owns the sequences that must not be
reordered or partially applied.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.identity import (
    InvitationRepository,
    generate_invitation_token,
    hash_invitation_token,
    invitation_expiry,
)
from lucent.db.user import UserRepository
from lucent.identity.provisioning import FederatedIdentityError
from lucent.logging import get_logger

logger = get_logger("services.identity")

#: Product default for an invitation link.
DEFAULT_INVITATION_TTL_HOURS = 24


class InvitationError(Exception):
    """An invitation could not be created or redeemed."""


async def create_invitation(
    pool: asyncpg.Pool,
    *,
    organization_id: UUID | str,
    email: str,
    role: str,
    invited_by: UUID | str,
    inviter_role: str,
    ttl_hours: int = DEFAULT_INVITATION_TTL_HOURS,
    note: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Mint an invitation and return ``(record, plaintext_token)``.

    Guards, in order of intent rather than execution:

    * The inviter may not grant a role above their own. Otherwise an admin
      could invite an owner, which is a privilege escalation that bypasses
      ``can_assign_role`` entirely.
    * ``hyperadmin`` is never invitable — it is an out-of-band grant.
    * An existing live invitation for the same mailbox is rejected rather than
      superseded, so there is exactly one usable link at a time and no doubt
      about which one an auditor should look at.
    * An existing account for that mailbox is rejected: inviting somebody who
      already has an account produces a confusing "welcome back" experience and
      wastes a link.
    """
    from lucent.rbac import Role, can_assign_role

    normalized_email = (email or "").strip().lower()
    if "@" not in normalized_email:
        raise InvitationError("A valid email address is required.")

    if role not in ("member", "admin", "owner"):
        raise InvitationError("Role must be one of: member, admin, owner.")

    if not can_assign_role(inviter_role, role):
        raise InvitationError("You cannot invite someone with a role above your own.")

    # Explicitly refuse the instance-operator role even if can_assign_role were
    # ever relaxed: an invitation must never confer it.
    if role == Role.HYPERADMIN.value:
        raise InvitationError("Instance operator access cannot be granted by invitation.")

    ttl = int(ttl_hours)
    if ttl < 1 or ttl > 720:
        raise InvitationError("Invitation lifetime must be between 1 and 720 hours.")

    user_repo = UserRepository(pool)
    existing_user = await user_repo.get_by_email_in_organization(
        normalized_email, organization_id
    )
    if existing_user is not None:
        raise InvitationError("That email address already belongs to a member.")

    repo = InvitationRepository(pool)
    pending = await repo.find_pending_for_email(organization_id, normalized_email)
    if pending is not None:
        raise InvitationError(
            "There is already a pending invitation for that email address. "
            "Revoke it before creating a new one."
        )

    token = generate_invitation_token()
    record = await repo.create(
        organization_id=organization_id,
        email=normalized_email,
        role=role,
        token_hash=hash_invitation_token(token),
        invited_by=invited_by,
        expires_at=invitation_expiry(ttl),
        note=note,
    )
    logger.info(
        "Invitation %s created for %s in org %s by %s (role=%s, ttl=%sh)",
        record["id"],
        normalized_email,
        organization_id,
        invited_by,
        role,
        ttl,
    )
    return record, token


async def resolve_invitation(pool: asyncpg.Pool, token: str) -> dict[str, Any] | None:
    """Look up a live invitation from a presented token."""
    if not token or not token.startswith("li_"):
        return None
    return await InvitationRepository(pool).get_pending_by_token(
        hash_invitation_token(token)
    )


async def redeem_invitation(
    pool: asyncpg.Pool,
    *,
    token: str,
    display_name: str | None,
    password: str | None,
) -> dict[str, Any]:
    """Accept an invitation and return the created (or adopted) user.

    The token is looked up again here rather than trusted from the rendered
    page, so the form cannot be replayed with a token the server already
    consumed between GET and POST.
    """
    from lucent.identity.provisioning import accept_invitation

    invitation = await resolve_invitation(pool, token)
    if invitation is None:
        raise InvitationError(
            "This invitation link is invalid, expired, or has already been used."
        )

    if invitation.get("organization_status") != "active":
        raise InvitationError(
            "The organization this invitation belongs to is not active."
        )

    try:
        user = await accept_invitation(
            pool,
            invitation=invitation,
            email=invitation["email"],
            display_name=display_name,
            password=password,
        )
    except FederatedIdentityError as exc:
        raise InvitationError(str(exc)) from exc

    return user


async def ensure_org_has_owner(
    pool: asyncpg.Pool, organization_id: UUID | str
) -> bool:
    """Whether the tenant has at least one active owner.

    Used before allowing a self-serve signup to create a tenant, because a
    tenant created without an owner is unadministrable from the web UI.
    """
    users = await UserRepository(pool).get_by_organization(UUID(str(organization_id)))
    return any(user.get("role") == "owner" and user.get("is_active") for user in users)
