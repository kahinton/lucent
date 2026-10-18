"""Unit tests for the portable database prerequisites (lucent.db.prerequisites)."""

from __future__ import annotations

import asyncpg
import pytest

from lucent.db.prerequisites import (
    DAEMON_ROLE,
    ensure_database_prerequisites,
)


class FakeConn:
    """Minimal asyncpg-connection stand-in for prerequisite logic."""

    def __init__(
        self,
        *,
        installed=(),
        available=("pg_trgm", "uuid-ossp"),
        roles=(),
        dbname="lucent",
        fail_create_extension=False,
        fail_create_role=False,
        fail_alter_database=False,
    ):
        self.installed = set(installed)
        self.available = set(available)
        self.roles = set(roles)
        self.dbname = dbname
        self.fail_create_extension = fail_create_extension
        self.fail_create_role = fail_create_role
        self.fail_alter_database = fail_alter_database
        self.executed: list[str] = []

    async def fetchval(self, query, *args):
        q = query.lower()
        if "from pg_extension" in q:
            return args[0] in self.installed
        if "from pg_available_extensions" in q:
            return args[0] in self.available
        if "from pg_roles" in q:
            return args[0] in self.roles
        if "current_database" in q:
            return self.dbname
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query, *args):
        q = query.lower()
        if "create extension" in q and self.fail_create_extension:
            raise asyncpg.exceptions.InsufficientPrivilegeError()
        if "create role" in q and self.fail_create_role:
            raise asyncpg.exceptions.InsufficientPrivilegeError()
        if "alter database" in q and self.fail_alter_database:
            raise asyncpg.exceptions.InsufficientPrivilegeError()
        self.executed.append(query)
        return "OK"


class TestAllPresent:
    async def test_noop_when_everything_exists(self):
        conn = FakeConn(
            installed=("pg_trgm", "uuid-ossp"), roles=(DAEMON_ROLE,)
        )
        report = await ensure_database_prerequisites(conn, daemon_password=None)

        assert report.ok
        assert report.extensions == {"pg_trgm": "present", "uuid-ossp": "present"}
        assert report.daemon_role == "present"
        assert report.trigram_threshold.startswith("set")
        # Only the two idempotent GRANTs + the ALTER DATABASE ran.
        assert all("create extension" not in q.lower() for q in conn.executed)


class TestExtensionFailures:
    async def test_pg_trgm_unavailable_is_hard_error(self):
        conn = FakeConn(installed=(), available=("uuid-ossp",))
        report = await ensure_database_prerequisites(conn, daemon_password=None)

        assert not report.ok
        assert report.extensions["pg_trgm"] == "unavailable"
        assert any("pg_trgm" in e for e in report.errors)

    async def test_pg_trgm_permission_denied_is_hard_error(self):
        conn = FakeConn(
            installed=(), available=("pg_trgm", "uuid-ossp"), fail_create_extension=True
        )
        report = await ensure_database_prerequisites(conn, daemon_password=None)

        assert not report.ok
        assert report.extensions["pg_trgm"] == "permission-denied"
        assert any("azure_pg_admin" in e for e in report.errors)

    async def test_uuid_ossp_failure_is_only_a_warning(self):
        # uuid-ossp exists in available but creation is denied; pg_trgm already
        # installed, so this must not fail the run.
        conn = FakeConn(
            installed=("pg_trgm",), available=("pg_trgm", "uuid-ossp"),
            fail_create_extension=True,
        )
        report = await ensure_database_prerequisites(conn, daemon_password=None)

        assert report.ok
        assert report.extensions["pg_trgm"] == "present"
        assert report.extensions["uuid-ossp"] == "permission-denied"
        assert report.warnings


class TestDaemonRole:
    async def test_missing_role_without_password_warns(self):
        conn = FakeConn(installed=("pg_trgm",))
        report = await ensure_database_prerequisites(conn, daemon_password=None)

        assert report.ok  # a warning, not an error
        assert report.daemon_role == "missing-no-password"
        assert any(DAEMON_ROLE in w for w in report.warnings)

    async def test_missing_role_with_password_is_created(self):
        conn = FakeConn(installed=("pg_trgm",))
        report = await ensure_database_prerequisites(conn, daemon_password="s3cret-pw")

        assert report.ok
        assert report.daemon_role == "created"
        assert any("CREATE ROLE" in q for q in conn.executed)

    async def test_existing_role_password_updated(self):
        conn = FakeConn(installed=("pg_trgm",), roles=(DAEMON_ROLE,))
        report = await ensure_database_prerequisites(conn, daemon_password="s3cret-pw")

        assert report.daemon_role == "password-updated"

    async def test_password_never_appears_in_report(self):
        conn = FakeConn(installed=("pg_trgm",), roles=(DAEMON_ROLE,))
        report = await ensure_database_prerequisites(conn, daemon_password="top-secret-value")

        assert all("top-secret-value" not in line for line in report.summary_lines())

    async def test_password_with_quote_is_escaped(self):
        conn = FakeConn(installed=("pg_trgm",))
        await ensure_database_prerequisites(conn, daemon_password="a'b")
        role_sql = next(q for q in conn.executed if 'CREATE ROLE' in q)
        assert "'a''b'" in role_sql

    async def test_create_role_denied_is_hard_error(self):
        conn = FakeConn(installed=("pg_trgm",), fail_create_role=True)
        report = await ensure_database_prerequisites(conn, daemon_password="pw")
        assert not report.ok
        assert report.daemon_role == "permission-denied"


class TestThreshold:
    async def test_alter_database_denied_is_warning_only(self):
        conn = FakeConn(installed=("pg_trgm",), roles=(DAEMON_ROLE,), fail_alter_database=True)
        report = await ensure_database_prerequisites(conn, daemon_password=None)
        assert report.ok
        assert report.trigram_threshold == "permission-denied"
        assert report.warnings


@pytest.mark.parametrize("dbname", ["lucent", 'weird"name'])
async def test_database_identifier_is_escaped(dbname):
    conn = FakeConn(installed=("pg_trgm",), roles=(DAEMON_ROLE,), dbname=dbname)
    report = await ensure_database_prerequisites(conn, daemon_password=None)
    assert report.trigram_threshold.startswith("set")
