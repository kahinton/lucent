"""``python -m lucent.hyperadmin`` — manage instance-operator accounts.

A hyperadmin is the Lucent equivalent of a cloud provider's root operator: it
can see and act on *every* organization, which is what a signup site needs when
it provisions a tenant for a new customer. That power is granted deliberately,
never through the normal admin UI — the tenant-facing user routes refuse to
assign ``hyperadmin`` (see :func:`lucent.rbac.can_assign_role`), so the only way
to create one is to have shell access to the instance database.

This CLI is that path. It is intentionally the smallest possible surface:

    python -m lucent.hyperadmin list
    python -m lucent.hyperadmin grant --email ops@example.com
    python -m lucent.hyperadmin revoke --email ops@example.com
    python -m lucent.hyperadmin mint-key --email ops@example.com
    python -m lucent.hyperadmin mint-key --email ops@example.com --scope admin-orgs
    python -m lucent.hyperadmin revoke-key --key-id <uuid>
    python -m lucent.hyperadmin status

``mint-key`` exists because the hyperadmin API is authenticated by API key:
without a way to mint one for a brand-new operator you would have to log in to
the web UI first, which defeats the point of headless provisioning.

Every command that changes state writes an instance-level audit row, attributed
to the operator when one is being acted on, so promoting someone to instance
operator is itself auditable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any
from uuid import UUID

import asyncpg

from lucent.db import AdminAuditRepository, ApiKeyRepository, UserRepository
from lucent.db import admin_audit as audit_actions
from lucent.db.api_key import ADMIN_ORGS_SCOPE
from lucent.db.organization import OrganizationRepository
from lucent.db.pool import close_db, init_db, preauth_guc_preamble, tenant_guc_scrub
from lucent.logging import configure_logging, get_logger

logger = get_logger("lucent.hyperadmin")

#: Where an operator account lives when it is not a member of any tenant. The
#: internal system organization always exists (it stores instance secrets) and
#: is never listed as a tenant, which makes it the natural home for
#: infrastructure identities.
OPERATOR_ORG_FALLBACK = "__lucent_system__"

DEFAULT_KEY_NAME = "Instance operator key"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_NOT_FOUND = 2
EXIT_FAILED = 3


async def _resolve_user(
    pool: asyncpg.Pool, *, email: str | None, user_id: str | None
) -> dict[str, Any]:
    """Find a user by email or id, raising a clear error when absent."""
    user_repo = UserRepository(pool)

    if user_id:
        user = await user_repo.get_by_id(UUID(user_id))
        if user is None:
            raise LookupError(f"No user with id {user_id}")
        return user

    assert email is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, external_id, provider, organization_id, email, display_name,
                   avatar_url, provider_metadata, is_active, created_at, updated_at,
                   last_login_at, role
            FROM users
            WHERE LOWER(email) = LOWER($1)
            """,
            email,
        )
    if row is None:
        raise LookupError(f"No user with email {email}")
    return dict(row)


async def _operator_org_id(pool: asyncpg.Pool) -> UUID:
    """The organization that owns instance audit rows when no operator exists yet.

    Prefers the caller's own organization, else the internal system org, else
    any organization at all — an audit row has a NOT NULL organization_id and
    "we could not record that we did this" is not an acceptable outcome for a
    privilege change.
    """
    from lucent.auth import get_current_user

    current = get_current_user()
    if current and current.get("organization_id"):
        return UUID(str(current["organization_id"]))

    async with pool.acquire() as conn:
        found = await conn.fetchval(
            "SELECT id FROM organizations WHERE name = $1", OPERATOR_ORG_FALLBACK
        )
        if found:
            return found if isinstance(found, UUID) else UUID(str(found))
        found = await conn.fetchval("SELECT id FROM organizations ORDER BY created_at LIMIT 1")
    if not found:
        raise RuntimeError(
            "No organizations exist. Provision an instance first "
            "(python -m lucent.bootstrap)."
        )
    return found if isinstance(found, UUID) else UUID(str(found))


async def _audit(
    pool: asyncpg.Pool,
    *,
    action: str,
    entity_id: UUID | str | None,
    entity_label: str | None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    notes: str | None = None,
    entity_type: str = "user",
) -> None:
    """Record a privilege change in the instance audit trail."""
    org_id = await _operator_org_id(pool)
    await AdminAuditRepository(pool).log(
        organization_id=org_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        changed_fields=list((new_values or {}).keys()) or None,
        old_values=old_values,
        new_values=new_values,
        context={"instance_level": True, "actor_source": "cli:hyperadmin"},
        notes=notes,
    )


# --- Commands -------------------------------------------------------------


async def cmd_status(pool: asyncpg.Pool) -> int:
    """Report instance mode, organizations, and hyperadmin accounts."""
    from lucent.mode import get_mode
    from lucent.rbac import Role

    mode = get_mode()
    org_repo = OrganizationRepository(pool)
    orgs = await org_repo.list(exclude_names=[OPERATOR_ORG_FALLBACK], include_stats=True, limit=100)

    async with pool.acquire() as conn:
        operators = await conn.fetch(
            """
            SELECT u.id, u.email, u.display_name, u.organization_id, u.is_active,
                   o.name AS organization_name
            FROM users u
            LEFT JOIN organizations o ON o.id = u.organization_id
            WHERE u.role = $1
            ORDER BY u.created_at
            """,
            Role.HYPERADMIN.value,
        )

    out = sys.stdout
    out.write("\n=== Lucent instance ===\n")
    out.write(f"mode:            {mode.value} (reporting only)\n")
    out.write(f"organizations:   {orgs['total_count']} tenant(s)\n")
    for org in orgs["organizations"][:25]:
        out.write(
            f"  - {org['name']} [{org['status']}] "
            f"users={org.get('user_count', '?')} "
            f"admins={org.get('admin_count', '?')} "
            f"({org['id']})\n"
        )
    if orgs["total_count"] > 25:
        out.write(f"  ... and {orgs['total_count'] - 25} more\n")

    out.write(f"\nhyperadmins:     {len(operators)}\n")
    for row in operators:
        state = "active" if row["is_active"] else "inactive"
        out.write(
            f"  - {row['email'] or row['display_name'] or row['id']} [{state}] "
            f"org={row['organization_name'] or row['organization_id']}\n"
        )
    if not operators:
        out.write(
            "  (none) — grant one with: python -m lucent.hyperadmin grant --email ...\n"
        )
    out.write("========================\n")
    return EXIT_OK


async def cmd_list(pool: asyncpg.Pool) -> int:
    """List hyperadmin accounts."""
    return await cmd_status(pool)


async def cmd_grant(
    pool: asyncpg.Pool,
    *,
    email: str | None,
    user_id: str | None,
    create: bool,
    display_name: str | None,
    organization: str | None,
) -> int:
    """Promote an existing user to hyperadmin, or create the operator account."""
    user_repo = UserRepository(pool)
    created = False

    try:
        user = await _resolve_user(pool, email=email, user_id=user_id)
    except LookupError:
        if not create:
            logger.error(
                "No such user. Pass --create to provision the operator account."
            )
            return EXIT_NOT_FOUND
        if not email:
            logger.error("--create requires --email.")
            return EXIT_CONFIG
        user = await _create_operator_user(
            pool,
            email=email,
            display_name=display_name,
            organization=organization,
        )
        created = True

    previous_role = user.get("role", "member")
    if previous_role == "hyperadmin":
        # Distinguish "this command just created it" from "it already existed":
        # both land here because _create_operator_user stores role=hyperadmin,
        # and reporting a fresh create as "already a hyperadmin" is actively
        # confusing when you are trying to find out whether it worked.
        if created:
            sys.stdout.write(
                f"Created instance operator {user.get('email') or user['id']} "
                f"in organization {user.get('organization_id')}.\n"
                "Mint an API key with: python -m lucent.hyperadmin mint-key "
                f"--email {user.get('email') or '<id>'}\n"
            )
        else:
            sys.stdout.write(
                f"{user.get('email') or user['id']} is already a hyperadmin.\n"
            )
        return EXIT_OK

    promoted = await user_repo.update_role(user["id"], "hyperadmin")
    assert promoted is not None

    await _audit(
        pool,
        action="user.role_change",
        entity_id=user["id"],
        entity_label=promoted.get("email") or promoted.get("display_name"),
        old_values={"role": previous_role},
        new_values={"role": "hyperadmin"},
        notes="Instance-operator role granted via the hyperadmin CLI.",
    )

    sys.stdout.write(
        f"Granted hyperadmin to {promoted.get('email') or promoted['id']} "
        f"(was {previous_role}).\n"
        "Mint an API key with: python -m lucent.hyperadmin mint-key "
        f"--email {promoted.get('email') or '<id>'}\n"
    )
    return EXIT_OK


async def _create_operator_user(
    pool: asyncpg.Pool,
    *,
    email: str,
    display_name: str | None,
    organization: str | None,
) -> dict[str, Any]:
    """Create a hyperadmin account that belongs to no customer tenant.

    An operator needs *some* organization because the audit tables and the
    scoped connection helpers require one. Unless ``--organization`` names a
    tenant deliberately, the internal system organization is used so the
    operator never appears as a member of a customer's workspace.
    """
    org_repo = OrganizationRepository(pool)

    target_org: dict[str, Any] | None = None
    if organization:
        target_org = await org_repo.get_by_name(organization)
        if target_org is None:
            raise LookupError(f"No organization named {organization}")
    else:
        target_org = await org_repo.get_by_name(OPERATOR_ORG_FALLBACK)
        if target_org is None:
            # The system org is normally created by instance provisioning. If it
            # is missing, create it rather than dumping the operator into a
            # customer tenant.
            from lucent.builtin_definitions import SYSTEM_ORG_NAME

            target_org = await org_repo.get_by_name(SYSTEM_ORG_NAME) or await org_repo.create(
                OPERATOR_ORG_FALLBACK
            )

    user_repo = UserRepository(pool)
    user = await user_repo.create(
        external_id=email.lower(),
        provider="local",
        organization_id=target_org["id"],
        email=email,
        display_name=display_name or email,
        role="hyperadmin",
    )
    logger.info("Created instance operator %s in organization %s", email, target_org["name"])
    return user


async def cmd_revoke(
    pool: asyncpg.Pool,
    *,
    email: str | None,
    user_id: str | None,
    to_role: str,
) -> int:
    """Demote a hyperadmin back to a tenant role."""
    user_repo = UserRepository(pool)
    try:
        user = await _resolve_user(pool, email=email, user_id=user_id)
    except LookupError as exc:
        logger.error("%s", exc)
        return EXIT_NOT_FOUND

    if user.get("role") != "hyperadmin":
        sys.stdout.write(f"{user.get('email') or user['id']} is not a hyperadmin.\n")
        return EXIT_OK

    if to_role not in ("member", "admin", "owner"):
        logger.error("--to must be one of: member, admin, owner")
        return EXIT_CONFIG

    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE role = 'hyperadmin' AND is_active"
        )
    if remaining is not None and int(remaining) <= 1:
        logger.error(
            "Refusing to revoke the last hyperadmin — the instance would have no "
            "operator able to create or manage organizations. Grant another first."
        )
        return EXIT_FAILED

    demoted = await user_repo.update_role(user["id"], to_role)
    assert demoted is not None

    await _audit(
        pool,
        action="user.role_change",
        entity_id=user["id"],
        entity_label=demoted.get("email") or demoted.get("display_name"),
        old_values={"role": "hyperadmin"},
        new_values={"role": to_role},
        notes="Instance-operator role revoked via the hyperadmin CLI.",
    )

    sys.stdout.write(
        f"Revoked hyperadmin from {demoted.get('email') or demoted['id']} "
        f"(now {to_role}).\n"
    )
    return EXIT_OK


async def cmd_mint_key(
    pool: asyncpg.Pool,
    *,
    email: str | None,
    user_id: str | None,
    name: str,
    scopes: list[str] | None = None,
) -> int:
    """Mint an API key for a hyperadmin so it can call the instance APIs.

    ``scopes`` defaults to the repository's ``['read', 'write']`` (full access,
    what the ops console uses). Passing ``[ADMIN_ORGS_SCOPE]`` mints the
    least-privilege control-plane key: the admin organization endpoints and
    nothing else.
    """
    try:
        user = await _resolve_user(pool, email=email, user_id=user_id)
    except LookupError as exc:
        logger.error("%s", exc)
        return EXIT_NOT_FOUND

    if user.get("role") != "hyperadmin":
        logger.error(
            "%s is not a hyperadmin. Grant the role first.",
            user.get("email") or user["id"],
        )
        return EXIT_FAILED
    if not user.get("organization_id"):
        logger.error("The operator account has no organization; it cannot hold an API key.")
        return EXIT_FAILED

    record, plaintext = await ApiKeyRepository(pool).create(
        user_id=user["id"],
        organization_id=user["organization_id"],
        name=name,
        scopes=scopes,
    )

    await _audit(
        pool,
        action=audit_actions.API_KEY_CREATE,
        entity_id=record["id"],
        entity_label=name,
        new_values={
            "name": name,
            "user": str(user["id"]),
            "scopes": record["scopes"],
        },
        notes="Instance-operator API key minted via the hyperadmin CLI.",
    )

    if record["scopes"] == [ADMIN_ORGS_SCOPE]:
        reach = (
            "least-privilege — only the organization administration surface "
            "(POST /api/organizations/provision and its read/settings reach)"
        )
    else:
        reach = (
            "full access — the tenant data plane and the instance APIs "
            "(mint with --scope admin-orgs for a control-plane key)"
        )

    sys.stdout.write(
        "\nInstance operator API key (shown once — store it now):\n"
        f"  {plaintext}\n\n"
        f"Scopes: {', '.join(record['scopes'])}\n"
        f"Reach:  {reach}\n\n"
        f"Use it as: Authorization: Bearer <key>\n"
    )
    return EXIT_OK


async def cmd_revoke_key(pool: asyncpg.Pool, *, key_id: str) -> int:
    """Revoke an API key by id, whatever user or scope it belongs to.

    Machine credentials are minted headlessly, so they must also be
    terminable headlessly — revoking through the web UI would require the
    operator to have an interactive session.
    """
    try:
        key_uuid = UUID(key_id)
    except (TypeError, ValueError):
        logger.error("--key-id must be a UUID")
        return EXIT_CONFIG

    # ``api_keys`` is read under the same narrow pre-auth branch as bearer
    # verification: this lookup happens before any user/org context exists.
    async with pool.acquire() as conn:
        await conn.execute(preauth_guc_preamble())
        try:
            row = await conn.fetchrow(
                "SELECT name, scopes, revoked_at FROM api_keys WHERE id = $1",
                key_uuid,
            )
        finally:
            await conn.execute(tenant_guc_scrub())

    if row is None:
        logger.error("No API key with id %s", key_id)
        return EXIT_NOT_FOUND
    if row["revoked_at"] is not None:
        sys.stdout.write(f"API key {key_id} is already revoked.\n")
        return EXIT_OK

    await ApiKeyRepository(pool).revoke_by_id(key_uuid)

    await _audit(
        pool,
        action=audit_actions.API_KEY_REVOKE,
        entity_id=key_uuid,
        entity_label=row["name"],
        old_values={"revoked": False},
        new_values={"revoked": True, "scopes": row["scopes"]},
        notes="API key revoked via the hyperadmin CLI.",
        entity_type="api_key",
    )

    sys.stdout.write(f"Revoked API key {key_id} ({row['name']}).\n")
    return EXIT_OK


# --- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lucent.hyperadmin",
        description=(
            "Manage Lucent instance-operator (hyperadmin) accounts. These "
            "commands need direct database access and are the only way to "
            "create a hyperadmin."
        ),
    )
    parser.add_argument("--database-url", default=None, help="Defaults to $DATABASE_URL.")
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Do not apply pending migrations before running (faster, riskier).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show instance mode, tenants, and operators.")
    sub.add_parser("list", help="Alias for status.")

    grant = sub.add_parser("grant", help="Promote (or create) an instance operator.")
    grant.add_argument("--email")
    grant.add_argument("--user-id")
    grant.add_argument(
        "--create",
        action="store_true",
        help="Create the account if it does not exist (no password; use the web UI reset flow).",
    )
    grant.add_argument("--display-name")
    grant.add_argument(
        "--organization",
        help=(
            "Organization to place a newly created operator in. Defaults to the "
            "internal system organization so the operator is not a member of a "
            "customer tenant."
        ),
    )

    revoke = sub.add_parser("revoke", help="Demote an instance operator.")
    revoke.add_argument("--email")
    revoke.add_argument("--user-id")
    revoke.add_argument("--to", default="member", help="member, admin, or owner")

    mint = sub.add_parser("mint-key", help="Mint an API key for an instance operator.")
    mint.add_argument("--email")
    mint.add_argument("--user-id")
    mint.add_argument("--name", default=DEFAULT_KEY_NAME)
    mint.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        choices=(ADMIN_ORGS_SCOPE,),
        metavar="SCOPE",
        help=(
            "Restrict the key to a capability instead of granting full access. "
            f"Supported: {ADMIN_ORGS_SCOPE} — the instance admin organization "
            "endpoints only. Omit for a full read/write key (ops console)."
        ),
    )

    revoke_key = sub.add_parser(
        "revoke-key", help="Revoke an API key by id (any user or scope)."
    )
    revoke_key.add_argument("--key-id", required=True, help="The api_keys UUID.")

    return parser


async def _run(args: argparse.Namespace) -> int:
    # Validate the command line before touching the database: a missing
    # --email should fail instantly with a usage error, not attempt a
    # connection (and not require a reachable database to report it).
    if args.command in ("grant", "revoke", "mint-key") and not (
        args.email or args.user_id
    ):
        logger.error("%s requires --email or --user-id", args.command)
        return EXIT_CONFIG

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required (or pass --database-url)")
        return EXIT_CONFIG

    if args.skip_migrations:
        pool = await asyncpg.create_pool(database_url)
    else:
        pool = await init_db(database_url)

    try:
        if args.command in ("status", "list"):
            return await cmd_status(pool)
        if args.command == "grant":
            return await cmd_grant(
                pool,
                email=args.email,
                user_id=args.user_id,
                create=args.create,
                display_name=args.display_name,
                organization=args.organization,
            )
        if args.command == "revoke":
            return await cmd_revoke(
                pool, email=args.email, user_id=args.user_id, to_role=args.to
            )
        if args.command == "mint-key":
            return await cmd_mint_key(
                pool,
                email=args.email,
                user_id=args.user_id,
                name=args.name,
                scopes=args.scopes,
            )
        if args.command == "revoke-key":
            return await cmd_revoke_key(pool, key_id=args.key_id)
        logger.error("Unknown command: %s", args.command)
        return EXIT_CONFIG
    finally:
        if args.skip_migrations:
            await pool.close()
        else:
            await close_db()


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except LookupError as exc:
        logger.error("%s", exc)
        return EXIT_NOT_FOUND
    except Exception as exc:  # noqa: BLE001 — surface *why* to the operator
        logger.error("hyperadmin command failed: %s", exc, exc_info=True)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
