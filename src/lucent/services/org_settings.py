"""Read and write an organization's runtime settings on someone else's behalf.

The regular settings UI is scoped to the caller's own organization: it reads
``user.organization_id`` and writes through the request-scoped tenant context.
An instance operator needs the same operations *aimed at a target org* — that is
what "set and adjust org settings" for a newly created tenant means.

Everything here takes an explicit organization id and never falls back to the
caller's context, so a mis-passed id cannot silently read the operator's own
settings.

This module is also where **locks** are created. A lock is a write barrier, not
a value: the effective value still comes from ``runtime_settings`` (or its
env/default fallback), so locking a setting that has no saved row is legal and
meaningful. The usual operator flow is "set the value, then lock it", but the
two are separate calls on purpose — a tier can pin `features.sandboxes_enabled`
to its instance default without storing a row at all.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from lucent import settings as runtime_settings
from lucent.db.models import ModelRepository
from lucent.db.organization import OrganizationRepository
from lucent.db.runtime_settings import RuntimeSettingsRepository
from lucent.logging import get_logger

logger = get_logger(__name__)


class UnknownSettingError(ValueError):
    """The key is not in the admin-editable allowlist."""


class SettingNotLockedError(ValueError):
    """The setting is editable by the tenant; there is nothing to release."""


class UnknownOrganizationError(ValueError):
    """The target organization does not exist."""


async def _option_sets(pool: asyncpg.Pool, organization_id: UUID | str) -> dict[str, list[dict]]:
    """Typed option catalogs (models / decision models) for one organization."""
    result = await ModelRepository(pool).list_models(
        enabled_only=True,
        org_id=str(organization_id),
        limit=500,
    )
    models = result["items"]
    return {
        "models": [
            {
                "value": model["id"],
                "label": f"{model['name']} — {model['provider']}",
                "description": model.get("category") or "general",
            }
            for model in models
        ],
        "decision_models": [
            {
                "value": model["id"],
                "label": f"{model['name']} — {model['provider']}",
                "description": "decision model",
            }
            for model in models
            if (model.get("model_class") or "language") == "decision"
        ],
    }


async def load_settings(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    include_read_only: bool = False,
) -> dict[str, Any]:
    """Return the target organization's runtime settings, grouped by section.

    Only settings an operator is allowed to change are returned by default —
    a read-only entry (database passwords and the like) has no business in an
    operator console, even as a redacted row.
    """
    org_id = UUID(str(organization_id))

    # The in-process cache is keyed by org, so loading the target org's rows is
    # additive and does not disturb the caller's own cached values.
    await runtime_settings.load_runtime_settings_from_db(pool, organization_id=org_id)
    snapshots = runtime_settings.runtime_setting_snapshots(
        org_id, option_sets=await _option_sets(pool, org_id)
    )

    settings: list[dict[str, Any]] = []
    sections: dict[str, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        definition = snapshot["definition"]
        if not include_read_only and not definition.editable:
            continue
        entry = _snapshot_entry(snapshot)
        settings.append(entry)
        sections.setdefault(definition.section, []).append(entry)

    return {
        "organization_id": org_id,
        "settings": settings,
        "sections": sections,
    }


def _snapshot_entry(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Serialize one settings snapshot into the wire shape.

    ``value`` is the raw value so a read and a write describe the same thing and
    a caller can round-trip it; ``display_value`` is the human-readable form.
    ``locked`` is the operator-relevant fact: a locked setting is one this
    console controls and the tenant does not. ``lock_reason`` is free text
    rendered to the tenant in place of the editing controls.
    """
    definition = snapshot["definition"]
    lock = snapshot.get("lock") or {}
    return {
        "key": definition.key,
        "value": snapshot["value"],
        "value_type": definition.value_type,
        "source": snapshot["source"],
        "title": definition.title,
        "section": definition.section,
        "display_value": snapshot["display_value"],
        "locked": bool(snapshot.get("locked")),
        "lock_reason": lock.get("reason") or None,
        "locked_by_name": lock.get("locked_by_name"),
        "locked_at": lock.get("locked_at"),
    }


async def _lock_entry(
    pool: asyncpg.Pool,
    organization_id: UUID,
    key: str,
) -> dict[str, Any]:
    """Re-read one setting after a lock change and return its wire entry."""
    await runtime_settings.load_runtime_setting_locks_from_db(
        pool, organization_id=organization_id
    )
    option_sets = await _option_sets(pool, organization_id)
    snapshot = next(
        item
        for item in runtime_settings.runtime_setting_snapshots(
            organization_id, option_sets=option_sets
        )
        if item["definition"].key == key
    )
    return _snapshot_entry(snapshot)


def describe_setting(key: str) -> dict[str, Any]:
    """Metadata for one editable setting, or raise :class:`UnknownSettingError`."""
    definition = runtime_settings.get_runtime_setting_definition(key)
    if definition is None or not definition.editable:
        raise UnknownSettingError(f"Unknown or read-only setting: {key}")
    return {
        "key": definition.key,
        "title": definition.title,
        "section": definition.section,
        "value_type": definition.value_type,
        "env_var": definition.env_var,
        "description": definition.description,
        "help_text": definition.help_text,
        "min_value": definition.min_value,
        "max_value": definition.max_value,
        "choices": list(definition.choices),
    }


async def update_setting(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    key: str,
    *,
    value: Any = None,
    reset: bool = False,
    actor_user_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Set or reset one setting for a target organization.

    This is the *operator* path, so it is allowed to write a locked setting: the
    same code creates the locks, and "pin this tenant to a value" is exactly
    "write it even though it is locked". Tenant-facing writes go through the web
    routes, which deliberately do not have this bypass.

    Args:
        value: The new value. Coerced and range-checked by the same validator
            the settings UI uses, so an operator cannot store a value the UI
            would reject.
        reset: Delete the DB override instead, falling back to env/default.
        actor_user_id: Recorded as ``updated_by`` on the row.

    Raises:
        UnknownSetting: If the key is not editable.
        ValueError: If the value fails validation.
    """
    definition_meta = describe_setting(key)
    definition = runtime_settings.get_runtime_setting_definition(key)
    assert definition is not None  # describe_setting already proved this

    org_id = UUID(str(organization_id))
    repo = RuntimeSettingsRepository(pool)
    previous = runtime_settings.get_runtime_setting(key, organization_id=org_id)
    previous_source = runtime_settings.get_runtime_setting_source(
        key, organization_id=org_id
    )

    if reset:
        await repo.delete_setting(org_id, key, ignore_lock=True)
        runtime_settings.clear_runtime_setting_cache(org_id, key)
    else:
        coerced = runtime_settings.validate_runtime_setting_value(key, value)
        runtime_settings.validate_runtime_setting_semantics(
            key, coerced, await _option_sets(pool, org_id)
        )
        await repo.upsert_setting(
            organization_id=org_id,
            key=key,
            value=coerced,
            value_type=definition.value_type,
            user_id=actor_user_id,
            ignore_lock=True,
        )
        runtime_settings.set_runtime_setting_cache(org_id, key, coerced)

    return {
        "key": key,
        "previous_value": previous,
        "previous_source": previous_source,
        "value": runtime_settings.get_runtime_setting(key, organization_id=org_id),
        "source": runtime_settings.get_runtime_setting_source(key, organization_id=org_id),
        "title": definition_meta["title"],
        # Ready-to-serialize shape, matching one entry of ``load_settings``. The
        # lock state is re-read because the operator may have just written a
        # setting that is pinned, and the entry must still report it as locked.
        "entry": await _lock_entry(pool, org_id, key),
    }


async def lock_setting(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    key: str,
    *,
    reason: str = "",
    actor_user_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Pin one setting so the tenant's admins and owners can no longer change it.

    Locking does **not** write a value. The effective value keeps coming from
    ``runtime_settings`` or its env/default fallback, which is what makes the
    lock unambiguous: there is exactly one place a value can live. The usual
    flow is "set the value, then lock it", but pinning a setting that has no row
    is legal and meaningful — it freezes whatever the instance currently
    resolves to.

    Re-locking updates the reason and the operator, which is how a pin is
    re-justified after a plan change.

    Raises:
        UnknownSettingError: If the key is not in the editable allowlist.
    """
    describe_setting(key)  # raises UnknownSettingError for unknown/read-only keys
    org_id = UUID(str(organization_id))
    repo = RuntimeSettingsRepository(pool)
    previous = await repo.get_lock(org_id, key)
    row = await repo.lock_setting(
        organization_id=org_id,
        key=key,
        user_id=actor_user_id,
        reason=(reason or "").strip(),
    )

    entry = await _lock_entry(pool, org_id, key)
    # ``set_runtime_setting_lock_cache`` is what the tenant web process would
    # need, but that is a different process: this call only keeps *this*
    # process's rendering cache truthful, never the barrier.
    runtime_settings.set_runtime_setting_lock_cache(
        org_id,
        key,
        {
            "reason": row["reason"] or "",
            "locked_by": row["locked_by"],
            "locked_by_name": entry.get("locked_by_name"),
            "locked_at": row["locked_at"],
        },
    )

    return {
        "key": key,
        "title": entry["title"],
        "reason": row["reason"] or "",
        "already_locked": previous is not None,
        "previous_reason": (previous or {}).get("reason") or "",
        "lock": row,
        "entry": entry,
    }


async def unlock_setting(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    key: str,
    *,
    actor_user_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Hand a pinned setting back to the tenant.

    Unlocking deliberately leaves the stored value alone: a lock is a barrier on
    *who may write*, not a saved value. Releasing it must not silently change
    what the tenant experiences, only who can change it next.

    Raises:
        UnknownSettingError: If the key is not in the editable allowlist.
        SettingNotLockedError: If nothing was pinned, so the caller can tell the
            difference between "unlocked" and "was not locked".
    """
    describe_setting(key)
    org_id = UUID(str(organization_id))
    repo = RuntimeSettingsRepository(pool)
    row = await repo.unlock_setting(org_id, key)
    if row is None:
        raise SettingNotLockedError(f"Setting {key} is not locked.")

    entry = await _lock_entry(pool, org_id, key)
    runtime_settings.set_runtime_setting_lock_cache(org_id, key, None)

    return {
        "key": key,
        "title": entry["title"],
        "previous_reason": row.get("reason") or "",
        "entry": entry,
    }


# ---------------------------------------------------------------------------
# Bulk / tier application
# ---------------------------------------------------------------------------
#
# Tier definitions live in an outside application, so this module supplies only
# the primitives that app needs to apply a tier atomically enough to be safe.
# Two deliberate shape decisions:
#
# * **The lock set is declared in full, not as a delta.** ``apply_lock_set`` takes
#   the complete set a tier wants pinned and reconciles to it — locking what is
#   missing, re-reasoning what changed, releasing what the tier no longer
#   includes. A delta API would put the reconciliation in the caller and let two
#   callers disagree about what "the tier" is; declaring the target state makes
#   the call idempotent, which matters when a webhook fires twice.
# * **Per-item failure, not all-or-nothing.** A tier that names one retired
#   setting should still apply the other twenty and report the bad one, otherwise
#   a single catalog change bricks the caller's whole flow until they redeploy.
#   Validation therefore happens before any write, and the writes themselves are
#   single SQL statements per direction.


async def apply_lock_set(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    entries: list[dict[str, Any]],
    actor_user_id: UUID | str | None = None,
    release_unlisted: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reconcile one organization's lock set to exactly ``entries``.

    Args:
        entries: ``[{"key": ..., "reason": ..., "value": ...}]``. ``reason`` is
            shown to the tenant; ``value`` is optional and, when present, is
            written before the lock so the tenant is pinned to that value.
        release_unlisted: Release locks that exist but are not in ``entries``.
            This is what makes a repeated call converge rather than accumulate.
        dry_run: Validate and report without writing anything. Callers use this
            to diff a tier against live state before applying it.

    Returns:
        ``{"locked", "updated", "values_written", "released", "unchanged",
        "failed", "dry_run"}`` where the first four are lists of setting keys and
        ``failed`` is a list of ``{"key", "error"}``.

        ``updated`` means the *lock* changed (its reason), and ``values_written``
        means the *value* changed. They are separate because a plan can change one
        without the other, and a caller reconciling a tier needs to tell those
        apart.

    Raises:
        UnknownOrganizationError: If the organization does not exist.
    """
    org_id = UUID(str(organization_id))
    repo = RuntimeSettingsRepository(pool)

    org = await OrganizationRepository(pool).get_by_id(org_id)
    if org is None:
        raise UnknownOrganizationError(f"Organization {org_id} not found.")

    current = {row["key"]: row for row in await repo.list_locks(org_id)}
    # Stored overrides, only to decide whether a requested value is a change.
    # A key absent from this map currently inherits the env/default fallback.
    stored = {
        row["key"]: row["value"] for row in await repo.list_settings(organization_id=org_id)
    }

    failed: list[dict[str, str]] = []
    wanted: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = str(entry.get("key") or "").strip()
        if not key:
            failed.append({"key": "", "error": "A setting key is required."})
            continue
        if key in wanted:
            failed.append({"key": key, "error": "Setting listed more than once."})
            continue
        try:
            define = describe_setting(key)  # rejects unknown and read-only keys
            coerced: Any = entry.get("value")
            if coerced is not None:
                coerced = runtime_settings.validate_runtime_setting_value(key, coerced)
                runtime_settings.validate_runtime_setting_semantics(
                    key, coerced, await _option_sets(pool, org_id)
                )
        except (UnknownSettingError, ValueError) as exc:
            failed.append({"key": key, "error": str(exc)})
            continue
        wanted[key] = {
            "key": key,
            "title": define["title"],
            "reason": str(entry.get("reason") or "").strip(),
            "value": coerced,
        }

    # Two independent decisions per setting, because they answer different
    # questions and a tier can want either alone:
    #
    #   * ``to_write`` — a value was supplied and differs from what is stored;
    #   * ``to_lock``  — the setting is not pinned yet, or its reason changed.
    #
    # They must not be folded together. A tier that re-sends the same reason with
    # a new value (a price/limit change on an unchanged plan) is a value write
    # with no lock change, and classifying it as "unchanged" would silently skip
    # the write — the tenant would keep the old limit while the caller was told
    # everything was already correct.
    to_write = {
        key: item["value"]
        for key, item in wanted.items()
        if item["value"] is not None
        and (key not in stored or stored[key] != item["value"])
    }
    to_lock = [
        item
        for key, item in wanted.items()
        if key not in current or (current[key].get("reason") or "") != item["reason"]
    ]
    to_lock_keys = {item["key"] for item in to_lock}
    updated = [item["key"] for item in to_lock if item["key"] in current]
    locked = [item["key"] for item in to_lock if item["key"] not in current]
    # ``to_lock`` holds dicts, so membership must be tested against a key set:
    # comparing a key to the list directly is always False and would report one
    # setting as both updated and unchanged.
    unchanged = [
        key
        for key in wanted
        if key in current
        and key not in to_lock_keys
        and key not in to_write
    ]
    to_release = (
        [key for key in current if key not in wanted] if release_unlisted else []
    )

    if dry_run:
        return {
            "organization_id": org_id,
            "dry_run": True,
            "locked": locked,
            "updated": updated,
            "values_written": sorted(to_write),
            "released": to_release,
            "unchanged": unchanged,
            "failed": failed,
        }

    # Values before locks: a caller that says "pin this to X" must not be able to
    # end up with a lock on a value it never wrote. Operator writes bypass the
    # barrier because this is the code that owns the barriers.
    written: list[str] = []
    for key, value in to_write.items():
        try:
            await update_setting(
                pool, org_id, key, value=value, actor_user_id=actor_user_id
            )
            written.append(key)
        except (UnknownSettingError, ValueError) as exc:
            failed.append({"key": key, "error": str(exc)})

    # A setting whose value write failed must not be locked: that would pin the
    # tenant to exactly the stale value the caller was trying to replace.
    rejected = {item["key"] for item in failed}
    applied = [item for item in to_lock if item["key"] not in rejected]
    if applied:
        await repo.lock_settings_bulk(
            organization_id=org_id,
            entries=[{"key": item["key"], "reason": item["reason"]} for item in applied],
            user_id=actor_user_id,
        )
    if to_release:
        await repo.unlock_settings_bulk(org_id, to_release)

    # Refresh this process's render-only caches for the affected org.
    await runtime_settings.load_runtime_setting_locks_from_db(pool, organization_id=org_id)

    return {
        "organization_id": org_id,
        "dry_run": False,
        "locked": [item["key"] for item in applied if item["key"] in locked],
        "updated": [item["key"] for item in applied if item["key"] in updated],
        "values_written": sorted(written),
        "released": to_release,
        "unchanged": [key for key in unchanged if key not in rejected],
        "failed": failed,
    }


async def release_lock_set(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    keys: list[str],
) -> dict[str, Any]:
    """Release several named locks, reporting which were actually present.

    Unlike :func:`apply_lock_set` this does not reconcile — it releases exactly
    what it is given. That is the right shape for "undo this tier": the caller
    names the settings the tier pinned, and anything an operator pinned by hand
    survives.
    """
    org_id = UUID(str(organization_id))
    repo = RuntimeSettingsRepository(pool)
    for key in keys:
        describe_setting(key)  # unknown keys are a caller bug, not a partial success

    released = await repo.unlock_settings_bulk(org_id, keys)
    if released:
        await runtime_settings.load_runtime_setting_locks_from_db(
            pool, organization_id=org_id
        )
        for row in released:
            runtime_settings.set_runtime_setting_lock_cache(org_id, row["key"], None)

    released_keys = [row["key"] for row in released]
    return {
        "organization_id": org_id,
        "released": released_keys,
        "not_locked": [key for key in keys if key not in released_keys],
    }


async def monitor_locks(
    pool: asyncpg.Pool,
    *,
    key: str | None = None,
) -> dict[str, Any]:
    """Every lock on the instance, grouped by setting and by organization.

    This is the monitoring half of the feature: an outside application that
    assigns tiers still needs to answer "which tenants are pinned to what", and
    doing that by iterating tenants over HTTP would be O(tenants) round trips for
    a single dashboard.
    """
    rows = await RuntimeSettingsRepository(pool).list_locks_with_values(key=key)

    by_setting: dict[str, list[dict[str, Any]]] = {}
    by_organization: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        definition = runtime_settings.get_runtime_setting_definition(row["key"])
        entry = {
            "organization_id": row["organization_id"],
            "organization_name": row.get("organization_name"),
            "key": row["key"],
            "title": definition.title if definition else None,
            "section": definition.section if definition else None,
            "reason": row.get("reason") or "",
            "db_value": row.get("db_value"),
            "locked_by": row.get("locked_by"),
            "locked_by_name": row.get("locked_by_name"),
            "locked_at": row.get("locked_at"),
        }
        by_setting.setdefault(row["key"], []).append(entry)
        by_organization.setdefault(str(row["organization_id"]), []).append(entry)

    return {
        "total": len(rows),
        "locks": rows,
        "by_setting": by_setting,
        "by_organization": by_organization,
    }
