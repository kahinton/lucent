"""``python -m lucent.migrate`` — apply schema migrations and exit.

The server applies migrations on startup, but a dedicated pre-deploy step is
the right pattern for a multi-replica rollout: run this once against the
database, then start (or scale) the app. It reuses the exact runner the server
uses (:func:`lucent.db.pool.run_migrations`), so behaviour cannot drift.

The runner serialises concurrent invocations with a session-level advisory
lock, so two of these running at once wait rather than racing.

Exit codes:
    0  success (migrations applied, or nothing to do)
    1  configuration error (no database URL)
    2  prerequisite provisioning failed (an extension could not be created)
    3  a migration failed to apply

Usage::

    python -m lucent.migrate                     # DATABASE_URL from env
    python -m lucent.migrate --database-url ...   # explicit URL
    python -m lucent.migrate --skip-prerequisites # schema only
    python -m lucent.migrate --rollback 1         # reverse one migration
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

from lucent.db.pool import close_db, init_db, rollback_migrations, run_migrations
from lucent.db.prerequisites import ensure_database_prerequisites
from lucent.logging import configure_logging, get_logger

logger = get_logger("lucent.migrate")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_PREREQUISITES = 2
EXIT_MIGRATION = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lucent.migrate",
        description="Apply (or roll back) Lucent database migrations and exit.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL connection URL. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--skip-prerequisites",
        action="store_true",
        help=(
            "Skip extension/role/threshold provisioning. Use when an "
            "administrator has already prepared the database."
        ),
    )
    parser.add_argument(
        "--rollback",
        type=int,
        default=None,
        metavar="N",
        help="Roll back the N most recently applied migrations instead of applying.",
    )
    parser.add_argument(
        "--target",
        default=None,
        metavar="NAME",
        help="With --rollback, roll back everything applied after NAME.",
    )
    parser.add_argument(
        "--allow-irreversible",
        action="store_true",
        help="Permit rollback of migrations marked irreversible or lacking a .down.sql.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required (or pass --database-url)")
        return EXIT_CONFIG

    if not args.skip_prerequisites:
        conn = await asyncpg.connect(database_url)
        try:
            report = await ensure_database_prerequisites(conn)
        except Exception as exc:  # noqa: BLE001 — a prerequisite step failure, not a migration
            logger.error("Database prerequisites failed: %s", exc)
            return EXIT_PREREQUISITES
        finally:
            await conn.close()
        if not report.ok:
            logger.error("Database prerequisites could not be provisioned:")
            for line in report.summary_lines():
                logger.error("  %s", line)
            return EXIT_PREREQUISITES

    # run_migrations=False: this process exists to run the runner itself and
    # control the exit code, not to serve the app.
    pool = await init_db(database_url, run_migrations=False)
    try:
        if args.rollback is not None:
            count = await rollback_migrations(
                pool,
                steps=args.rollback,
                target_name=args.target,
                allow_irreversible=args.allow_irreversible,
            )
            logger.info("Rolled back %d migration(s)", count)
        else:
            await run_migrations(pool)
            logger.info("Migrations up to date")
    finally:
        await close_db()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — surface *why* to the CI operator
        logger.error("Migration failed: %s", exc, exc_info=True)
        return EXIT_MIGRATION


if __name__ == "__main__":
    sys.exit(main())
