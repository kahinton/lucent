"""Admin engine registry API endpoints.

Exposes the engine registry (kinds, capabilities, config schema, availability)
so the admin UI and automation can render engine/backend controls without
hardcoding engine names.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from lucent.api.deps import AuthenticatedUser
from lucent.llm.engine_registry import ENGINE_REGISTRY, engine_availability

router = APIRouter()


def _require_admin_user(user: AuthenticatedUser) -> None:
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required")


def _serialize_engine(name: str) -> dict:
    spec = ENGINE_REGISTRY[name]
    availability = engine_availability(name)
    return {
        "name": spec.name,
        "display_name": spec.display_name,
        "kind": spec.kind,
        "implemented": spec.implemented,
        "notes": spec.notes,
        "provider_support": sorted(spec.provider_support) if spec.provider_support else None,
        "capabilities": asdict(spec.capabilities),
        "config_schema": [
            {
                "key": f.key,
                "label": f.label,
                "field_type": f.field_type,
                "required": f.required,
                "env_var": f.env_var,
                "choices": list(f.choices),
                "help_text": f.help_text,
                "is_secret": f.is_secret,
            }
            for f in spec.config_schema
        ],
        "availability": {
            "available": availability.available,
            "detail": availability.detail,
            "missing": list(availability.missing),
        },
    }


@router.get("")
async def list_engines(user: AuthenticatedUser, implemented_only: bool = False):
    """List engines with capabilities, config schema, and runtime availability."""
    user.require_scope("read")
    _require_admin_user(user)
    engines = [
        _serialize_engine(name)
        for name, spec in ENGINE_REGISTRY.items()
        if spec.implemented or not implemented_only
    ]
    from lucent.llm.local_providers import LOCAL_PROVIDERS

    local_providers = [
        {
            "name": spec.name,
            "display_name": spec.display_name,
            "default_base_url": spec.default_base_url,
            "base_url_env": spec.base_url_env,
            "flavors": sorted(spec.flavors),
            "codex_provider_id": spec.codex_provider_id,
        }
        for spec in LOCAL_PROVIDERS.values()
    ]
    return {"items": engines, "local_providers": local_providers, "total_count": len(engines)}
