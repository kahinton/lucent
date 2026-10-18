"""Portable, idempotent provisioning of Lucent's database prerequisites.

Local Docker creates these out-of-band in ``docker/postgres-init.sh``: that
script runs only on first container init and only for the Docker image. A
managed PostgreSQL (or any CI/Terraform provisioner) has no equivalent, and
the migrations cannot supply it — migration 001 issues ``CREATE EXTENSION``,
which *fails* when the deployment role is not a superuser / ``azure_pg_admin``,
and the ``lucent_daemon`` role is only ever *granted to* behind
``IF EXISTS`` guards.

This module is the explicit, idempotent, portable step: it is safe to run
before migrations, safe to run repeatedly, and it *detects and reports*
privilege failures with the operator action required instead of crashing
obscurely.

What it ensures:

- ``pg_trgm`` — mandatory (trigram search). A missing/uncreatable ``pg_trgm``
  is a hard error: the schema and search paths depend on it.
- ``uuid-ossp`` — legacy ``uuid_generate_v4()`` default. Best-effort: after
  migration ``138`` no column default needs it, so a deployment that cannot
  allowlist it only needs it for the one-time migration 001 bootstrap.
- the ``lucent_daemon`` role, with a password taken from ``DAEMON_DB_PASSWORD``
  (never logged), plus its ``CONNECT``/``USAGE`` grants.
- ``ALTER DATABASE ... SET pg_trgm.similarity_threshold``.

The caller decides what to do with a non-``ok`` report; on a managed server the
expected resolution is that an administrator pre-creates the extensions and
role, after which this step reports them as already ``present``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

import asyncpg

from lucent.logging import get_logger

logger = get_logger(__name__)

# ``CREATE EXTENSION/ROLE ... IF NOT EXISTS`` is not atomic under concurrency:
# two sessions that both see the object missing both insert, and the loser gets
# a unique/duplicate-object violation instead of a NOTICE. Treat those as "the
# other session created it" after re-checking.
_CONCURRENT_CREATE_ERRORS = (
    asyncpg.exceptions.UniqueViolationError,
    asyncpg.exceptions.DuplicateObjectError,
)

DAEMON_ROLE: Final[str] = "lucent_daemon"
DAEMON_DB_PASSWORD_ENV: Final[str] = "DAEMON_DB_PASSWORD"

#: Extensions the schema genuinely cannot run without.
REQUIRED_EXTENSIONS: Final[tuple[str, ...]] = ("pg_trgm",)
#: Extensions kept only for the legacy ``uuid_generate_v4()`` default.
LEGACY_EXTENSIONS: Final[tuple[str, ...]] = ("uuid-ossp",)

TRIGRAM_THRESHOLD: Final[str] = "0.3"


@dataclass
class PrerequisiteReport:
    """Outcome of :func:`ensure_database_prerequisites` (never holds secrets)."""

    # extension name -> status ("present" | "created" | "unavailable" |
    # "permission-denied")
    extensions: dict[str, str] = field(default_factory=dict)
    daemon_role: str = "not-attempted"
    trigram_threshold: str = "not-attempted"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing hard-failed (warnings are permitted)."""
        return not self.errors

    def summary_lines(self) -> list[str]:
        """Human-readable lines for logs / CLI output (no secrets)."""
        lines: list[str] = []
        for name, status in self.extensions.items():
            lines.append(f"extension {name}: {status}")
        lines.append(f"daemon role {DAEMON_ROLE}: {self.daemon_role}")
        lines.append(f"pg_trgm.similarity_threshold: {self.trigram_threshold}")
        lines.extend(f"warning: {w}" for w in self.warnings)
        lines.extend(f"error: {e}" for e in self.errors)
        return lines


async def _extension_installed(conn: asyncpg.Connection, name: str) -> bool:
    return bool(
        await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = $1)", name)
    )


async def _extension_available(conn: asyncpg.Connection, name: str) -> bool:
    """True if the server ships the extension at all (allowlisting aside)."""
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = $1)", name
        )
    )


async def _ensure_extension(
    conn: asyncpg.Connection, name: str, report: PrerequisiteReport, *, required: bool
) -> None:
    if await _extension_installed(conn, name):
        report.extensions[name] = "present"
        return

    if not await _extension_available(conn, name):
        report.extensions[name] = "unavailable"
        message = (
            f"extension {name} is not available on this server — it must be "
            f"allowlisted (managed PostgreSQL: add it to the `azure.extensions` "
            f"server parameter) before Lucent can start"
        )
        if required:
            report.errors.append(message)
            logger.error(message)
        else:
            report.warnings.append(message)
            logger.warning(message)
        return

    try:
        await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{name}"')
    except _CONCURRENT_CREATE_ERRORS:
        # A concurrent session created it between our check and ours; verify.
        if await _extension_installed(conn, name):
            report.extensions[name] = "present"
            return
        raise
    except asyncpg.exceptions.InsufficientPrivilegeError:
        report.extensions[name] = "permission-denied"
        message = (
            f"CREATE EXTENSION {name} was denied: the deployment role lacks "
            f"privileges. On managed PostgreSQL this needs azure_pg_admin (or "
            f"superuser); have an administrator run CREATE EXTENSION {name} "
            f"once, then re-run this step"
        )
        if required:
            report.errors.append(message)
            logger.error(message)
        else:
            report.warnings.append(message)
            logger.warning(message)
        return

    report.extensions[name] = "created"


async def _ensure_role(
    conn: asyncpg.Connection,
    *,
    role: str,
    password: str | None,
    report: PrerequisiteReport,
) -> None:
    exists = bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", role
        )
    )
    if not exists:
        if not password:
            report.daemon_role = "missing-no-password"
            message = (
                f"role {role} does not exist and {DAEMON_DB_PASSWORD_ENV} is "
                f"unset; the daemon will not be able to authenticate. Create "
                f"the role and set {DAEMON_DB_PASSWORD_ENV} (from a secret)."
            )
            report.warnings.append(message)
            logger.warning(message)
            return
        try:
            # Role name is a module constant; the password is escaped as a
            # literal because ALTER/CREATE ROLE PASSWORD will not take a bind
            # parameter. It is never logged.
            await conn.execute(
                f'CREATE ROLE "{role}" WITH LOGIN PASSWORD {_sql_literal(password)}'
            )
        except _CONCURRENT_CREATE_ERRORS:
            # Another session created the role first; make sure it carries the
            # intended password and fall through to the grant handling below.
            logger.info("role %s was created concurrently by another session", role)
            try:
                await conn.execute(
                    f'ALTER ROLE "{role}" WITH PASSWORD {_sql_literal(password)}'
                )
            except asyncpg.exceptions.InsufficientPrivilegeError:
                pass
        except asyncpg.exceptions.InsufficientPrivilegeError:
            report.daemon_role = "permission-denied"
            message = (
                f"CREATE ROLE {role} was denied: the deployment role needs "
                f"CREATEROLE (or an administrator must create {role} first)"
            )
            report.errors.append(message)
            logger.error(message)
            return
        report.daemon_role = "created"
    elif password:
        try:
            await conn.execute(f'ALTER ROLE "{role}" WITH PASSWORD {_sql_literal(password)}')
            report.daemon_role = "password-updated"
        except asyncpg.exceptions.InsufficientPrivilegeError:
            report.daemon_role = "permission-denied"
            message = f"ALTER ROLE {role} was denied: the deployment role cannot set its password"
            report.errors.append(message)
            logger.error(message)
            return
    else:
        report.daemon_role = "present"

    # Grants are idempotent and need ownership of the database/schema.
    try:
        db = await conn.fetchval("SELECT current_database()")
        await conn.execute(f'GRANT CONNECT ON DATABASE "{_ident(db)}" TO "{role}"')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
    except asyncpg.exceptions.InsufficientPrivilegeError:
        message = (
            f"could not GRANT CONNECT/USAGE to {role}: the deployment role must "
            f"own the database (or be its admin)"
        )
        report.warnings.append(message)
        logger.warning(message)


async def _ensure_trigram_threshold(conn: asyncpg.Connection, report: PrerequisiteReport) -> None:
    db = await conn.fetchval("SELECT current_database()")
    try:
        await conn.execute(
            f'ALTER DATABASE "{_ident(db)}" '
            f"SET pg_trgm.similarity_threshold = {TRIGRAM_THRESHOLD}"
        )
    except asyncpg.exceptions.InsufficientPrivilegeError:
        report.trigram_threshold = "permission-denied"
        message = (
            "could not set pg_trgm.similarity_threshold on the database: the "
            "deployment role must own it (or an administrator must run the "
            "ALTER DATABASE ... SET)"
        )
        report.warnings.append(message)
        logger.warning(message)
        return
    except asyncpg.PostgresError as exc:
        # e.g. a concurrent ALTER DATABASE SET racing on the same row. Not
        # fatal: a database-wide default of 0.3 is pg_trgm's own default.
        report.trigram_threshold = "not-set"
        message = f"could not set pg_trgm.similarity_threshold: {exc}"
        report.warnings.append(message)
        logger.warning(message)
        return
    report.trigram_threshold = f"set ({TRIGRAM_THRESHOLD})"


def _sql_literal(value: str) -> str:
    """Quote a Python string as a PostgreSQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _ident(value: str) -> str:
    """Quote a Python string as a PostgreSQL identifier."""
    return value.replace('"', '""')


async def ensure_database_prerequisites(
    conn: asyncpg.Connection,
    *,
    daemon_password: str | None = None,
) -> PrerequisiteReport:
    """Ensure every out-of-band database prerequisite, idempotently.

    Args:
        conn: An open connection with the privileges the operator intends the
            deployment to use for schema creation (table owner / admin).
        daemon_password: Password for the ``lucent_daemon`` role. Falls back to
            the ``DAEMON_DB_PASSWORD`` environment variable. Never logged.

    Returns:
        A :class:`PrerequisiteReport`. Callers should treat
        ``report.ok is False`` as a hard failure (a mandatory piece — the
        ``pg_trgm`` extension, usually — could not be provisioned).
    """
    report = PrerequisiteReport()
    password = daemon_password if daemon_password is not None else os.environ.get(
        DAEMON_DB_PASSWORD_ENV
    )

    for name in REQUIRED_EXTENSIONS:
        await _ensure_extension(conn, name, report, required=True)
    for name in LEGACY_EXTENSIONS:
        await _ensure_extension(conn, name, report, required=False)

    await _ensure_role(conn, role=DAEMON_ROLE, password=password or None, report=report)
    await _ensure_trigram_threshold(conn, report)

    for line in report.summary_lines():
        logger.info("db prerequisite: %s", line)
    return report
