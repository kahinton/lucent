"""Non-interactive, idempotent first-run bootstrap for Lucent.

First run used to be browser-only. ``POST /setup`` was the sole caller of the
first-user path, it needed CSRF + a password + a model checkbox, and migration
088 disables every seed model — so on a fresh database with no provider
credentials setup could not be completed at all. A Terraform/CI provisioner
cannot fill in a web form.

This module is the reusable bootstrap that both ``/setup`` and a non-interactive
caller use:

- :func:`list_initial_setup_models` / :func:`enable_initial_setup_models` —
  the model-discovery + enable steps ``/setup`` runs (moved out of the route so
  there is exactly one implementation).
- :func:`provision_instance` — create (or adopt) the organisation, create the
  first owner user, ensure at least one model is *enabled and accessible*, and
  mint an initial API key. Safe to run twice.
- :func:`run_startup_bootstrap` — the env-driven container path. Opt-in, and
  guarded so it can never resurrect or reset an already-provisioned instance.

The one trap worth calling out: an *enabled* model is not a *usable* model.
Usage is clearance-driven and default-deny (migration 131), and the usage query
only considers org-scoped rows (``models.organization_id = <org>``). The seed
models shipped by migration 031 are global registry rows
(``organization_id IS NULL``) and disabled. So enabling a seed the way
``/setup`` does produces a model the org still cannot use. :func:`ensure_usable_model`
therefore adopts the chosen seed into the organisation first (exactly the shape
provider discovery writes), then enables it via the same repository call
``/setup`` uses and grants organisation read clearance via the same
``AuthAccessRepository`` call the admin models page uses, so clearances and
settings stay consistent with the UI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool

from lucent.db.models import ModelRepository
from lucent.logging import configure_logging, get_logger

logger = get_logger("lucent.bootstrap")

# --- Environment contract -------------------------------------------------

STARTUP_FLAG_ENV = "LUCENT_BOOTSTRAP_ON_STARTUP"
DISPLAY_NAME_ENV = "LUCENT_BOOTSTRAP_DISPLAY_NAME"
EMAIL_ENV = "LUCENT_BOOTSTRAP_EMAIL"
PASSWORD_ENV = "LUCENT_BOOTSTRAP_PASSWORD"
ORG_NAME_ENV = "LUCENT_BOOTSTRAP_ORG_NAME"
MODEL_ENV = "LUCENT_BOOTSTRAP_MODEL"
API_KEY_FILE_ENV = "LUCENT_BOOTSTRAP_API_KEY_FILE"

DEFAULT_DISPLAY_NAME = "Administrator"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Seed models (migration 031) tried in order when no model is specified. They
#: need no provider credentials to be *enabled and accessible*; whether an
#: inference call succeeds still depends on the operator configuring a provider.
PREFERRED_FALLBACK_MODELS = (
    "claude-sonnet-4.6",
    "gpt-5.1",
    "gemini-2.5-pro",
)


class BootstrapError(RuntimeError):
    """Raised when the bootstrap cannot complete a required step."""


@dataclass
class ProvisionResult:
    """What a bootstrap run did (never contains the raw API key)."""

    dry_run: bool = False
    created_org: bool = False
    created_user: bool = False
    organization: dict[str, Any] | None = None
    user: dict[str, Any] | None = None
    api_key: str | None = None
    model: "ModelSetup | None" = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ModelSetup:
    """Outcome of :func:`ensure_usable_model` for one model."""

    model_id: str
    adopted: bool = False  # a global registry row re-homed into the org
    enabled: bool = False
    granted: bool = False


# --- Model selection / enabling (shared with POST /setup) -----------------


async def list_initial_setup_models(pool) -> list[dict]:
    """List setup choices, retrying discovery when the catalog is empty.

    Moved verbatim from ``web/routes/auth.py`` so ``/setup`` and the CLI share
    one implementation; ``ModelRepository`` / ``ModelDiscoveryService`` are
    resolved at call time so callers can patch them.
    """
    repo = ModelRepository(pool)
    models = await repo.list_initial_setup_models()
    if models:
        return models

    try:
        from lucent.model_discovery import ModelDiscoveryService

        await ModelDiscoveryService(pool).sync()
    except Exception:
        logger.warning("Model discovery failed while rendering initial setup", exc_info=True)
    return await repo.list_initial_setup_models()


async def enable_initial_setup_models(pool, selected_model_ids: list[str]) -> str | None:
    """Validate and enable the models explicitly selected during setup.

    Returns ``None`` on success or a user-facing error string — the contract
    ``POST /setup`` renders back into the form.
    """
    selected_ids = set(selected_model_ids)
    if not selected_ids:
        return "Select at least one model to continue."

    available_models = await list_initial_setup_models(pool)
    available_ids = {model["id"] for model in available_models}
    if not selected_ids.issubset(available_ids):
        return "One or more selected models are no longer available. Refresh and try again."

    enabled_ids = await enable_models(pool, sorted(selected_ids))
    if not selected_ids.issubset(enabled_ids):
        return "The selected models could not be enabled. Refresh and try again."
    return None


async def enable_models(pool, model_ids: list[str]) -> set[str]:
    """Enable model rows and refresh the in-process registry.

    The single enable primitive: ``/setup`` and :func:`ensure_usable_model`
    both route through it, so ``is_enabled`` and the runtime registry never
    drift apart.
    """
    enabled_ids = await ModelRepository(pool).enable_models(sorted(set(model_ids)))
    from lucent.model_registry import load_models_from_db

    await load_models_from_db(pool)
    return enabled_ids


# --- Credential-less usable model -----------------------------------------


async def _org_usable_models(pool: Pool, org_id: str) -> list[dict]:
    """Enabled, org-scoped models that carry an organisation read clearance."""
    rows = await pool.fetch(
        """
        SELECT m.id, m.provider
        FROM models m
        JOIN auth_clearances acl
          ON acl.auth_id = m.auth_id
         AND acl.role = 'read'
         AND acl.principal_type = 'org'
         AND acl.principal_id = $1
        WHERE m.organization_id = $1
          AND m.is_enabled = true
        ORDER BY m.provider, m.id
        """,
        UUID(str(org_id)),
    )
    return [dict(row) for row in rows]


async def _pick_model_id(pool: Pool, preferred: str | None) -> str | None:
    """Choose a model id to enable: explicit, else preferred seed, else any."""
    if preferred:
        exists = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM models WHERE id = $1)", preferred
        )
        if exists:
            return preferred
        logger.warning("Requested bootstrap model %r is not in the registry", preferred)
    for candidate in PREFERRED_FALLBACK_MODELS:
        exists = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM models WHERE id = $1)", candidate
        )
        if exists:
            return candidate
    return await pool.fetchval(
        "SELECT id FROM models ORDER BY is_enabled DESC, category, id LIMIT 1"
    )


async def ensure_usable_model(
    pool: Pool,
    *,
    org_id: str,
    granted_by: str,
    model_id: str | None = None,
    dry_run: bool = False,
) -> ModelSetup:
    """Guarantee at least one model in the org is enabled *and* accessible.

    Idempotent: if the org already has an enabled, org-cleared model this is a
    no-op unless an explicit ``model_id`` is given.
    """
    org_uuid = UUID(str(org_id))
    existing = await _org_usable_models(pool, str(org_id))
    existing_ids = {row["id"] for row in existing}

    if model_id is None and existing:
        chosen = existing[0]["id"]
        return ModelSetup(chosen, adopted=False, enabled=True, granted=True)

    target = model_id or await _pick_model_id(pool, os.environ.get(MODEL_ENV))
    if not target:
        raise BootstrapError("No models exist in the registry; cannot enable one.")

    row = await pool.fetchrow(
        "SELECT id, organization_id, is_enabled FROM models WHERE id = $1", target
    )
    if row is None:
        raise BootstrapError(f"Model {target!r} is not in the registry.")

    needs_adoption = row["organization_id"] is None
    setup = ModelSetup(
        model_id=target,
        adopted=needs_adoption,
        enabled=bool(row["is_enabled"]),
        granted=target in existing_ids,
    )

    if dry_run:
        setup.enabled = True
        setup.granted = True
        return setup

    if needs_adoption:
        # A global registry row is invisible to the clearance-driven usage
        # query and cannot be granted to an org (grants require an org-scoped
        # row). Promote it to this org — the same shape provider discovery
        # writes for a discovered model.
        await pool.execute(
            "UPDATE models SET organization_id = $1, updated_at = NOW() "
            "WHERE id = $2 AND organization_id IS NULL",
            org_uuid,
            target,
        )
        logger.warning(
            "Adopted global seed model %s into organization %s so it can be "
            "enabled and granted (it becomes org-scoped)",
            target,
            org_id,
        )

    await enable_models(pool, [target])
    setup.enabled = True

    from lucent.db.access_control import AuthAccessRepository

    await AuthAccessRepository(pool).upsert_grant(
        "model",
        target,
        grantee_type="org",
        grantee_id=None,
        role="read",
        granted_by=UUID(str(granted_by)),
        organization_id=org_uuid,
    )
    setup.granted = True
    logger.info("Model %s enabled and granted to organization %s", target, org_id)
    return setup


# --- First user / organisation --------------------------------------------


async def _find_existing_human(pool: Pool, *, org_name: str | None) -> dict | None:
    """Return an existing human owner/admin to adopt on a repeat run."""
    if org_name:
        rows = await pool.fetch(
            """
            SELECT u.id, u.display_name, u.email, u.role, u.organization_id
            FROM users u JOIN organizations o ON o.id = u.organization_id
            WHERE o.name = $1 AND u.is_active IS TRUE AND u.role <> 'daemon'
              AND COALESCE(u.email, '') NOT LIKE '%@lucent.local'
            ORDER BY (u.role = 'owner') DESC, u.created_at
            LIMIT 1
            """,
            org_name,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, display_name, email, role, organization_id
            FROM users
            WHERE is_active IS TRUE AND role <> 'daemon'
              AND COALESCE(email, '') NOT LIKE '%@lucent.local'
            ORDER BY (role = 'owner') DESC, created_at
            LIMIT 1
            """
        )
    return dict(rows[0]) if rows else None


async def provision_instance(
    pool: Pool,
    *,
    display_name: str,
    email: str | None,
    password: str,
    model_id: str | None = None,
    org_name: str | None = None,
    dry_run: bool = False,
) -> ProvisionResult:
    """Create (or adopt) the org + first user, ensure a usable model, mint a key.

    Safe to run twice: a second run adopts the existing organisation/owner,
    ensures the model, and does not create a duplicate user or key.

    The first-user path always mints an initial API key (the same call
    ``/setup`` uses); ``ProvisionResult.api_key`` carries the plaintext once so
    the caller can print or persist it. Callers that cannot deliver it (the
    container path with no key file) simply drop it — the plaintext is only
    ever stored hashed.
    """
    from lucent.auth_providers import (
        create_initial_user,
        is_first_run,
        validate_password_complexity,
    )
    from lucent.db.organization import OrganizationRepository

    result = ProvisionResult(dry_run=dry_run)

    if not await is_first_run(pool):
        existing = await _find_existing_human(pool, org_name=org_name)
        if existing is None:
            raise BootstrapError(
                "A human user exists but none could be adopted (organisation mismatch)."
            )
        result.user = existing
        result.organization = await OrganizationRepository(pool).get_by_id(
            existing["organization_id"]
        )
        result.notes.append(
            "Instance already provisioned; adopted the existing owner instead of "
            "creating another user (no new API key was minted)."
        )
        logger.info(
            "Bootstrap: adopting existing owner %r (org %s)",
            existing.get("display_name"),
            existing["organization_id"],
        )
    else:
        complexity_error = validate_password_complexity(password)
        if complexity_error:
            raise BootstrapError(f"Password rejected: {complexity_error}")

        if dry_run:
            result.notes.append(
                f"Would create organization and first owner user {display_name!r}."
            )
            chosen = model_id or await _pick_model_id(pool, os.environ.get(MODEL_ENV))
            if chosen:
                row = await pool.fetchrow(
                    "SELECT organization_id FROM models WHERE id = $1", chosen
                )
                result.model = ModelSetup(
                    chosen,
                    adopted=bool(row and row["organization_id"] is None),
                    enabled=True,
                    granted=True,
                )
                result.notes.append(
                    f"Model {chosen} would be enabled and granted to the new organization."
                )
            else:
                result.notes.append("No model is available to enable.")
        else:
            user, api_key = await create_initial_user(pool, display_name, email, password)
            result.user = user
            result.created_user = True
            result.created_org = True
            result.api_key = api_key
            if org_name:
                org_repo = OrganizationRepository(pool)
                current = await org_repo.get_by_id(user["organization_id"])
                if current and current["name"] != org_name:
                    await org_repo.update(user["organization_id"], org_name)
            result.organization = await OrganizationRepository(pool).get_by_id(
                user["organization_id"]
            )

    org_id = str(result.organization["id"]) if result.organization else None
    granted_by = str(result.user["id"]) if result.user else None

    if org_id and granted_by:
        # Ensure a credential-less instance is actually usable. On adoption the
        # existing instance may already satisfy this — ensure_usable_model is a
        # no-op then.
        result.model = await ensure_usable_model(
            pool, org_id=org_id, granted_by=granted_by, model_id=model_id, dry_run=dry_run
        )
    elif result.model is None:
        result.notes.append("Dry run: no model changes made.")

    return result


# --- Env-driven container bootstrap ---------------------------------------


def startup_bootstrap_enabled() -> bool:
    """True only when the operator explicitly opted the container in."""
    return os.environ.get(STARTUP_FLAG_ENV, "").strip().lower() in _TRUTHY


async def run_startup_bootstrap(pool: Pool) -> ProvisionResult | None:
    """Bootstrap a container from env, or return ``None`` if not applicable.

    Guarded conservatively — every condition must hold before an admin account
    is created, and the function *never* mutates a provisioned instance:

    1. ``LUCENT_BOOTSTRAP_ON_STARTUP`` is explicitly truthy (empty = off).
    2. No active human user exists (``is_first_run``). A provisioned instance
       is never touched, so this cannot re-run or reset one.
    3. ``LUCENT_BOOTSTRAP_PASSWORD`` is set and satisfies the password policy
       (a blank or weak admin password is refused, not created).
    """
    if not startup_bootstrap_enabled():
        return None

    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        logger.error(
            "%s is enabled but %s is unset; refusing to create an admin account "
            "without a password. Set it from a secret and restart.",
            STARTUP_FLAG_ENV,
            PASSWORD_ENV,
        )
        return None

    from lucent.auth_providers import is_first_run, validate_password_complexity

    if not await is_first_run(pool):
        logger.info(
            "%s is enabled but a human user already exists; skipping bootstrap "
            "(existing instance left untouched).",
            STARTUP_FLAG_ENV,
        )
        return None

    complexity_error = validate_password_complexity(password)
    if complexity_error:
        logger.error(
            "Refusing startup bootstrap: %s password does not meet policy (%s).",
            PASSWORD_ENV,
            complexity_error,
        )
        return None

    display_name = os.environ.get(DISPLAY_NAME_ENV) or DEFAULT_DISPLAY_NAME
    email = os.environ.get(EMAIL_ENV) or None
    org_name = os.environ.get(ORG_NAME_ENV) or None
    model_id = os.environ.get(MODEL_ENV) or None

    logger.warning(
        "Startup bootstrap: no human user exists and %s is enabled — creating "
        "the first owner %r now.",
        STARTUP_FLAG_ENV,
        display_name,
    )
    result = await provision_instance(
        pool,
        display_name=display_name,
        email=email,
        password=password,
        model_id=model_id,
        org_name=org_name,
    )
    _deliver_startup_api_key(result)
    logger.warning(
        "Startup bootstrap complete: organization=%s user=%s model=%s.",
        result.organization["id"] if result.organization else None,
        result.user["id"] if result.user else None,
        result.model.model_id if result.model else None,
    )
    return result


def _deliver_startup_api_key(result: ProvisionResult) -> None:
    """Persist the minted key to a file if configured — never into the logs."""
    if not result.api_key:
        return
    path = os.environ.get(API_KEY_FILE_ENV)
    if not path:
        logger.warning(
            "Startup bootstrap minted an API key but %s is unset; it was not "
            "written anywhere (the plaintext is shown only once). Retrieve a key "
            "via the web UI (Settings → API keys) or the bootstrap CLI.",
            API_KEY_FILE_ENV,
        )
        return
    # 0600: the file holds a live credential.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(result.api_key + "\n")
    logger.warning("Startup bootstrap wrote the initial API key to %s", path)
    # Do not keep the plaintext in the returned object any longer than needed.
    result.api_key = None


async def ensure_prerequisites(database_url: str) -> None:
    """Run the portable database prerequisites on a raw connection."""
    from lucent.db.prerequisites import ensure_database_prerequisites

    conn = await asyncpg.connect(database_url)
    try:
        report = await ensure_database_prerequisites(conn)
    finally:
        await conn.close()
    if not report.ok:
        raise BootstrapError(
            "Database prerequisites failed: " + "; ".join(report.errors)
        )


# --- CLI -------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lucent.bootstrap",
        description="Idempotently provision a fresh Lucent instance.",
    )
    parser.add_argument("--database-url", default=None, help="Defaults to $DATABASE_URL.")
    parser.add_argument(
        "--display-name",
        default=os.environ.get(DISPLAY_NAME_ENV) or DEFAULT_DISPLAY_NAME,
        help=f"First owner display name (env {DISPLAY_NAME_ENV}).",
    )
    parser.add_argument(
        "--email", default=os.environ.get(EMAIL_ENV) or None, help=f"Owner email (env {EMAIL_ENV})."
    )
    parser.add_argument(
        "--org-name",
        default=os.environ.get(ORG_NAME_ENV) or None,
        help=f"Organisation name (env {ORG_NAME_ENV}).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get(MODEL_ENV) or None,
        help="Model id to enable and grant (env LUCENT_BOOTSTRAP_MODEL).",
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "Owner password. Prefer the %s environment variable (a value on "
            "argv is visible in the process list)." % PASSWORD_ENV
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--skip-prerequisites",
        action="store_true",
        help="Skip extension/role/threshold provisioning.",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    from lucent.db.pool import close_db, init_db

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required (or pass --database-url)")
        return 1

    password = args.password or os.environ.get(PASSWORD_ENV)
    if not password:
        logger.error(
            "A password is required: set %s (preferred) or pass --password.", PASSWORD_ENV
        )
        return 1

    if not args.skip_prerequisites:
        try:
            await ensure_prerequisites(database_url)
        except Exception as exc:  # noqa: BLE001 — surface to the operator
            logger.error("Database prerequisites failed: %s", exc)
            return 2

    # init_db applies migrations (the schema must exist before provisioning).
    pool = await init_db(database_url)
    try:
        result = await provision_instance(
            pool,
            display_name=args.display_name,
            email=args.email,
            password=password,
            model_id=args.model,
            org_name=args.org_name,
            dry_run=args.dry_run,
        )
    finally:
        await close_db()

    _print_result(result)
    return 0


def _print_result(result: ProvisionResult) -> None:
    out = sys.stdout
    out.write("\n=== Lucent bootstrap ===\n")
    out.write(f"mode:            {'DRY RUN (no changes)' if result.dry_run else 'applied'}\n")
    if result.organization:
        name = result.organization.get("name")
        out.write(f"organization:    {name} ({result.organization['id']})\n")
    if result.user:
        out.write(
            f"owner:           {result.user.get('display_name')} "
            f"({result.user['id']}) role={result.user.get('role')}\n"
        )
    if result.model:
        m = result.model
        actions = []
        if m.adopted:
            actions.append("adopted-into-org")
        actions.append("enabled")
        actions.append("org-read-granted")
        out.write(f"model:           {m.model_id} [{', '.join(actions)}]\n")
    for note in result.notes:
        out.write(f"note:            {note}\n")
    if result.api_key:
        out.write("\nInitial API key (shown once — store it now):\n")
        out.write(f"  {result.api_key}\n")
    elif not result.dry_run:
        out.write("\nNo new API key minted (an existing one is reused on repeat runs).\n")
    out.write("========================\n")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run_cli(args))
    except Exception as exc:  # noqa: BLE001 — surface *why* to the operator
        logger.error("Bootstrap failed: %s", exc, exc_info=True)
        return 3


if __name__ == "__main__":
    sys.exit(main())
