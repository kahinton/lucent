"""Unit tests for the ``python -m lucent.migrate`` CLI (lucent.migrate)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import lucent.migrate as migrate
from lucent.db.prerequisites import PrerequisiteReport
from lucent.migrate import EXIT_CONFIG, EXIT_MIGRATION, EXIT_OK, EXIT_PREREQUISITES, _build_parser


def _args(*argv: str):
    return _build_parser().parse_args(list(argv))


class TestArgParsing:
    def test_defaults(self):
        args = _args()
        assert args.database_url is None
        assert args.skip_prerequisites is False
        assert args.rollback is None

    def test_rollback_and_target(self):
        args = _args("--rollback", "2", "--target", "100_x.sql")
        assert args.rollback == 2
        assert args.target == "100_x.sql"


class TestRun:
    async def test_missing_database_url_is_config_error(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert await migrate._run(_args()) == EXIT_CONFIG

    async def test_applies_migrations_and_returns_ok(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
        pool = MagicMock()
        with (
            patch.object(migrate, "init_db", AsyncMock(return_value=pool)) as init_db,
            patch.object(migrate, "run_migrations", AsyncMock()) as run,
            patch.object(migrate, "close_db", AsyncMock()) as close,
        ):
            code = await migrate._run(_args("--skip-prerequisites"))

        assert code == EXIT_OK
        init_db.assert_awaited_once()
        # run_migrations=False: the CLI controls migrations itself.
        assert init_db.await_args.kwargs["run_migrations"] is False
        run.assert_awaited_once_with(pool)
        close.assert_awaited_once()

    async def test_prerequisite_failure_returns_two(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
        report = PrerequisiteReport()
        report.errors.append("pg_trgm unavailable")
        conn = AsyncMock()
        with (
            patch.object(migrate.asyncpg, "connect", AsyncMock(return_value=conn)),
            patch.object(
                migrate, "ensure_database_prerequisites", AsyncMock(return_value=report)
            ),
            patch.object(migrate, "init_db", AsyncMock()) as init_db,
        ):
            code = await migrate._run(_args())

        assert code == EXIT_PREREQUISITES
        init_db.assert_not_awaited()

    def test_migration_failure_is_reported(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
        with (
            patch.object(migrate, "init_db", AsyncMock(return_value=MagicMock())),
            patch.object(migrate, "run_migrations", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(migrate, "close_db", AsyncMock()),
        ):
            code = migrate.main(["--skip-prerequisites"])

        assert code == EXIT_MIGRATION

    async def test_rollback_path(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
        with (
            patch.object(migrate, "init_db", AsyncMock(return_value=MagicMock())),
            patch.object(migrate, "run_migrations", AsyncMock()) as run,
            patch.object(migrate, "rollback_migrations", AsyncMock(return_value=1)) as rb,
            patch.object(migrate, "close_db", AsyncMock()),
        ):
            code = await migrate._run(_args("--skip-prerequisites", "--rollback", "1"))

        assert code == EXIT_OK
        run.assert_not_awaited()
        rb.assert_awaited_once()

    def test_main_catches_exceptions(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")

        async def _boom(_args):
            raise ValueError("nope")

        with patch.object(migrate, "_run", _boom):
            assert migrate.main([]) == EXIT_MIGRATION


@pytest.mark.parametrize("argv", [["--rollback", "0"], []])
def test_parser_accepts_common_forms(argv):
    assert _build_parser().parse_args(argv) is not None
