"""Global Lucent runtime settings and feature flags.

Safe, non-secret settings are allowlisted here and can be persisted in the
database per organization. Existing environment variables remain the fallback
source when no database value exists, which keeps container/deployment defaults
working while allowing admins to manage day-to-day settings from the UI.

This module deliberately exposes synchronous accessors because settings are
read inside hot database/query paths. Database values are loaded into an
in-process cache at startup and refreshed after settings UI changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

RuntimeSettingType = Literal["boolean", "integer", "float", "string", "json"]
RuntimeSettingSource = Literal["database", "environment", "default"]


@dataclass(frozen=True)
class RuntimeSettingDefinition:
    """Metadata and validation rules for an admin-editable setting."""

    key: str
    env_var: str
    value_type: RuntimeSettingType
    default: Any
    title: str
    section: str
    description: str
    help_text: str = ""
    min_value: int | float | None = None
    max_value: int | float | None = None
    editable: bool = True


_RUNTIME_SETTING_DEFINITIONS: tuple[RuntimeSettingDefinition, ...] = (
    RuntimeSettingDefinition(
        key="memory.shadow_forget_enabled",
        env_var="LUCENT_SHADOW_FORGET_ENABLED",
        value_type="boolean",
        default=False,
        title="Shadow forgetting sidecar",
        section="Memory lifecycle",
        description="Record lifecycle scoring sidecar data while memory operations run.",
        help_text=(
            "Useful for rollout and diagnostics. Leave off unless you are "
            "evaluating memory lifecycle behavior."
        ),
    ),
    RuntimeSettingDefinition(
        key="memory.search_exclude_archived_enabled",
        env_var="LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED",
        value_type="boolean",
        default=False,
        title="Exclude archived memories from search",
        section="Memory search",
        description="Hide archived and forgotten memories from normal search results by default.",
        help_text="Callers can still explicitly include archived results when supported.",
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_enabled",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_ENABLED",
        value_type="boolean",
        default=False,
        title="Vitality-boosted search ranking",
        section="Memory search",
        description="Blend memory vitality into search ranking instead of using similarity alone.",
        help_text=(
            "This is a rollout flag. Enable after verifying vitality scores are "
            "healthy for the workspace."
        ),
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_alpha",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_ALPHA",
        value_type="float",
        default=0.15,
        title="Vitality boost weight",
        section="Memory search",
        description=(
            "Controls how strongly vitality nudges search ranking when vitality "
            "boost is enabled."
        ),
        min_value=0.0,
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_log_sample_rate",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE",
        value_type="float",
        default=0.0,
        title="Search comparison log sample rate",
        section="Observability",
        description="Fraction of searches that log old-vs-new ranking comparisons.",
        help_text=(
            "Use temporarily during rollout. 0 disables comparison logs; 1 logs "
            "every eligible search."
        ),
        min_value=0.0,
        max_value=1.0,
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_log_top_n",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_LOG_TOP_N",
        value_type="integer",
        default=10,
        title="Search comparison top-N",
        section="Observability",
        description="Number of ranked results to compare in search rollout logs.",
        min_value=1,
        max_value=50,
    ),
    RuntimeSettingDefinition(
        key="requests.daemon_auto_approve",
        env_var="LUCENT_AUTO_APPROVE",
        value_type="boolean",
        default=False,
        title="Auto-approve daemon-created requests",
        section="Requests",
        description="Let daemon/cognitive requests start without waiting for human approval.",
        help_text=(
            "User, API, and scheduled requests are already auto-approved. This "
            "only affects daemon-originated work."
        ),
    ),
    RuntimeSettingDefinition(
        key="requests.skip_post_completion_review",
        env_var="LUCENT_SKIP_POST_REVIEW",
        value_type="boolean",
        default=False,
        title="Skip automatic post-completion review",
        section="Requests",
        description=(
            "Send completed requests straight to completed instead of an "
            "internal review step."
        ),
        help_text=(
            "Keep this off for safer autonomous work; enable only when review "
            "throughput is more important than quality gates."
        ),
    ),
)

_SETTING_DEFINITIONS_BY_KEY = {d.key: d for d in _RUNTIME_SETTING_DEFINITIONS}
_runtime_settings_by_org: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def runtime_setting_definitions() -> list[RuntimeSettingDefinition]:
    """Return all safe runtime settings exposed to admins."""
    return list(_RUNTIME_SETTING_DEFINITIONS)


def get_runtime_setting_definition(key: str) -> RuntimeSettingDefinition | None:
    """Return the allowlist definition for ``key`` if it exists."""
    return _SETTING_DEFINITIONS_BY_KEY.get(key)


def _normalize_org_id(organization_id: Any | None) -> str | None:
    if organization_id is None:
        return None
    value = str(organization_id).strip()
    return value or None


def _current_organization_id() -> str | None:
    try:
        from lucent.auth import get_current_user

        user = get_current_user()
    except Exception:
        return None
    if not user:
        return None
    return _normalize_org_id(user.get("organization_id"))


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Enter true or false.")


def _coerce_runtime_value(
    definition: RuntimeSettingDefinition,
    raw: Any,
    *,
    clamp_bounds: bool = False,
) -> Any:
    if definition.value_type == "boolean":
        return _parse_bool(raw)
    if definition.value_type == "integer":
        if isinstance(raw, bool):
            raise ValueError("Enter a whole number.")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a whole number.") from exc
    elif definition.value_type == "float":
        if isinstance(raw, bool):
            raise ValueError("Enter a number.")
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a number.") from exc
    elif definition.value_type == "string":
        value = str(raw)
    elif definition.value_type == "json":
        value = raw
    else:
        raise ValueError("Unsupported setting type.")

    if definition.value_type in {"integer", "float"}:
        if definition.min_value is not None and value < definition.min_value:
            if clamp_bounds:
                value = definition.min_value
            else:
                raise ValueError(f"Value must be at least {definition.min_value}.")
        if definition.max_value is not None and value > definition.max_value:
            if clamp_bounds:
                value = definition.max_value
            else:
                raise ValueError(f"Value must be at most {definition.max_value}.")

    return value


def validate_runtime_setting_value(key: str, raw: Any) -> Any:
    """Validate and coerce a user-provided runtime setting value."""
    definition = get_runtime_setting_definition(key)
    if not definition or not definition.editable:
        raise ValueError("Unknown or read-only setting.")
    return _coerce_runtime_value(definition, raw, clamp_bounds=False)


def _fallback_value(definition: RuntimeSettingDefinition) -> tuple[Any, RuntimeSettingSource]:
    raw = os.environ.get(definition.env_var)
    if raw is None:
        return definition.default, "default"
    try:
        return _coerce_runtime_value(definition, raw, clamp_bounds=True), "environment"
    except ValueError:
        return definition.default, "default"


def _org_cache(organization_id: Any | None = None) -> tuple[str | None, dict[str, Any]]:
    org_id = _normalize_org_id(organization_id) or _current_organization_id()
    if not org_id:
        return None, {}
    return org_id, _runtime_settings_by_org.get(org_id, {})


def get_runtime_setting(
    key: str,
    *,
    organization_id: Any | None = None,
) -> Any:
    """Return a setting value using DB → env → default precedence."""
    definition = get_runtime_setting_definition(key)
    if not definition:
        raise KeyError(f"Unknown runtime setting: {key}")
    _org_id, cached = _org_cache(organization_id)
    if key in cached:
        try:
            return _coerce_runtime_value(definition, cached[key], clamp_bounds=True)
        except ValueError:
            pass
    value, _source = _fallback_value(definition)
    return value


def get_runtime_setting_source(
    key: str,
    *,
    organization_id: Any | None = None,
) -> RuntimeSettingSource:
    """Return which source currently supplies ``key``."""
    definition = get_runtime_setting_definition(key)
    if not definition:
        raise KeyError(f"Unknown runtime setting: {key}")
    _org_id, cached = _org_cache(organization_id)
    if key in cached:
        return "database"
    _value, source = _fallback_value(definition)
    return source


def set_runtime_setting_cache(
    organization_id: Any,
    key: str,
    value: Any,
) -> None:
    """Update the in-process DB settings cache for one setting."""
    definition = get_runtime_setting_definition(key)
    if not definition:
        return
    org_id = _normalize_org_id(organization_id)
    if not org_id:
        return
    _runtime_settings_by_org.setdefault(org_id, {})[key] = _coerce_runtime_value(
        definition,
        value,
        clamp_bounds=True,
    )


def clear_runtime_setting_cache(organization_id: Any | None = None, key: str | None = None) -> None:
    """Clear cached DB setting values.

    With no arguments the entire cache is cleared. Passing only an org clears
    that org. Passing both org and key clears one setting so env/default
    fallback is used immediately in this process.
    """
    org_id = _normalize_org_id(organization_id)
    if not org_id:
        _runtime_settings_by_org.clear()
        return
    if key is None:
        _runtime_settings_by_org.pop(org_id, None)
        return
    values = _runtime_settings_by_org.get(org_id)
    if values is not None:
        values.pop(key, None)
        if not values:
            _runtime_settings_by_org.pop(org_id, None)


async def load_runtime_settings_from_db(
    pool: Any,
    organization_id: Any | None = None,
) -> None:
    """Load DB-backed settings into the in-process cache.

    Startup calls this after migrations. Web settings updates adjust the cache
    directly so the current process sees changes immediately.
    """
    from lucent.db.runtime_settings import RuntimeSettingsRepository

    repo = RuntimeSettingsRepository(pool)
    rows = await repo.list_settings(organization_id=organization_id)

    if organization_id is not None:
        clear_runtime_setting_cache(organization_id)
    else:
        clear_runtime_setting_cache()

    for row in rows:
        key = row.get("key")
        if key not in _SETTING_DEFINITIONS_BY_KEY:
            continue
        set_runtime_setting_cache(row.get("organization_id"), key, row.get("value"))


def runtime_setting_snapshots(organization_id: Any) -> list[dict[str, Any]]:
    """Return UI-friendly setting snapshots for one organization."""
    snapshots: list[dict[str, Any]] = []
    org_id, cached = _org_cache(organization_id)
    for definition in _RUNTIME_SETTING_DEFINITIONS:
        value = get_runtime_setting(definition.key, organization_id=org_id)
        source = get_runtime_setting_source(definition.key, organization_id=org_id)
        env_raw = os.environ.get(definition.env_var)
        snapshots.append(
            {
                "definition": definition,
                "value": value,
                "form_value": _format_form_value(value),
                "source": source,
                "db_value": cached.get(definition.key),
                "env_var_set": env_raw is not None,
                "env_raw": env_raw,
                "default_value": definition.default,
            }
        )
    return snapshots


def runtime_settings_by_section(organization_id: Any) -> dict[str, list[dict[str, Any]]]:
    """Return setting snapshots grouped by display section."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for snapshot in runtime_setting_snapshots(organization_id):
        section = snapshot["definition"].section
        grouped.setdefault(section, []).append(snapshot)
    return grouped


def _format_form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def shadow_forget_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether shadow forgetting sidecar reads/writes are enabled."""
    return bool(
        get_runtime_setting(
            "memory.shadow_forget_enabled",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether search ranking includes the vitality boost term."""
    return bool(
        get_runtime_setting(
            "memory.search_vitality_boost_enabled",
            organization_id=organization_id,
        )
    )


def search_exclude_archived_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether search excludes ``archived``/``forgotten`` lifecycle stages by default.

    Phase-2 M9 rollout flag, mirroring ``LUCENT_SEARCH_VITALITY_BOOST_ENABLED``.

    - Off (default): the search SQL is byte-identical to the pre-M9 baseline —
      ``include_archived`` is accepted on the API surface but has no effect on
      the emitted query, so production behavior is unchanged until an operator
      opts in.
    - On: queries with ``include_archived=False`` (the default) get a
      ``lifecycle_stage NOT IN ('archived', 'forgotten')`` WHERE-clause
      addition. ``include_archived=True`` callers continue to see all rows.
    """
    return bool(
        get_runtime_setting(
            "memory.search_exclude_archived_enabled",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_alpha(*, organization_id: Any | None = None) -> float:
    """Weight for vitality contribution in ranked memory search."""
    return float(
        get_runtime_setting(
            "memory.search_vitality_boost_alpha",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_log_sample_rate(*, organization_id: Any | None = None) -> float:
    """Sampling rate (0.0–1.0) for emitting legacy-vs-boosted top-N comparison logs.

    Phase-2 observability hook: when the vitality boost flag is enabled, a
    fraction of search calls additionally run the legacy ranking and emit a
    structured JSON log line comparing the top-N results. Defaults to ``0.0``
    (disabled) so production is unaffected unless an operator opts in.
    """
    return float(
        get_runtime_setting(
            "memory.search_vitality_boost_log_sample_rate",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_log_top_n(*, organization_id: Any | None = None) -> int:
    """How many ranked results to compare in the boost-vs-legacy log line."""
    return int(
        get_runtime_setting(
            "memory.search_vitality_boost_log_top_n",
            organization_id=organization_id,
        )
    )


def daemon_auto_approve_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether daemon/cognitive requests bypass human approval."""
    return bool(
        get_runtime_setting(
            "requests.daemon_auto_approve",
            organization_id=organization_id,
        )
    )


def post_completion_review_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether completed requests go through the automatic review step."""
    skip_review = bool(
        get_runtime_setting(
            "requests.skip_post_completion_review",
            organization_id=organization_id,
        )
    )
    return not skip_review
