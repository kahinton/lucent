"""The single provisioning path from an asserted identity to a Lucent account.

Three separate features — SSO federation, invitation acceptance, and self-serve
signup — all need the same dangerous decision: *given an email address somebody
else vouched for, which Lucent account is this, and what may it do?* Getting
that wrong once is an account takeover, so it lives here and only here rather
than being re-implemented at each entry point.

The rules this module enforces, in order:

1. **Never adopt a daemon identity.** Service accounts have synthetic addresses
   and no password; a login flow that matched one would hand a human a
   machine's role.
2. **Never cross a tenant boundary.** Every lookup is scoped to the
   organization the *connection or invitation* names, never to whatever the
   IdP happens to say. Otherwise a user with an account in tenant A could sign
   in through tenant B's IdP and land in A.
3. **Email-based linking requires a verified address.** A password account's
   email was typed by an admin or by the user themselves — it was never proven.
   Federating onto it because the IdP happened to send the same string would
   let the IdP (or anyone who controls a mailbox at that address) assume an
   existing identity. ``email_verified`` on the target row plus the connection's
   ``allow_email_linking`` flag are both required.
4. **A federated identity can never become an instance operator.** ``hyperadmin``
   is granted only by the out-of-band CLI, on purpose: an IdP compromise must
   not be an instance compromise.
5. **JIT-created accounts start at the configured role**, default ``member``,
   and are marked so an auditor can tell how they came to exist.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

import asyncpg

from lucent.db import SsoConnectionRepository, UserRepository
from lucent.logging import get_logger

logger = get_logger("identity.provisioning")

#: Roles a federated identity may ever hold. ``hyperadmin`` is deliberately
#: absent — see rule 4 above.
FEDERATED_ROLES = frozenset({"member", "admin", "owner"})


class FederatedIdentityError(Exception):
    """The assertion is valid but this account may not be signed in.

    Distinct from ``None`` (bad credentials) so the caller can log the reason
    for an operator without telling the visitor anything beyond "denied".
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AssertedIdentity:
    """What an identity provider asserted about a person.

    Only fields the IdP actually vouched for belong here. Nothing derived from
    a request parameter may be added to this structure — the whole point is
    that it is the trusted half of the login.
    """

    #: IdP-side stable subject: OIDC ``sub``, SAML NameID, GitHub user id.
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None = None
    avatar_url: str | None = None
    #: Group/role claims already extracted through the connection's mappings.
    groups: tuple[str, ...] = ()
    raw: dict[str, Any] | None = None


async def resolve_federated_user(
    pool: asyncpg.Pool,
    *,
    connection: dict[str, Any],
    identity: AssertedIdentity,
) -> dict[str, Any]:
    """Find or create the Lucent account for an asserted identity.

    Returns the user row to sign in. Raises :class:`FederatedIdentityError`
    with a logged reason when the identity may not be admitted.

    Order of resolution matters and is deliberate:

    1. Match on ``(provider, external_id)`` — the stable IdP subject. This is
       the identity binding and it wins over email, because a person can change
       their mailbox but not (usually) their subject.
    2. Fall back to email within the organization, but only for a *verified*
       address on both sides and only when the connection permits linking.
    3. Otherwise create the account, if JIT provisioning is enabled.
    """

    organization_id = connection["organization_id"]
    provider = "oidc" if connection["provider_type"] == "github" else connection["provider_type"]
    user_repo = UserRepository(pool)
    connection_repo = SsoConnectionRepository(pool)

    # ── 1. Existing federated binding ───────────────────────────────────
    existing = await user_repo.get_by_external_id(identity.subject, provider)
    if existing is not None:
        return await _admit_existing(
            pool,
            user=existing,
            connection=connection,
            identity=identity,
        )

    # ── 2. Email-based linking onto an existing account ─────────────────
    email = (identity.email or "").strip().lower()
    if email:
        candidate = await user_repo.get_by_email_in_organization(email, organization_id)
        if candidate is not None:
            if candidate.get("role") == "daemon":
                raise FederatedIdentityError(
                    "Refusing to federate onto a daemon service identity"
                )
            if not connection.get("allow_email_linking", True):
                raise FederatedIdentityError(
                    "This email matches an existing account, but this organization "
                    "does not allow linking accounts by email. Sign in with your "
                    "password, or ask an administrator to link the account."
                )
            if not identity.email_verified:
                raise FederatedIdentityError(
                    "The identity provider did not verify this email address, so it "
                    "cannot be linked to an existing account."
                )
            if not candidate.get("email_verified"):
                raise FederatedIdentityError(
                    "This email matches an existing account that has not been "
                    "verified. Sign in with your password first, then link SSO from "
                    "your account settings."
                )

            linked = await user_repo.link_sso_identity(
                candidate["id"],
                provider=provider,
                external_id=identity.subject,
                provider_metadata=_provider_metadata(connection, identity),
                email_verified=True,
            )
            logger.info(
                "Linked federated identity %s/%s to existing user %s in org %s",
                provider,
                identity.subject,
                candidate["id"],
                organization_id,
            )
            return await _admit_existing(
                pool,
                user=linked or candidate,
                connection=connection,
                identity=identity,
            )

    # ── 3. Just-in-time account creation ───────────────────────────────
    if not connection.get("auto_create_users", True):
        raise FederatedIdentityError(
            "No account exists for this identity and automatic account creation "
            "is disabled for this connection. Ask an administrator to invite you."
        )
    if not email:
        raise FederatedIdentityError(
            "The identity provider did not supply an email address, which is "
            "required to create an account."
        )

    role = _resolve_role(connection, identity)
    if role not in FEDERATED_ROLES:
        role = "member"

    created = await user_repo.create(
        external_id=identity.subject,
        provider=provider,
        organization_id=organization_id,
        email=email,
        display_name=identity.display_name or email,
        avatar_url=identity.avatar_url,
        provider_metadata=_provider_metadata(connection, identity),
        role=role,
        email_verified=identity.email_verified,
        provisioned_by="sso",
    )
    logger.info(
        "JIT-provisioned %s user %s in org %s as %s",
        provider,
        created["id"],
        organization_id,
        role,
    )
    # The connection row is the authority for defaults; re-read nothing else.
    _ = connection_repo
    return created


async def _admit_existing(
    pool: asyncpg.Pool,
    *,
    user: dict[str, Any],
    connection: dict[str, Any],
    identity: AssertedIdentity,
) -> dict[str, Any]:
    """Apply the admission checks that a returning federated user still faces."""
    organization_id = connection["organization_id"]

    if str(user.get("organization_id")) != str(organization_id):
        # The identity is bound to a user in a different tenant. Signing them in
        # here would cross tenants, so refuse rather than "helpfully" moving them.
        raise FederatedIdentityError(
            "This identity is already associated with a different organization."
        )
    if user.get("role") == "daemon":
        raise FederatedIdentityError("Refusing to sign a daemon identity in as a human")
    if not user.get("is_active", True):
        raise FederatedIdentityError("This account has been deactivated.")

    # Refresh display data and verification state from the IdP, which is
    # authoritative for them. Role is NOT refreshed here: role changes are a
    # deliberate administrative act, and letting an IdP group claim demote or
    # promote a Lucent admin on every login makes the two systems fight.
    if identity.email_verified and not user.get("email_verified"):
        await UserRepository(pool).set_email_verified(user["id"], True)
        user["email_verified"] = True

    return user


def _resolve_role(connection: dict[str, Any], identity: AssertedIdentity) -> str:
    from lucent.identity.sso_config import role_from_mappings

    return role_from_mappings(
        claim_values=list(identity.groups),
        role_mappings=connection.get("role_mappings") or [],
        default_role=connection.get("default_role") or "member",
    )


def _provider_metadata(
    connection: dict[str, Any], identity: AssertedIdentity
) -> dict[str, Any]:
    """Non-secret provenance recorded on the user row for support and audit."""
    metadata: dict[str, Any] = {
        "sso_connection_id": str(connection["id"]),
        "sso_provider_type": connection["provider_type"],
        "sso_slug": connection["slug"],
    }
    if identity.groups:
        metadata["sso_groups"] = list(identity.groups)
    return metadata


async def accept_invitation(
    pool: asyncpg.Pool,
    *,
    invitation: dict[str, Any],
    email: str,
    display_name: str | None,
    password: str | None,
) -> dict[str, Any]:
    """Create (or adopt) the account an invitation names, then consume the link.

    The invitation's email is authoritative: the account is created for the
    mailbox the link was sent to, not for whatever the signup form typed. An
    invitation is addressed to a person, and allowing a different address to
    redeem it turns any forwarded link into an account-creation primitive.

    Password is optional so the same flow serves both "set a password now" and
    "I will sign in with SSO" cases. When omitted the account has no password
    and can only be accessed through an identity provider.
    """
    from lucent.auth_providers import set_user_password, validate_password_complexity
    from lucent.db.identity import InvitationRepository

    organization_id = invitation["organization_id"]
    invited_email = (invitation["email"] or "").strip().lower()
    presented = (email or "").strip().lower()

    if presented and presented != invited_email:
        raise FederatedIdentityError(
            "This invitation was issued for a different email address."
        )

    role = invitation["role"]
    if role not in ("member", "admin", "owner"):
        role = "member"

    user_repo = UserRepository(pool)
    existing = await user_repo.get_by_email_in_organization(invited_email, organization_id)
    if existing is not None:
        # The mailbox already has an account in this tenant. Do not mint a
        # second one; treat acceptance as "welcome back" and let the caller
        # sign them in. The invitation is still consumed so the link cannot be
        # reused later to create a shadow account.
        if existing.get("role") == "daemon":
            raise FederatedIdentityError("Refusing to redeem an invitation as a daemon")
        await InvitationRepository(pool).claim(invitation["id"], accepted_by=existing["id"])
        return existing

    if password is not None:
        complexity_error = validate_password_complexity(password)
        if complexity_error:
            raise FederatedIdentityError(complexity_error)

    local_external_id = f"local_{secrets.token_hex(8)}"
    user = await user_repo.create(
        external_id=local_external_id,
        provider="local",
        organization_id=organization_id,
        email=invited_email,
        display_name=display_name or invited_email,
        role=role,
        # Accepting an invitation proves control of the mailbox the link was
        # delivered to, but the link itself was the only proof and it was
        # handed over out of band. Treat it as verified for linking purposes
        # only when the inviter is inside the tenant, which is always the case.
        email_verified=True,
        provisioned_by="invitation",
    )

    if password is not None:
        await set_user_password(pool, user["id"], password, force_change=False)

    claimed = await InvitationRepository(pool).claim(
        invitation["id"], accepted_by=user["id"]
    )
    if not claimed:
        # Lost the race with a concurrent redemption. Undo the creation: an
        # account must never exist without the invitation that authorised it.
        await user_repo.delete(user["id"])
        raise FederatedIdentityError("This invitation has already been used.")

    logger.info(
        "Invitation %s accepted by user %s in org %s",
        invitation["id"],
        user["id"],
        organization_id,
    )
    return user
