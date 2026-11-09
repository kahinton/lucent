"""Integration tests for organization entitlements: storage + repository.

Covers the behavior only a real database can prove:

* the table's rows are organization-scoped and default-deny (no row = no
  entitlement, and the migration backfill cannot be conjured by reading),
* ``set_entitlement`` upserts — a second write for the same
  (organization, capability) replaces the row instead of violating the
  unique index,
* the CHECK constraints reject an unknown capability or source at write time,
  so a typo cannot create a silently dead entitlement,
* the bulk read serves several organizations in one query and maps orgs with
  no rows to an empty list instead of omitting them,
* the shared ``updated_at`` trigger fires on update, consistent with the rest
  of this schema.
"""

import uuid

import asyncpg
import pytest

from lucent.db import EntitlementRepository, OrganizationRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def other_organization(db_pool):
    """A second organization, cleaned up after the test."""
    from lucent.db import get_pool

    repo = OrganizationRepository(db_pool)
    org = await repo.create(name=f"test_{uuid.uuid4().hex[:10]}_entitlements")
    yield org
    async with (await get_pool()).acquire() as conn:
        await conn.execute("DELETE FROM organizations WHERE id = $1", str(org["id"]))


async def test_new_organization_has_no_entitlement_rows_and_is_denied(
    db_pool, test_organization
):
    org_id = test_organization["id"]
    repo = EntitlementRepository(db_pool)

    assert await repo.get_entitlements(org_id) == []
    assert await repo.is_enabled(org_id, "custom_sandboxes") is False
    assert await repo.is_enabled(org_id, "managed_tools") is False


async def test_set_entitlement_inserts_then_upserts(db_pool, test_organization):
    org_id = test_organization["id"]
    repo = EntitlementRepository(db_pool)

    created = await repo.set_entitlement(
        org_id,
        "custom_sandboxes",
        True,
        source="override",
        limits={"max_sandboxes": 5},
        note="eval window",
    )
    assert created["capability"] == "custom_sandboxes"
    assert created["is_enabled"] is True
    assert created["source"] == "override"
    assert created["limits"] == {"max_sandboxes": 5}
    assert created["organization_id"] == str(org_id)

    # Reading it back through the other methods agrees.
    rows = await repo.get_entitlements(org_id)
    assert [r["capability"] for r in rows] == ["custom_sandboxes"]
    assert await repo.is_enabled(org_id, "custom_sandboxes") is True

    # An update through the same pair replaces rather than duplicates, and a
    # disabled rewrite makes the gate deny again without touching other
    # columns it was not given.
    original_updated_at = created["updated_at"]
    updated = await repo.set_entitlement(
        org_id, "custom_sandboxes", False, source="override"
    )
    assert updated["id"] == created["id"]
    assert updated["is_enabled"] is False
    assert updated["source"] == "override"
    # limits were passed as none — replaced wholesale, not merged.
    assert updated["limits"] == {}
    assert updated["created_at"] == created["created_at"]
    assert await repo.is_enabled(org_id, "custom_sandboxes") is False
    rows = await repo.get_entitlements(org_id)
    assert len(rows) == 1
    # The migration-001 updated_at trigger fired: the row moved forward.
    assert updated["updated_at"] >= original_updated_at


async def test_unknown_capability_is_rejected_by_check(db_pool, test_organization):
    repo = EntitlementRepository(db_pool)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await repo.set_entitlement(
            test_organization["id"], "managed_tols", True  # deliberate typo
        )


async def test_unknown_source_is_rejected_by_check(db_pool, test_organization):
    repo = EntitlementRepository(db_pool)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await repo.set_entitlement(
            test_organization["id"],
            "managed_tools",
            True,
            source="friendship",
        )


async def test_entitlements_are_organization_scoped(db_pool, test_organization, other_organization):
    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(test_organization["id"], "managed_tools", True)

    assert await repo.get_entitlements(other_organization["id"]) == []
    assert await repo.is_enabled(other_organization["id"], "managed_tools") is False


async def test_bulk_read_groups_by_organization(db_pool, test_organization, other_organization):
    repo = EntitlementRepository(db_pool)
    await repo.set_entitlement(test_organization["id"], "managed_tools", True)
    await repo.set_entitlement(
        test_organization["id"], "custom_sandboxes", True, source="grandfathered"
    )
    await repo.set_entitlement(other_organization["id"], "managed_tools", False)

    grouped = await repo.get_entitlements_for_organizations(
        [test_organization["id"], other_organization["id"]]
    )
    mine = [r["capability"] for r in grouped[str(test_organization["id"])]]
    theirs = [r["capability"] for r in grouped[str(other_organization["id"])]]
    assert mine == ["custom_sandboxes", "managed_tools"]
    assert theirs == ["managed_tools"]
    assert grouped[str(test_organization["id"])][0]["source"] == "grandfathered"

    # Callers pass a broader id list than orgs that have rows: every requested
    # key comes back mapped, missing ones to [].
    unrelated_org_id = str(uuid.uuid4())
    padded = await repo.get_entitlements_for_organizations([unrelated_org_id])
    assert padded == {unrelated_org_id: []}


async def test_bulk_read_with_no_ids_short_circuits(db_pool):
    repo = EntitlementRepository(db_pool)
    assert await repo.get_entitlements_for_organizations([]) == {}
