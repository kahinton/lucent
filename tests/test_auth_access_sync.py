"""Guards that the SQL migrations and the Python resource specs agree.

The auth-ID authorization system has two descriptions of "which resource
tables get ownership clearances and from which owner column":

- migration 123's VALUES list (builds the auth_owner_clearance triggers), and
- ``AUTH_ACCESS_RESOURCES`` in lucent.db.access_control (the grant-management
  API's table/owner-column lookup).

These must not drift: a mismatch means triggers grant ownership from a
different column than the API treats as authoritative (or, when the column
does not exist at all, migration 123 silently skips the table).

A database-backed guard also checks that every live resource row actually
carries at least one clearance — the drift class a backfill omission falls
into (migration 130 fixed exactly that for managed_tool_definitions).
"""

import re
from pathlib import Path

import pytest

from lucent.db.access_control import AUTH_ACCESS_RESOURCES

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "lucent" / "db" / "migrations"

_VALUES_ROW = re.compile(r"\('([a-z_]+)', '([a-z_]+)', '([a-z_]+)'\)")
_UNNEST_ROW = re.compile(r"[a-z_]+")


def _migration_123_owner_targets() -> set[tuple[str, str]]:
    sql = (MIGRATIONS_DIR / "123_auth_access_roles.sql").read_text()
    block_match = re.search(
        r"SELECT \* FROM \(VALUES\n(.*?)\n    \) AS targets\(table_name, id_column, owner_column\)",
        sql,
        flags=re.DOTALL,
    )
    assert block_match, "123_auth_access_roles.sql is missing its VALUES target list"
    return {
        (table, owner_column)
        for table, _id_column, owner_column in _VALUES_ROW.findall(block_match.group(1))
    }


def _later_owner_trigger_targets() -> set[tuple[str, str]]:
    """Resources whose owner triggers were added after migration 123.

    Later migrations that create auth_owner_clearance triggers list their
    targets in the same ('table', 'id', 'owner') comment shape so this guard
    keeps 123's list authoritative for exactly the tables 123 covers.
    """
    targets: set[tuple[str, str]] = set()
    for path in sorted(MIGRATIONS_DIR.glob("1*.sql")):
        if path.name.startswith("123_"):
            continue
        sql = path.read_text()
        if "auth_owner_clearance" not in sql:
            continue
        for match in re.finditer(
            r"^-- \('([a-z_]+)', '([a-z_]+)', '([a-z_]+)'\)(?: *-- .+)?$",
            sql,
            flags=re.MULTILINE,
        ):
            table, _id_column, owner_column = match.groups()
            targets.add((table, owner_column))
    return targets


def _migration_122_auth_id_tables() -> set[str]:
    sql = (MIGRATIONS_DIR / "122_auth_id_resource_columns.sql").read_text()
    match = re.search(
        r"INSERT INTO auth_id_targets \(table_name, key_column\) VALUES\n(.*?);",
        sql,
        flags=re.DOTALL,
    )
    assert match, "122_auth_id_resource_columns.sql is missing its target list"
    return set(_UNNEST_ROW.findall(match.group(1)))


# Tables whose 123+ owner-clearance trigger stays but whose grantable API
# spec was deliberately removed. Sandbox instances belong to whoever is
# running them and are never shareable; their creator's owner clearance
# still files automatically.
_NON_GRANTABLE_OWNER_TABLES = {("sandboxes", "created_by")}


def test_owner_trigger_tables_match_auth_access_resource_specs():
    trigger_targets = _migration_123_owner_targets() | _later_owner_trigger_targets()
    spec_targets = {
        (spec.table, spec.owner_column) for spec in AUTH_ACCESS_RESOURCES.values()
    }
    assert trigger_targets == spec_targets | _NON_GRANTABLE_OWNER_TABLES, (
        "the auth_owner_clearance trigger list (123 + later migrations) must "
        "exactly match AUTH_ACCESS_RESOURCES (table, owner_column) pairs "
        "plus the documented non-grantable owner tables"
    )


def test_every_acl_resource_table_has_an_auth_id():
    """The clearance triggers need each resource row's auth_id column."""
    auth_id_tables = _migration_122_auth_id_tables()
    for spec in AUTH_ACCESS_RESOURCES.values():
        assert spec.table in auth_id_tables, (
            f"{spec.table} carries clearances but has no auth_id column in migration 122"
        )


# Documented zero-clearance exemptions per family (migration 130 header):
#   models          — models belong to the organization and start CLOSED
#                     (migration 131): every row is default-deny until
#                     someone is granted access explicitly, so a live model
#                     with no clearance is by design, not drift.
#   secrets         — system_managed rows are read on the scoped system path
#                     and carry no clearances by design.
#   sandboxes       — destroyed/failed rows are terminal archives; live rows
#                     are assigned an owner (127 §5) whose clearance is filed.
#   schedules       — is_system rows are daemon-service-user owned (128), but
#                     the legacy "__lucent_system__" org has no users at all;
#                     its process-owned rows are only read on the system path.
# Every other resource row must carry at least one clearance. Models are
# exempted entirely: closed-by-default (131) means zero clearances is the
# intended starting state for any model.
_EXEMPT_TABLES = {"models", "secrets", "sandboxes", "schedules"}


@pytest.mark.asyncio
async def test_no_zero_clearance_resource_rows_outside_documented_exemptions(db_pool):
    """Every live resource row must carry at least one clearance.

    Catches the drift class migration 126 hit: triggers exist, but rows whose
    owner predates the trigger never got their backfill, silently falling out
    of every clearance-driven *use* read.
    """
    for spec in AUTH_ACCESS_RESOURCES.values():
        table = spec.table
        if table in _EXEMPT_TABLES:
            predicate = {
                "models": "false",
                "secrets": "s.system_managed IS NOT TRUE",
                "sandboxes": "s.status NOT IN ('destroyed', 'failed')",
                # All system schedules in a real org are daemon-owner clears;
                # only the legacy infra org is exempt.
                "schedules": (
                    "EXISTS (SELECT 1 FROM organizations o WHERE o.id = s.organization_id "
                    "AND o.name = '__lucent_system__') IS NOT TRUE"
                ),
            }[table]
        else:
            predicate = "true"

        query = (
            f"SELECT count(*) AS n FROM {table} s "
            f"WHERE s.auth_id IS NOT NULL AND ({predicate}) "
            f"AND NOT EXISTS (SELECT 1 FROM auth_clearances c WHERE c.auth_id = s.auth_id)"
        )
        async with db_pool.acquire() as conn:
            zero = await conn.fetchval(query)
        assert zero == 0, (
            f"{table} has {zero} live row(s) with an owner-set state but no "
            "clearance — the clearance representation drifted (see migration 130)"
        )
