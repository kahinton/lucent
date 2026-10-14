"""Backend selection helpers for task and tool surfaces.

Lets a task (or an agent authoring one) pin a specific engine backend — e.g.
"run this on Claude Code" or "run this on Codex against local Ollama". The
chosen backend id is stored on ``tasks.backend_id`` and honoured at dispatch by
``lucent.llm.engine_resolution``.
"""

from __future__ import annotations

from typing import Any

from lucent.llm.engine_injection import local_provider_for_backend
from lucent.llm.engine_registry import UnknownEngineError, engine_availability, engine_spec


async def validate_backend_selection(
    pool: Any,
    backend_id: str | None,
    *,
    org_id: str | None,
    model_id: str | None = None,
) -> str | None:
    """Return an error message when a backend cannot serve the task, else None."""
    if not backend_id:
        return "backend_id is required"
    from lucent.db.backends import BackendRepository

    backend = await BackendRepository(pool).get(backend_id, org_id=org_id)
    if not backend:
        return f"Unknown backend: {backend_id}"
    if not backend.get("is_enabled", True):
        return f"Backend '{backend.get('name')}' is disabled"
    engine = backend.get("engine")
    try:
        spec = engine_spec(str(engine))
    except UnknownEngineError:
        return f"Backend engine {engine!r} is not available"
    if not spec.implemented:
        return f"Backend engine {engine!r} is not implemented"

    # A native-provider backend must match the model's provider. Local-provider
    # backends serve whatever the local server exposes, so no provider check.
    if model_id and local_provider_for_backend(backend) is None:
        from lucent.model_registry import get_model

        model = get_model(model_id)
        if model and model.provider and model.provider != backend.get("provider"):
            return (
                f"Model '{model_id}' is an {model.provider} model and cannot run on the "
                f"'{backend.get('provider')}' backend"
            )
    return None


async def engine_catalog(pool: Any, *, org_id: str | None) -> list[dict[str, Any]]:
    """Coding engines with the backends a caller may pin to a task.

    Used by the ``list_engines`` agent tool and the engines admin API so surfaces
    describe the same thing: what engines exist, whether they're usable, what
    model sources each supports, and which backends are configured.
    """
    from lucent.db.backends import BackendRepository
    from lucent.llm.engine_injection import compatible_provider_options
    from lucent.llm.engine_registry import engine_names
    from lucent.model_registry import list_models

    backends = await BackendRepository(pool).list(org_id=org_id, enabled_only=False)
    catalog: list[dict[str, Any]] = []
    for name in engine_names(kind="coding_agent"):
        spec = engine_spec(name)
        availability = engine_availability(name)
        catalog.append(
            {
                "engine": name,
                "display_name": spec.display_name,
                "available": availability.available,
                "model_sources": [o["label"] for o in compatible_provider_options(name)],
                "backends": [
                    {
                        "id": str(backend["id"]),
                        "name": backend["name"],
                        "provider": backend["provider"],
                        "enabled": backend.get("is_enabled", True),
                        "organization_id": (
                            str(backend["organization_id"])
                            if backend.get("organization_id")
                            else None
                        ),
                        # Optional model the backend runs by default.
                        "default_model": (backend.get("config") or {}).get("model"),
                        # Models this backend can run: registry models whose provider
                        # matches (e.g. a Copilot backend → Copilot-hosted models, an
                        # Ollama backend → locally discovered models).
                        "models": [
                            {"id": model.id, "name": model.name}
                            for model in list_models(
                                provider=backend["provider"], include_disabled=False
                            )
                            if model.model_class != "decision"
                        ],
                    }
                    for backend in backends
                    if backend["engine"] == name
                ],
            }
        )
    return catalog


async def backend_default_model(
    pool: Any, backend_id: str | None, *, org_id: str | None
) -> str | None:
    """The model a backend runs by default (its ``config.model``), or None."""
    if not backend_id:
        return None
    from lucent.db.backends import BackendRepository

    backend = await BackendRepository(pool).get(backend_id, org_id=org_id)
    if not backend:
        return None
    model = (backend.get("config") or {}).get("model")
    return str(model) if model else None
