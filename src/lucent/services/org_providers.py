"""Per-organization LLM provider credentials and models, for instance operators.

There are two distinct things an operator configures, and conflating them is the
easy mistake:

* **Provider credentials.** One API key per provider per organization, stored in
  the secret provider at a canonical key (``model_providers.<provider>.api_key``).
  This is what the engines and model discovery actually read
  (``resolve_provider_credential``) — *not* an engine-backend row.
* **Engine backends.** Optional routing overrides (a custom base URL, a gateway,
  a pinned model). A backend carries no provider API key for langchain; that key
  lives at the canonical provider secret above.

The tenant-facing surface resolves the organization from the *caller's* session,
which is useless for provisioning someone else's workspace. Everything here takes
an explicit target organization and writes only into it, so configuring a new
tenant can never leak a credential or a model into another tenant.

Secrets are never returned. Reads report only whether a field is configured.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.access_control import AuthAccessRepository
from lucent.db.backends import BackendRepository
from lucent.db.models import ModelRepository
from lucent.logging import get_logger
from lucent.secrets import SecretRegistry

logger = get_logger(__name__)


class ProviderConfigError(ValueError):
    """Invalid provider/engine combination or credential field."""


class ModelConfigError(ValueError):
    """Invalid model definition."""


def _credential_defs() -> dict[str, Any]:
    from lucent.model_discovery import MODEL_PROVIDER_CREDENTIALS

    return {d.provider: d for d in MODEL_PROVIDER_CREDENTIALS}


def available_providers() -> list[dict[str, Any]]:
    """Providers an operator can configure, with the engines that accept each.

    Derived from the credential registry (the single source of truth for what the
    engines read) so the console can never offer a provider the server cannot use.
    """
    from lucent.llm.engine_injection import compatible_provider_options
    from lucent.llm.engine_registry import engine_names

    engines_by_provider: dict[str, list[str]] = {}
    for engine in engine_names(implemented_only=True):
        for option in compatible_provider_options(engine):
            engines_by_provider.setdefault(option["name"], []).append(engine)

    out: list[dict[str, Any]] = []
    for provider, definition in sorted(_credential_defs().items()):
        out.append(
            {
                "provider": provider,
                "name": definition.name,
                "credential_label": definition.credential_label,
                "env_vars": list(definition.env_vars),
                "engines": sorted(engines_by_provider.get(provider, [])),
                "needs_api_key": True,
            }
        )
    # Providers an engine accepts that carry no API key (Ollama, LM Studio):
    # they still need to be visible, they just need a base URL or nothing at all.
    known = {item["provider"] for item in out}
    for provider, engines in sorted(engines_by_provider.items()):
        if provider in known:
            continue
        out.append(
            {
                "provider": provider,
                "name": provider.replace("_", " ").title(),
                "credential_label": None,
                "env_vars": [],
                "engines": sorted(engines),
                "needs_api_key": False,
            }
        )
    return out


def available_engines() -> list[dict[str, Any]]:
    """Engines with the providers each accepts and their config fields."""
    from lucent.llm.engine_credentials import engine_config_schema
    from lucent.llm.engine_injection import compatible_provider_options
    from lucent.llm.engine_registry import engine_availability, engine_names

    out: list[dict[str, Any]] = []
    for engine in engine_names(implemented_only=True):
        out.append(
            {
                "engine": engine,
                "providers": [
                    {"name": p["name"], "label": p.get("label") or p["name"]}
                    for p in compatible_provider_options(engine)
                ],
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "field_type": f.field_type,
                        "required": f.required,
                        "is_secret": f.is_secret,
                        "choices": list(f.choices),
                        "help_text": f.help_text,
                    }
                    for f in engine_config_schema(engine)
                ],
                "availability": engine_availability(engine),
            }
        )
    return out


async def list_credentials(
    pool: asyncpg.Pool, organization_id: UUID | str
) -> list[dict[str, Any]]:
    """Which provider credentials a tenant has, without the values."""
    from lucent.model_discovery import model_provider_secret_scope

    scope = model_provider_secret_scope(str(organization_id))
    registry = SecretRegistry.get()
    out: list[dict[str, Any]] = []
    for provider, definition in sorted(_credential_defs().items()):
        configured = False
        try:
            value = await registry.get(definition.secret_key, scope)
            configured = bool(value and value.strip())
        except Exception:  # noqa: BLE001 — a read failure means "not configured"
            configured = False
        out.append(
            {
                "provider": provider,
                "name": definition.name,
                "credential_label": definition.credential_label,
                "configured": configured,
                "env_vars": list(definition.env_vars),
            }
        )
    return out


async def set_credential(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    provider: str,
    value: str,
) -> dict[str, Any]:
    """Store a provider API key for a tenant at the canonical secret key.

    This is the key the engines and discovery actually read, so setting it here
    is what makes a provider usable by that tenant. Writing it anywhere else (for
    example into a backend's ``secret_refs``) would look configured and do
    nothing.
    """
    from lucent.model_discovery import model_provider_secret_scope

    provider_key = (provider or "").strip().lower()
    definition = _credential_defs().get(provider_key)
    if definition is None:
        raise ProviderConfigError(
            f"Provider '{provider_key}' has no API-key credential. Known: "
            + ", ".join(sorted(_credential_defs()))
        )
    clean = (value or "").strip()
    if not clean:
        raise ProviderConfigError("A credential value is required.")

    scope = model_provider_secret_scope(str(organization_id))
    await SecretRegistry.get().set(definition.secret_key, clean, scope)
    logger.info("Stored %s credential for org %s", provider_key, organization_id)
    return {"provider": provider_key, "configured": True}


async def clear_credential(
    pool: asyncpg.Pool, organization_id: UUID | str, *, provider: str
) -> dict[str, Any]:
    """Remove a provider API key so an environment fallback can apply."""
    from lucent.model_discovery import model_provider_secret_scope

    provider_key = (provider or "").strip().lower()
    definition = _credential_defs().get(provider_key)
    if definition is None:
        raise ProviderConfigError(f"Unknown provider '{provider_key}'")

    scope = model_provider_secret_scope(str(organization_id))
    await SecretRegistry.get().delete(definition.secret_key, scope)
    return {"provider": provider_key, "configured": False}


async def discover_models(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    providers: list[str] | None = None,
) -> dict[str, Any]:
    """Ask configured providers what they offer and register it for a tenant.

    This is the step that turns a stored credential into selectable models, so it
    belongs immediately after credentials in a provisioning flow.
    """
    from lucent.model_discovery import ModelDiscoveryService
    from lucent.model_registry import load_models_from_db

    result = await ModelDiscoveryService(pool).sync(
        providers=providers or None,
        org_id=str(organization_id),
    )
    await load_models_from_db(pool)
    return result


async def list_backends(
    pool: asyncpg.Pool, organization_id: UUID | str
) -> list[dict[str, Any]]:
    """Engine backends owned by this tenant only (shared rows excluded)."""
    rows = await BackendRepository(pool).list(org_id=str(organization_id))
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("organization_id") is None:
            # A shared/global row belongs to no tenant; deleting it from a
            # per-tenant screen would be a cross-tenant action.
            continue
        refs = row.get("secret_refs") or {}
        out.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "engine": row["engine"],
                "provider": row["provider"],
                "is_enabled": row["is_enabled"],
                "config": row.get("config") or {},
                "secrets_configured": {
                    field: bool(value) for field, value in refs.items()
                },
                "created_at": row.get("created_at"),
            }
        )
    return out


async def create_backend(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    name: str,
    engine: str,
    provider: str,
    config: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    is_enabled: bool = True,
) -> dict[str, Any]:
    """Create an engine backend override for a tenant.

    Optional: a tenant can call a provider with no backend at all as long as the
    provider credential is set. Backends exist for base URLs, gateways, and pinned
    models.
    """
    from lucent.llm.engine_credentials import engine_config_schema, secret_key_for
    from lucent.llm.engine_injection import (
        compatible_provider_options,
        provider_allowed_for_engine,
    )
    from lucent.model_discovery import model_provider_secret_scope

    org_id = str(organization_id)
    clean_name = (name or "").strip()
    if not clean_name:
        raise ProviderConfigError("A backend name is required.")

    provider_key = (provider or "").strip().lower()
    if not provider_allowed_for_engine(engine, provider_key):
        allowed = ", ".join(p["name"] for p in compatible_provider_options(engine))
        raise ProviderConfigError(
            f"Engine '{engine}' cannot use provider '{provider_key}'. "
            f"Supported: {allowed or 'none'}."
        )

    secret_fields = {f.key for f in engine_config_schema(engine) if f.is_secret}
    supplied = {k: v for k, v in (secrets or {}).items() if str(v).strip()}
    unknown = set(supplied) - secret_fields
    if unknown:
        raise ProviderConfigError(
            f"Not secret field(s) of engine '{engine}': {', '.join(sorted(unknown))}. "
            f"Secret fields: {', '.join(sorted(secret_fields)) or 'none'} — for an "
            "API key use the provider credential instead."
        )

    repo = BackendRepository(pool)
    existing = await repo.list(org_id=org_id, engine=engine, provider=provider_key)
    if any(
        row["name"] == clean_name and row.get("organization_id") is not None
        for row in existing
    ):
        raise ProviderConfigError(f"A backend named '{clean_name}' already exists.")

    secret_refs: dict[str, str] = {}
    registry = SecretRegistry.get()
    scope = model_provider_secret_scope(org_id)
    for field, value in supplied.items():
        key = secret_key_for(engine, clean_name, field)
        await registry.set(key, value.strip(), scope)
        secret_refs[field] = key

    backend = await repo.create(
        name=clean_name,
        engine=engine,
        provider=provider_key,
        config=config or {},
        secret_refs=secret_refs,
        is_enabled=is_enabled,
        org_id=org_id,
    )
    return {
        "id": str(backend["id"]),
        "name": backend["name"],
        "engine": backend["engine"],
        "provider": backend["provider"],
        "is_enabled": backend["is_enabled"],
        "config": backend.get("config") or {},
        "secrets_configured": {f: True for f in secret_refs},
    }


async def delete_backend(
    pool: asyncpg.Pool, organization_id: UUID | str, backend_id: str
) -> bool:
    """Delete a tenant-owned backend. Shared rows are never deletable here."""
    org_id = str(organization_id)
    repo = BackendRepository(pool)
    backend = await repo.get(backend_id, org_id=org_id)
    if backend is None or backend.get("organization_id") is None:
        return False
    return await repo.delete(backend_id, org_id=org_id)


async def list_models(
    pool: asyncpg.Pool, organization_id: UUID | str
) -> list[dict[str, Any]]:
    """Models visible to a tenant, enabled or not.

    A model row either belongs to this organization or is shared by the whole
    instance (``organization_id`` NULL). The distinction is returned as
    ``owned`` because it decides what an operator may do: an org-owned row can
    be enabled or disabled for that tenant alone, while a shared row is
    instance-wide and changing it here would affect every other tenant.
    """
    result = await ModelRepository(pool).list_models(
        org_id=str(organization_id), enabled_only=False, limit=500
    )
    org_id = str(organization_id)
    items = result.get("items") or []

    # Which engines can actually run each model. A model's own ``engine`` is only
    # its preferred default; compatibility comes from the engine's declared
    # provider support plus whatever catalogs reported it (migration 151). This is
    # what lets a shared `gpt-5.6-terra` row show up as available on Codex even
    # though Copilot owns the row.
    reported = await _catalog_engines(pool)

    from lucent.llm.engine_registry import compatible_engine_names

    for item in items:
        item["owned"] = str(item.get("organization_id") or "") == org_id
        engines = set(compatible_engine_names(str(item.get("provider") or "")))
        engines.update(reported.get(item["id"], ()))
        item["engines"] = sorted(engines)
    return items


async def _catalog_engines(pool: asyncpg.Pool) -> dict[str, set[str]]:
    """Model id → engines that reported it in their own catalog."""
    try:
        rows = await pool.fetch(
            "SELECT model_id, engine FROM model_engine_catalogs"
        )
    except Exception:  # noqa: BLE001 — table absent on a pre-151 database
        return {}
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["model_id"], set()).add(row["engine"])
    return out


async def create_model(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    *,
    model_id: str,
    provider: str,
    name: str,
    engine: str | None = None,
    category: str = "general",
    model_class: str = "language",
    context_window: int = 0,
    supports_tools: bool = True,
    supports_vision: bool = False,
    notes: str = "",
    is_enabled: bool = True,
    grant_to_org: bool = True,
    granted_by: UUID | str | None = None,
) -> dict[str, Any]:
    """Define a model for a tenant and (optionally) entitle the org to it.

    An enabled-but-ungranted model is not usable: usage is clearance-driven and
    default-deny. ``grant_to_org`` therefore defaults to True, because the point of
    defining a model for a tenant is that they can use it.
    """
    from lucent.model_registry import load_models_from_db

    org_id = str(organization_id)
    clean_id = (model_id or "").strip()
    if not clean_id:
        raise ModelConfigError("A model id is required.")
    if not (provider or "").strip():
        raise ModelConfigError("A provider is required.")

    if engine:
        from lucent.llm.model_engine_validation import validate_engine_override

        try:
            validate_engine_override(provider, engine)
        except ValueError as exc:
            raise ModelConfigError(str(exc)) from exc

    repo = ModelRepository(pool)
    if await repo.get_model(clean_id) is not None:
        raise ModelConfigError(f"Model id '{clean_id}' already exists.")

    model = await repo.create_model(
        model_id=clean_id,
        provider=provider.strip().lower(),
        name=name or clean_id,
        category=category,
        model_class=model_class,
        api_model_id=clean_id,
        context_window=int(context_window or 0),
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        notes=notes,
        is_enabled=is_enabled,
        org_id=org_id,
        engine=engine or None,
        discovery_source="manual",
        is_custom=True,
    )

    granted = False
    if grant_to_org:
        try:
            # `upsert_grant` refuses anyone who is not the resource owner or an
            # admin *of the target organization*. An instance operator is
            # neither — they are not a member of the tenant at all. The grant is
            # therefore recorded against the tenant's own owner, which is both
            # what the guard requires and the truthful attribution: the model
            # belongs to that organization, not to the operator.
            grantor = granted_by
            owner_row = await pool.fetchrow(
                "SELECT id FROM users WHERE organization_id = $1::uuid "
                "  AND role = 'owner' AND is_active "
                "ORDER BY created_at LIMIT 1",
                org_id,
            )
            if owner_row is not None:
                grantor = owner_row["id"]
            if grantor is None:
                raise ModelConfigError(
                    "Organization has no owner to grant the model to."
                )

            await AuthAccessRepository(pool).upsert_grant(
                "model",
                clean_id,
                grantee_type="org",
                grantee_id=None,
                role="read",
                granted_by=UUID(str(grantor)),
                organization_id=UUID(org_id),
            )
            granted = True
        except Exception:  # noqa: BLE001 — the model exists either way
            logger.warning(
                "Model %s created but org grant failed; tenant cannot use it yet",
                clean_id,
                exc_info=True,
            )

    await load_models_from_db(pool)
    return {**model, "org_granted": granted}


async def set_model_enabled(
    pool: asyncpg.Pool, organization_id: UUID | str, model_id: str, *, enabled: bool
) -> bool:
    """Enable or disable one model for a tenant.

    Only rows owned by this organization are touched. A shared model
    (``organization_id`` NULL) is instance-wide configuration: flipping it here
    would silently change every other tenant, so it is refused rather than
    quietly ignored. Returns ``False`` for both "no such model" and "not yours"
    so the caller reports one unambiguous failure.
    """
    from lucent.model_registry import load_models_from_db

    updated = await ModelRepository(pool).update_model(
        model_id, organization_id=str(organization_id), is_enabled=enabled
    )
    if updated is None:
        return False
    await load_models_from_db(pool)
    return True


async def _owning_user_id(pool: asyncpg.Pool, organization_id: UUID | str) -> Any:
    """The tenant's own owner, used as grantor for clearance writes.

    ``upsert_grant`` refuses anyone who is neither the resource owner nor an
    admin *of the target organization*. An instance operator is neither — they
    are not a member of the tenant at all. Recording the grant against the
    tenant's owner satisfies that guard and is the truthful attribution: the
    model belongs to that organization, not to the operator.
    """
    row = await pool.fetchrow(
        "SELECT id FROM users WHERE organization_id = $1::uuid "
        "  AND role = 'owner' AND is_active "
        "ORDER BY created_at LIMIT 1",
        str(organization_id),
    )
    return row["id"] if row is not None else None


async def grant_model_to_org(
    pool: asyncpg.Pool,
    organization_id: UUID | str,
    model_id: str,
    *,
    granted_by: UUID | str,
) -> dict[str, Any]:
    """Make a model usable by one tenant, cloning it if it is instance-shared.

    Model usage is clearance-driven, and a grant requires a row the organization
    *owns* — ``upsert_grant`` resolves the resource with
    ``WHERE organization_id = $2``, so a shared (``organization_id`` NULL) row
    is simply not findable there.

    The obvious workaround, promoting the shared row with
    ``UPDATE models SET organization_id = <org>``, is wrong: ``models.id`` is the
    primary key and holds one row per model, so promoting it **takes the model
    away from every other tenant**. (That is what ``bootstrap.ensure_usable_model``
    does, and it is safe there only because bootstrap provisions the single
    instance owner.) This clones the row into the tenant instead, leaving the
    shared catalogue intact, then grants the clone.
    """
    from lucent.model_registry import load_models_from_db

    org_str = str(organization_id)
    repo = ModelRepository(pool)

    source = await repo.get_model(model_id)
    if source is None:
        raise ValueError(f"Model {model_id!r} is not in the registry.")

    source_org = source.get("organization_id")
    if source_org is not None and str(source_org) != org_str:
        raise ValueError(
            f"Model {model_id!r} belongs to another organization."
        )

    target_id = model_id
    cloned = False
    if source_org is None:
        # Shared row: give this tenant its own copy under a deterministic id so
        # a retry is idempotent and the provenance stays readable.
        target_id = f"{model_id}@{org_str[:8]}"
        existing = await repo.get_model(target_id, org_str)
        if existing is None:
            await repo.create_model(
                model_id=target_id,
                provider=source["provider"],
                name=source.get("name") or model_id,
                category=source.get("category") or "general",
                model_class=source.get("model_class") or "language",
                api_model_id=source.get("api_model_id") or model_id,
                context_window=int(source.get("context_window") or 0),
                supports_tools=bool(source.get("supports_tools", True)),
                supports_vision=bool(source.get("supports_vision", False)),
                notes=source.get("notes") or "",
                tags=list(source.get("tags") or []),
                reasoning_efforts=list(source.get("reasoning_efforts") or []),
                is_enabled=True,
                org_id=org_str,
                engine=source.get("engine"),
                discovery_source=source.get("discovery_source") or "seed",
                is_custom=bool(source.get("is_custom", False)),
                discovery_metadata=source.get("discovery_metadata") or {},
            )
            cloned = True
        else:
            await repo.update_model(target_id, organization_id=org_str, is_enabled=True)

    # Attribute the grant to the tenant's owner; see ``_owning_user_id``.
    grantor = granted_by
    owner_id = await _owning_user_id(pool, org_str)
    if owner_id is not None:
        grantor = owner_id

    await repo.update_model(target_id, organization_id=org_str, is_enabled=True)

    from lucent.db.access_control import AuthAccessRepository

    await AuthAccessRepository(pool).upsert_grant(
        "model",
        target_id,
        grantee_type="org",
        grantee_id=None,
        role="read",
        granted_by=UUID(str(grantor)),
        organization_id=UUID(org_str),
    )

    await load_models_from_db(pool)
    return {
        "model_id": target_id,
        "source_model_id": model_id,
        "cloned": cloned,
        "enabled": True,
        "granted": True,
    }
