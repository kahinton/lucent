"""Read and write an organization's runtime settings on someone else's behalf.

The regular settings UI is scoped to the caller's own organization: it reads
``user.organization_id`` and writes through the request-scoped tenant context.
An instance operator needs the same operations *aimed at a target org* — that is
what "set and adjust org settings" for a newly created tenant means.

Everything here takes an explicit organization id and never falls back to the
caller's context, so a mis-passed id cannot silently read the operator's own
settings.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from lucent import settings as runtime_settings
from lucent.db.models import ModelRepository
from lucent.db.runtime_settings import RuntimeSettingsRepository
from lucent.logging import get_logger

logger = get_logger(__name__)


class UnknownSettingError(ValueError):
    """The key is not in the admin-editable allowlist."""


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
        entry = {
            "key": definition.key,
            # The raw value, so a read and a write describe the same thing and
            # a caller can round-trip it. The human-readable form is separate.
            "value": snapshot["value"],
            "value_type": definition.value_type,
            "source": snapshot["source"],
            "title": definition.title,
            "section": definition.section,
            "display_value": snapshot["display_value"],
        }
        settings.append(entry)
        sections.setdefault(definition.section, []).append(entry)

    return {
        "organization_id": org_id,
        "settings": settings,
        "sections": sections,
    }


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
        await repo.delete_setting(org_id, key)
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
        )
        runtime_settings.set_runtime_setting_cache(org_id, key, coerced)

    return {
        "key": key,
        "previous_value": previous,
        "previous_source": previous_source,
        "value": runtime_settings.get_runtime_setting(key, organization_id=org_id),
        "source": runtime_settings.get_runtime_setting_source(key, organization_id=org_id),
        "title": definition_meta["title"],
        # Ready-to-serialize shape, matching one entry of ``load_settings``.
        "entry": {
            "key": key,
            "value": runtime_settings.get_runtime_setting(key, organization_id=org_id),
            "value_type": definition.value_type,
            "source": runtime_settings.get_runtime_setting_source(
                key, organization_id=org_id
            ),
            "title": definition_meta["title"],
            "section": definition_meta["section"],
            "display_value": _display(
                key, runtime_settings.get_runtime_setting(key, organization_id=org_id)
            ),
        },
    }


def _display(key: str, value: Any) -> str | None:
    """Human-readable form of a setting value (mirrors the settings UI)."""
    from lucent import settings as runtime_settings

    definition = runtime_settings.get_runtime_setting_definition(key)
    if definition is None:
        return None
    try:
        return runtime_settings._display_value(definition, value)  # noqa: SLF001
    except Exception:  # noqa: BLE001 — display is cosmetic
        return str(value)
