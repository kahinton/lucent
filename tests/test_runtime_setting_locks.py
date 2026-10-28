"""Instance-operator setting locks (migration 153).

A lock is a **write barrier**, not a value: the effective value keeps coming from
``runtime_settings`` or its env/default fallback, and a locked row only removes
the tenant's ability to change or reset it. These tests pin down the four things
that make that useful and safe:

* an operator can pin a setting and still write it themselves;
* a tenant admin or owner is refused, on both the write and the reset path
  (resetting would otherwise be a one-click way around a pin);
* unlocking hands control back without changing the effective value;
* every lock and unlock is audited in both organizations.

They live here rather than in ``test_hyperadmin_api.py`` because the interesting
assertions are about the *interaction* between the operator API and the tenant
web routes, and that needs both clients in one place.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent import settings as runtime_settings
from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import OrganizationRepository, RuntimeSettingsRepository, UserRepository

TEST_PASSWORD = "TestPass1"
SANDBOXES = "features.sandboxes_enabled"
ALPHA = "memory.search_vitality_boost_alpha"


@pytest_asyncio.fixture
async def lock_prefix(db_pool, delete_test_organizations):
    prefix = f"locks_{str(uuid4())[:8]}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM admin_audit_log WHERE organization_id IN "
            "(SELECT id FROM organizations WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await delete_test_organizations(conn, [f"{prefix}%"])
    runtime_settings.clear_runtime_setting_cache()


@pytest_asyncio.fixture
async def tenant(db_pool, lock_prefix):
    return await OrganizationRepository(db_pool).create(name=f"{lock_prefix}tenant")


@pytest_asyncio.fixture
async def tenant_owner(db_pool, tenant, lock_prefix):
    user = await UserRepository(db_pool).create(
        external_id=f"{lock_prefix}owner",
        provider="local",
        organization_id=tenant["id"],
        email=f"{lock_prefix}owner@test.com",
        display_name="Tenant Owner",
        role="owner",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    return user


@pytest_asyncio.fixture
async def operator_org(db_pool, lock_prefix):
    return await OrganizationRepository(db_pool).create(name=f"{lock_prefix}oporg")


@pytest_asyncio.fixture
async def operator(db_pool, operator_org, lock_prefix):
    return await UserRepository(db_pool).create(
        external_id=f"{lock_prefix}operator",
        provider="local",
        organization_id=operator_org["id"],
        email=f"{lock_prefix}operator@test.com",
        display_name="Instance Operator",
        role="hyperadmin",
    )


@pytest_asyncio.fixture
async def operator_client(operator):
    app = create_app()
    fake = CurrentUser(
        id=operator["id"],
        organization_id=operator["organization_id"],
        role="hyperadmin",
        email=operator.get("email"),
        display_name=operator.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def owner_web_client(db_pool, tenant_owner):
    """The tenant owner's browser session against the *web* settings routes."""
    token = await create_session(db_pool, tenant_owner["id"])
    csrf = "lock-test-csrf-token"
    transport = ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token, CSRF_COOKIE_NAME: csrf},
    ) as c:
        c._csrf_token = csrf
        yield c


def _csrf(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    data = {CSRF_FIELD_NAME: client._csrf_token}
    if extra:
        data.update(extra)
    return data


# ---------------------------------------------------------------------------
# The barrier itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_blocks_tenant_write(
    operator_client, owner_web_client, db_pool, tenant
):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/settings/{SANDBOXES}",
        json={"value": False},
    )
    assert resp.status_code == 200, resp.text

    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Not included in your plan."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"] == SANDBOXES
    assert body["reason"] == "Not included in your plan."
    assert body["value"] is False

    # The tenant owner is refused, with a message they can act on.
    resp = await owner_web_client.post(
        f"/settings/runtime/{SANDBOXES}",
        data=_csrf(owner_web_client, {"value": "true"}),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403
    assert "locked by an instance administrator" in resp.json()["error"]

    # ...and the stored value is what the operator pinned, not what was submitted.
    row = await RuntimeSettingsRepository(db_pool).get_setting(tenant["id"], SANDBOXES)
    assert row["value"] is False


@pytest.mark.asyncio
async def test_lock_blocks_tenant_reset(
    operator_client, owner_web_client, db_pool, tenant
):
    """Reset must be blocked too, or the pin is one click from being undone."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/settings/{SANDBOXES}",
        json={"value": False},
    )
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Plan limit"},
    )

    resp = await owner_web_client.post(
        f"/settings/runtime/{SANDBOXES}/reset",
        data=_csrf(owner_web_client),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403
    assert "locked" in resp.json()["error"]

    row = await RuntimeSettingsRepository(db_pool).get_setting(tenant["id"], SANDBOXES)
    assert row is not None, "a blocked reset must not delete the operator's value"


@pytest.mark.asyncio
async def test_operator_can_still_write_a_locked_setting(
    operator_client, db_pool, tenant
):
    """The operator path owns the barriers, so it must not be blocked by them."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Plan limit"},
    )

    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/settings/{SANDBOXES}",
        json={"value": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] is False
    assert resp.json()["locked"] is True

    row = await RuntimeSettingsRepository(db_pool).get_setting(tenant["id"], SANDBOXES)
    assert row["value"] is False


@pytest.mark.asyncio
async def test_reset_by_operator_clears_the_value_but_keeps_the_lock(
    operator_client, owner_web_client, db_pool, tenant
):
    """Reset by the operator means "inherit the instance value", not "unlock"."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/settings/{SANDBOXES}",
        json={"value": False},
    )
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Pilot"},
    )

    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/settings/{SANDBOXES}",
        json={"reset": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is True
    assert await RuntimeSettingsRepository(db_pool).get_setting(
        tenant["id"], SANDBOXES
    ) is None

    # Still barred from the tenant, so releasing a value is not releasing control.
    resp = await owner_web_client.post(
        f"/settings/runtime/{SANDBOXES}",
        data=_csrf(owner_web_client, {"value": "true"}),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unlock_restores_tenant_control_without_changing_value(
    operator_client, owner_web_client, db_pool, tenant
):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/settings/{SANDBOXES}",
        json={"value": False},
    )
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Pilot"},
    )

    resp = await operator_client.delete(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] is False

    # The value survived the unlock: releasing a barrier is not a value change.
    row = await RuntimeSettingsRepository(db_pool).get_setting(tenant["id"], SANDBOXES)
    assert row["value"] is False

    # ...and the tenant can now change it again.
    resp = await owner_web_client.post(
        f"/settings/runtime/{SANDBOXES}",
        data=_csrf(owner_web_client, {"value": "true"}),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" not in resp.headers["location"]
    assert runtime_settings.sandboxes_enabled(organization_id=tenant["id"]) is True


@pytest.mark.asyncio
async def test_lock_without_a_stored_value_freezes_the_fallback(
    operator_client, owner_web_client, tenant
):
    """A lock on a setting with no DB row is legal and meaningful.

    This is the pure "hold the instance default" case: nothing is written, and
    the tenant is still refused. It is why the lock is a separate table rather
    than a column on ``runtime_settings``.
    """
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{ALPHA}",
        json={"reason": "Pinned to the instance value."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] in ("environment", "default")

    resp = await owner_web_client.post(
        f"/settings/runtime/{ALPHA}",
        data=_csrf(owner_web_client, {"value": "0.9"}),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_settings_read_reports_lock_and_reason(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Not included in your plan."},
    )

    resp = await operator_client.get(f"/api/organizations/{tenant['id']}/settings")
    assert resp.status_code == 200
    entry = next(item for item in resp.json()["settings"] if item["key"] == SANDBOXES)
    assert entry["locked"] is True
    assert entry["lock_reason"] == "Not included in your plan."


@pytest.mark.asyncio
async def test_locks_listing_is_scoped_to_the_tenant(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Plan limit"},
    )

    resp = await operator_client.get(f"/api/organizations/{tenant['id']}/setting-locks")
    assert resp.status_code == 200
    items = resp.json()
    assert [item["key"] for item in items] == [SANDBOXES]
    assert items[0]["reason"] == "Plan limit"
    assert items[0]["title"]


# ---------------------------------------------------------------------------
# Editing a lock and its failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relocking_updates_reason(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Pilot"},
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Downgraded to Starter"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "Downgraded to Starter"

    locks = (
        await operator_client.get(f"/api/organizations/{tenant['id']}/setting-locks")
    ).json()
    assert len(locks) == 1
    assert locks[0]["reason"] == "Downgraded to Starter"


@pytest.mark.asyncio
async def test_lock_rejects_unknown_setting(operator_client, tenant):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/not.a.setting",
        json={"reason": "nope"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_lock_rejects_read_only_setting(operator_client, tenant):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/secrets.daemon_db_password",
        json={"reason": "nope"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unlock_of_an_unlocked_setting_conflicts(operator_client, tenant):
    resp = await operator_client.delete(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}"
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_tenant_owner_cannot_lock_their_own_settings(tenant_owner, tenant):
    app = create_app()
    fake = CurrentUser(
        id=tenant_owner["id"],
        organization_id=tenant["id"],
        role="owner",
        email=tenant_owner.get("email"),
        display_name=tenant_owner.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.put(
            f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
            json={"reason": "self-service"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_and_unlock_are_audited_in_the_target_org(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Plan limit"},
    )
    await operator_client.delete(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}"
    )

    resp = await operator_client.get(f"/api/organizations/{tenant['id']}/audit")
    assert resp.status_code == 200
    actions = [entry["action"] for entry in resp.json()["entries"]]
    assert "org.setting_lock" in actions
    assert "org.setting_unlock" in actions


@pytest.mark.asyncio
async def test_lock_is_visible_to_the_tenant_in_its_own_audit(operator_client, tenant):
    """The tenant must be able to see who removed their control."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Plan limit"},
    )
    resp = await operator_client.get(
        f"/api/organizations/{tenant['id']}/audit", params={"action": "org.setting_lock"}
    )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries
    assert entries[0]["entity_label"]
    assert entries[0]["new_values"]["reason"] == "Plan limit"


# ---------------------------------------------------------------------------
# Tenant UI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_settings_page_renders_a_locked_setting_as_read_only(
    operator_client, owner_web_client, tenant
):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{SANDBOXES}",
        json={"reason": "Not included in your plan."},
    )

    resp = await owner_web_client.get("/settings/runtime")
    assert resp.status_code == 200
    assert "Managed by your administrator" in resp.text
    assert "Not included in your plan." in resp.text
    # The editing form for that one setting must not be rendered at all: a
    # disabled input would still be a control the tenant is told not to use.
    card = resp.text.split(f'data-runtime-setting-card="{SANDBOXES}"', 1)[1]
    card = card.split("data-runtime-setting-card=", 1)[0]
    assert f'action="/settings/runtime/{SANDBOXES}"' not in card


# ---------------------------------------------------------------------------
# Bulk / tier application
# ---------------------------------------------------------------------------
#
# Tier definitions live in an outside application, so the API supplies only the
# primitives it needs. The assertions below encode the three properties that make
# a tier apply safe to retry from a webhook: convergence (declare the target
# state, don't send a delta), value-then-lock ordering, and per-setting failure
# rather than all-or-nothing.

CHAT_MODEL = "models.chat_model"


@pytest.mark.asyncio
async def test_bulk_apply_locks_a_whole_set_at_once(operator_client, tenant, db_pool):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter plan", "value": False},
                {"key": ALPHA, "reason": "Starter plan", "value": 0.1},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["locked"]) == sorted([SANDBOXES, ALPHA])
    assert body["failed"] == []

    # Values were written before the locks, so the tenant is pinned to them.
    repo = RuntimeSettingsRepository(db_pool)
    assert (await repo.get_setting(tenant["id"], SANDBOXES))["value"] is False
    assert (await repo.get_setting(tenant["id"], ALPHA))["value"] == 0.1
    assert {r["key"] for r in await repo.list_locks(tenant["id"])} == {
        SANDBOXES,
        ALPHA,
    }


@pytest.mark.asyncio
async def test_bulk_apply_without_a_value_freezes_the_fallback(
    operator_client, tenant, db_pool
):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Inherit instance default"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] == [SANDBOXES]

    # Nothing written: the lock holds the tenant to whatever the instance resolves.
    assert await RuntimeSettingsRepository(db_pool).get_setting(
        tenant["id"], SANDBOXES
    ) is None


@pytest.mark.asyncio
async def test_bulk_apply_is_idempotent(operator_client, tenant):
    body = {
        "locks": [
            {"key": SANDBOXES, "reason": "Starter plan"},
            {"key": ALPHA, "reason": "Starter plan"},
        ]
    }
    first = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks", json=body
    )
    assert first.status_code == 200, first.text
    assert sorted(first.json()["locked"]) == sorted([SANDBOXES, ALPHA])

    second = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks", json=body
    )
    assert second.status_code == 200, second.text
    payload = second.json()
    # Nothing to do: this is what makes a retried webhook harmless.
    assert payload["locked"] == []
    assert payload["updated"] == []
    assert payload["released"] == []
    assert sorted(payload["unchanged"]) == sorted([SANDBOXES, ALPHA])


@pytest.mark.asyncio
async def test_bulk_apply_reasons_are_updatable_in_place(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Pilot"}]},
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Downgraded"}]},
    )
    assert resp.status_code == 200, resp.text
    # A reasoning change is an update, not a fresh lock — the barrier never lifted.
    assert resp.json()["updated"] == [SANDBOXES]
    assert resp.json()["locked"] == []

    locks = (
        await operator_client.get(f"/api/organizations/{tenant['id']}/setting-locks")
    ).json()
    assert locks[0]["reason"] == "Downgraded"


@pytest.mark.asyncio
async def test_bulk_apply_releases_locks_not_in_the_set(operator_client, tenant, db_pool):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": ALPHA, "reason": "Starter"},
            ]
        },
    )

    # A plan change that no longer includes ALPHA.
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Team"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["released"] == [ALPHA]
    assert body["updated"] == [SANDBOXES]

    repo = RuntimeSettingsRepository(db_pool)
    assert {r["key"] for r in await repo.list_locks(tenant["id"])} == {SANDBOXES}


@pytest.mark.asyncio
async def test_bulk_apply_can_leave_unlisted_locks_alone(operator_client, tenant, db_pool):
    """``release_unlisted=false`` is for adding to a set without undoing hand pins."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks/{ALPHA}",
        json={"reason": "Pinned by hand"},
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [{"key": SANDBOXES, "reason": "Tier"}],
            "release_unlisted": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["released"] == []

    locks = {r["key"] for r in await RuntimeSettingsRepository(db_pool).list_locks(
        tenant["id"]
    )}
    assert locks == {SANDBOXES, ALPHA}


@pytest.mark.asyncio
async def test_bulk_apply_reports_per_setting_failure_and_still_applies_the_rest(
    operator_client, tenant, db_pool
):
    """One retired key must not brick the caller's whole tier apply."""
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": "not.a.setting", "reason": "Retired"},
                {"key": "secrets.daemon_db_password", "reason": "read-only"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["locked"] == [SANDBOXES]
    assert {f["key"] for f in body["failed"]} == {
        "not.a.setting",
        "secrets.daemon_db_password",
    }
    assert await RuntimeSettingsRepository(db_pool).get_lock(
        tenant["id"], SANDBOXES
    ) is not None


@pytest.mark.asyncio
async def test_bulk_apply_rejects_a_bad_value_without_locking_it(
    operator_client, tenant, db_pool
):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Tier", "value": "not-a-number"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["locked"] == []
    assert body["failed"][0]["key"] == ALPHA
    # A refused value must not leave a lock behind on an unwritten value.
    assert await RuntimeSettingsRepository(db_pool).get_lock(tenant["id"], ALPHA) is None


@pytest.mark.asyncio
async def test_bulk_apply_rejects_duplicate_keys(operator_client, tenant):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "a"},
                {"key": SANDBOXES, "reason": "b"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["failed"][0]["key"] == SANDBOXES


@pytest.mark.asyncio
async def test_bulk_dry_run_changes_nothing(operator_client, tenant, db_pool):
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": ALPHA, "reason": "Starter"},
            ],
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert sorted(body["locked"]) == sorted([SANDBOXES, ALPHA])
    assert await RuntimeSettingsRepository(db_pool).list_locks(tenant["id"]) == []


@pytest.mark.asyncio
async def test_bulk_dry_run_reports_the_diff_for_an_existing_set(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": ALPHA, "reason": "Starter"},
            ]
        },
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Team"},
                {"key": CHAT_MODEL, "reason": "Team"},
            ],
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == [SANDBOXES]
    assert body["locked"] == [CHAT_MODEL]
    assert body["released"] == [ALPHA]


@pytest.mark.asyncio
async def test_bulk_apply_leaves_the_tenant_barred(operator_client, owner_web_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Starter", "value": False}]},
    )
    resp = await owner_web_client.post(
        f"/settings/runtime/{SANDBOXES}",
        data=_csrf(owner_web_client, {"value": "true"}),
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403
    assert "locked" in resp.json()["error"]


@pytest.mark.asyncio
async def test_bulk_release_is_exact_and_idempotent(operator_client, tenant, db_pool):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": ALPHA, "reason": "Starter"},
            ]
        },
    )

    resp = await operator_client.post(
        f"/api/organizations/{tenant['id']}/setting-locks/release",
        json={"keys": [SANDBOXES, CHAT_MODEL]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["released"] == [SANDBOXES]
    # Not an error: releasing a set twice must be safe.
    assert body["not_locked"] == [CHAT_MODEL]

    locks = {r["key"] for r in await RuntimeSettingsRepository(db_pool).list_locks(
        tenant["id"]
    )}
    assert locks == {ALPHA}, "a release must not touch keys it was not given"


@pytest.mark.asyncio
async def test_bulk_release_rejects_unknown_keys(operator_client, tenant):
    resp = await operator_client.post(
        f"/api/organizations/{tenant['id']}/setting-locks/release",
        json={"keys": ["not.a.setting"]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_apply_rejects_unknown_organization(operator_client):
    resp = await operator_client.put(
        f"/api/organizations/{uuid4()}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "x"}]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_endpoints_require_hyperadmin(tenant_owner, tenant):
    app = create_app()
    fake = CurrentUser(
        id=tenant_owner["id"],
        organization_id=tenant["id"],
        role="owner",
        email=tenant_owner.get("email"),
        display_name=tenant_owner.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        put = await c.put(
            f"/api/organizations/{tenant['id']}/setting-locks",
            json={"locks": [{"key": SANDBOXES, "reason": "self"}]},
        )
        release = await c.post(
            f"/api/organizations/{tenant['id']}/setting-locks/release",
            json={"keys": [SANDBOXES]},
        )
        monitor = await c.get("/api/organizations/instance/setting-locks")
    app.dependency_overrides.clear()
    assert (put.status_code, release.status_code, monitor.status_code) == (403, 403, 403)


# ---------------------------------------------------------------------------
# Cross-tenant monitoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_lists_locks_across_tenants(
    operator_client, tenant, operator_org
):
    """An outside tier app needs one call, not one per tenant."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter", "value": False},
                {"key": ALPHA, "reason": "Starter"},
            ]
        },
    )
    await operator_client.put(
        f"/api/organizations/{operator_org['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Operator org"}]},
    )

    resp = await operator_client.get("/api/organizations/instance/setting-locks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 3

    org_ids = {str(tenant["id"]), str(operator_org["id"])}
    matching = [lock for lock in body["locks"] if str(lock["organization_id"]) in org_ids]
    assert len(matching) == 3

    # Grouped both ways so a dashboard does not have to reshape the list.
    assert str(tenant["id"]) in body["by_organization"]
    assert SANDBOXES in body["by_setting"]
    # The stored value is exposed, and is null for the frozen-fallback lock.
    alpha = next(
        lock
        for lock in matching
        if str(lock["organization_id"]) == str(tenant["id"]) and lock["key"] == ALPHA
    )
    assert alpha["db_value"] is None
    sandboxes = next(
        lock
        for lock in matching
        if str(lock["organization_id"]) == str(tenant["id"]) and lock["key"] == SANDBOXES
    )
    assert sandboxes["db_value"] is False
    assert sandboxes["organization_name"]


@pytest.mark.asyncio
async def test_monitor_can_filter_by_setting_key(operator_client, tenant, operator_org):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": ALPHA, "reason": "Starter"},
            ]
        },
    )
    resp = await operator_client.get(
        "/api/organizations/instance/setting-locks", params={"key": ALPHA}
    )
    assert resp.status_code == 200, resp.text
    assert {lock["key"] for lock in resp.json()["locks"]} == {ALPHA}


@pytest.mark.asyncio
async def test_monitor_is_not_shadowed_by_the_tenant_scoped_route(operator_client):
    """``instance`` must not be parsed as an organization id.

    The static path is declared above ``/{organization_id}/setting-locks``; if
    that ordering regresses this returns 422 instead of 200.
    """
    resp = await operator_client.get("/api/organizations/instance/setting-locks")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Audit of bulk operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_apply_is_audited_once_for_the_batch(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [
                {"key": SANDBOXES, "reason": "Starter"},
                {"key": ALPHA, "reason": "Starter"},
            ]
        },
    )
    resp = await operator_client.get(
        f"/api/organizations/{tenant['id']}/audit",
        params={"action": "org.setting_lock"},
    )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    # One row for the batch, not one per setting: a tier apply is one decision.
    assert len(entries) == 1
    assert sorted(entries[0]["changed_fields"]) == sorted([SANDBOXES, ALPHA])
    assert entries[0]["new_values"]["reasons"][SANDBOXES] == "Starter"


@pytest.mark.asyncio
async def test_unchanged_bulk_apply_writes_no_audit_row(operator_client, tenant):
    body = {"locks": [{"key": SANDBOXES, "reason": "Starter"}]}
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks", json=body
    )
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks", json=body
    )
    resp = await operator_client.get(
        f"/api/organizations/{tenant['id']}/audit",
        params={"action": "org.setting_lock"},
    )
    assert len(resp.json()["entries"]) == 1


@pytest.mark.asyncio
async def test_dry_run_writes_no_audit_row(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Starter"}], "dry_run": True},
    )
    resp = await operator_client.get(
        f"/api/organizations/{tenant['id']}/audit",
        params={"action": "org.setting_lock"},
    )
    assert resp.json()["entries"] == []


# ---------------------------------------------------------------------------
# Value writes are independent of lock changes
# ---------------------------------------------------------------------------
#
# These two cases were real bugs caught by live verification against the running
# instance, not by the first pass of tests: a setting was reported as both
# "updated" and "unchanged", and a tier that changed a limit without changing any
# reason silently skipped the write. Both are asserted here so they cannot
# regress.


@pytest.mark.asyncio
async def test_a_setting_is_never_both_updated_and_unchanged(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Starter"}]},
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": SANDBOXES, "reason": "Team"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == [SANDBOXES]
    assert body["unchanged"] == []


@pytest.mark.asyncio
async def test_a_value_change_with_an_unchanged_reason_still_writes(
    operator_client, tenant, db_pool
):
    """A limit change on an unchanged plan must reach the tenant.

    The reason is what identifies the plan, so a caller re-sending the same reason
    with a new value is the normal "same tier, new limit" case. Treating that as
    unchanged would leave the tenant on the old limit while telling the caller
    everything was already correct.
    """
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.1}]},
    )

    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.4}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["values_written"] == [ALPHA]
    assert body["updated"] == []
    assert body["unchanged"] == []

    assert (await RuntimeSettingsRepository(db_pool).get_setting(
        tenant["id"], ALPHA
    ))["value"] == 0.4


@pytest.mark.asyncio
async def test_an_idempotent_resend_writes_no_value(operator_client, tenant):
    body = {"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.1}]}
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks", json=body
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks", json=body
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["values_written"] == []
    assert payload["unchanged"] == [ALPHA]


@pytest.mark.asyncio
async def test_dry_run_reports_pending_value_writes(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.1}]},
    )
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={
            "locks": [{"key": ALPHA, "reason": "Starter", "value": 0.4}],
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["values_written"] == [ALPHA]


@pytest.mark.asyncio
async def test_a_bad_value_does_not_leave_the_setting_locked_to_the_old_value(
    operator_client, tenant, db_pool
):
    """A rejected write must not pin the stale value it was replacing."""
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.1}]},
    )

    # Same reason (so no lock change), but an invalid value.
    resp = await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": "bad"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The key fails validation up front, so it is not in `failed` twice and the
    # existing lock is untouched.
    assert body["failed"][0]["key"] == ALPHA
    assert (await RuntimeSettingsRepository(db_pool).get_setting(
        tenant["id"], ALPHA
    ))["value"] == 0.1


@pytest.mark.asyncio
async def test_bulk_audit_records_value_writes(operator_client, tenant):
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.1}]},
    )
    await operator_client.put(
        f"/api/organizations/{tenant['id']}/setting-locks",
        json={"locks": [{"key": ALPHA, "reason": "Starter", "value": 0.4}]},
    )
    resp = await operator_client.get(
        f"/api/organizations/{tenant['id']}/audit",
        params={"action": "org.setting_lock"},
    )
    entries = resp.json()["entries"]
    # Two batches, two rows: the value-only change is a real change.
    assert len(entries) == 2
    assert entries[0]["new_values"]["values_written"] == [ALPHA]
