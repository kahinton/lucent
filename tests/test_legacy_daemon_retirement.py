"""The bare `daemon-service` identity is retired.

Migration 017 created one instance-wide daemon account. Multi-tenancy replaced
it with one per organization (`daemon-service:{org_id}`), but nine code paths
kept accepting either form. That left a dormant privilege path: the legacy
account had no password and no API keys, yet held 1,248 memory read clearances
and owner clearances on 40 definitions — and an unscoped daemon key is
documented as "acting as the org's first owner" for memory visibility. Recognizing
an identity by *name prefix* rather than by role or clearance is the bug.

These tests pin the retirement so the dual-form checks cannot creep back.
"""

from __future__ import annotations

import inspect

from lucent.api.deps import CurrentUser


def _user(external_id: str, role: str = "daemon") -> CurrentUser:
    return CurrentUser(
        id="00000000-0000-0000-0000-000000000001",
        organization_id="00000000-0000-0000-0000-000000000002",
        role=role,
        email=None,
        display_name=None,
        external_id=external_id,
    )


class TestDaemonServiceRecognition:
    def test_per_org_identity_is_recognized(self):
        assert _user("daemon-service:abc-123").is_daemon_service is True

    def test_bare_legacy_identity_is_not_recognized(self):
        """The whole point: a bare `daemon-service` must not pass as a daemon."""
        assert _user("daemon-service").is_daemon_service is False

    def test_unrelated_identities_are_not_recognized(self):
        assert _user("someone@example.com", role="member").is_daemon_service is False
        assert _user("daemon-service-ish").is_daemon_service is False
        assert _user("").is_daemon_service is False


class TestNoBareIdentityInSource:
    """Guards the nine call sites that used to accept both forms."""

    def _assert_no_bare_equality(self, module) -> None:
        """No *code* may compare against the bare id.

        Comments are stripped first: a comment explaining the retirement
        legitimately names the old value, and flagging that would train people
        to delete the explanation.
        """
        lines = []
        for line in inspect.getsource(module).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Drop trailing comments (the codebase does not use '#' inside
            # string literals on these lines).
            code = line.split("#", 1)[0]
            lines.append(code)
        source = "\n".join(lines)

        # Exact-equality against the bare id is what we removed. The
        # ``daemon-service:`` prefix form is still legitimate.
        for needle in ('"daemon-service"', "'daemon-service'"):
            assert needle not in source, (
                f"{module.__name__} still compares against the retired bare "
                "daemon-service id; use the per-org 'daemon-service:' prefix"
            )

    def test_memory_repository_has_no_bare_check(self):
        from lucent.db import memory

        self._assert_no_bare_equality(memory)

    def test_tools_memories_has_no_bare_check(self):
        from lucent.tools import memories

        self._assert_no_bare_equality(memories)

    def test_schedules_router_has_no_bare_check(self):
        from lucent.api.routers import schedules

        self._assert_no_bare_equality(schedules)

    def test_schedules_repository_has_no_bare_creator_check(self):
        from lucent.db import schedules

        source = inspect.getsource(schedules)
        assert "creator.external_id = 'daemon-service'" not in source
        assert "creator.external_id LIKE 'daemon-service:%'" in source


class TestMigrationRetiresTheIdentity:
    def test_migration_exists(self):
        from pathlib import Path

        migrations = Path(__file__).resolve().parents[1] / "src/lucent/db/migrations"
        up = migrations / "150_retire_legacy_daemon_service.sql"
        down = migrations / "150_retire_legacy_daemon_service.down.sql"
        assert up.is_file()
        assert down.is_file()

    def test_migration_deactivates_rather_than_deletes(self):
        """Historical provenance is why the row survives.

        ~19,200 rows reference the identity through NO ACTION foreign keys —
        9,801 task requesters, 4,632 request creators, 4,614 audit rows.
        Deleting it would mean rewriting years of authorship and audit history.
        Deactivation removes the ability to authenticate, which is the actual
        security objective.
        """
        from pathlib import Path

        sql = (
            Path(__file__).resolve().parents[1]
            / "src/lucent/db/migrations/150_retire_legacy_daemon_service.sql"
        ).read_text()
        assert "is_active = false" in sql
        assert "password_hash = NULL" in sql
        assert "DELETE FROM users" not in sql

    def test_migration_reassigns_clearances_by_builtin_scope(self):
        from pathlib import Path

        sql = (
            Path(__file__).resolve().parents[1]
            / "src/lucent/db/migrations/150_retire_legacy_daemon_service.sql"
        ).read_text()
        # Built-in grants go to the org daemon; everything else to the owner.
        assert "org_daemon" in sql
        assert "owner_id" in sql
        assert "scope = 'built-in'" in sql
