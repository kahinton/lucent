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
    availability = engine_availability(str(engine))
    if not availability.available:
        detail = f": {availability.detail}" if availability.detail else ""
        return f"Backend engine {engine!r} is unavailable in this runtime{detail}"

    # The backend's provider is the model's provider. A local Ollama backend
    # serves Ollama models — not a cloud model id that would be sent to the
    # local server (or, worse, routed to a native key). The provider→engine map
    # must also allow the pairing.
    if model_id:
        from lucent.llm.engine_registry import engine_supports_provider
        from lucent.model_registry import get_model

        model = get_model(model_id)
        if model and model.provider:
            if model.provider != backend.get("provider"):
                return (
                    f"Model '{model_id}' is an {model.provider} model and cannot run on "
                    f"the '{backend.get('provider')}' backend"
                )
            if not engine_supports_provider(str(engine), model.provider):
                return (
                    f"Engine '{engine}' cannot run {model.provider} models"
                )
    return None


async def validate_request_execution_pin(
    pool: Any,
    backend_id: str | None,
    requested_model: str | None,
    *,
    org_id: str | None,
) -> str | None:
    """Validate a request-level execution pin, including model sufficiency.

    A request survives a daemon round trip before tasks exist, so the pin must
    stand on its own. For a local-provider backend (Ollama/LM Studio) that
    means naming a model: there is no safe global default to fall back to, and
    an auto-selected cloud model would be handed to a local engine.
    """
    if requested_model and len(requested_model) > 64:  # tasks.model is VARCHAR(64)
        return "requested_model must be 64 characters or fewer"
    error = await validate_backend_selection(
        pool, backend_id, org_id=org_id, model_id=requested_model
    )
    if error or not backend_id or requested_model:
        return error

    from lucent.db.backends import BackendRepository

    backend = await BackendRepository(pool).get(backend_id, org_id=org_id)
    if backend is None:
        return f"Unknown backend: {backend_id}"
    if local_provider_for_backend(backend) is None:
        return None
    if (backend.get("config") or {}).get("model"):
        return None
    return (
        "A local-provider backend needs requested_model (or config.model) so "
        "the daemon does not run an unrelated cloud model id against it"
    )


async def engine_catalog(pool: Any, *, org_id: str | None) -> list[dict[str, Any]]:
    """Coding engines with the backends a caller may pin to a task.

    Used by the ``list_engines`` agent tool and the engines admin API so surfaces
    describe the same thing: what engines exist, whether they're usable, what
    model sources each supports, and which backends are configured.
    """
    from lucent.db.backends import BackendRepository
    from lucent.llm.engine_injection import compatible_provider_options
    from lucent.llm.engine_registry import (
        engine_names,
        engine_supports_provider,
        provider_label,
    )
    from lucent.model_registry import list_models

    backends = await BackendRepository(pool).list(org_id=org_id, enabled_only=False)
    catalog: list[dict[str, Any]] = []
    for name in engine_names(kind="coding_agent"):
        spec = engine_spec(name)
        availability = engine_availability(name)
        provider_options = compatible_provider_options(name)
        catalog.append(
            {
                "engine": name,
                "display_name": spec.display_name,
                "available": availability.available,
                "supported_providers": provider_options,
                "model_sources": [o["label"] for o in provider_options],
                "backends": [
                    {
                        "id": str(backend["id"]),
                        "name": backend["name"],
                        "provider": backend["provider"],
                        "provider_label": provider_label(backend["provider"]),
                        "provider_supported": engine_supports_provider(
                            name, backend["provider"]
                        ),
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
                            {
                                "id": model.id,
                                "name": model.name,
                                # The model's own engine/backend binding. This
                                # is what model-only selection uses; it may
                                # differ from the backend this catalog entry
                                # describes (Ollama models are discovered as
                                # engine="langchain"). Passing `backend_id` is
                                # what overrides that binding.
                                "engine": model.engine,
                                "backend_id": (
                                    str(model.backend_id) if model.backend_id else None
                                ),
                                "supports_tools": model.supports_tools,
                            }
                            for model in list_models(
                                provider=backend["provider"], include_disabled=False
                            )
                            if model.model_class != "decision"
                            and engine_supports_provider(name, model.provider)
                        ],
                    }
                    for backend in backends
                    if backend["engine"] == name
                ],
            }
        )
    return catalog


async def model_execution_catalog(
    pool: Any,
    *,
    org_id: str | None,
    models: list[Any] | None = None,
    configured_default: str | None = None,
) -> dict[str, Any]:
    """Model-centric view: each model's provider and the engines it can run on.

    This is the presentation the LLM and admin UI should use. It answers one
    question — "given this model, where can I run it?" — without making the
    caller join engine specs, local-provider flavors, and backend rows.
    """
    from lucent.db.backends import BackendRepository
    from lucent.llm.engine_registry import (
        compatible_engines,
        default_engine_for_provider,
        provider_label,
        provider_engine_map,
    )
    from lucent.llm.local_providers import LOCAL_PROVIDERS
    from lucent.model_registry import list_models as _list_models

    backends = await BackendRepository(pool).list(org_id=org_id, enabled_only=False)
    if models is None:
        models = [
            model
            for model in _list_models(include_disabled=False)
            if model.model_class != "decision"
        ]

    def _backends_for(engine: str, provider: str) -> list[dict[str, Any]]:
        return [
            {
                "id": str(backend["id"]),
                "name": backend["name"],
                "enabled": backend.get("is_enabled", True),
                "default_model": (backend.get("config") or {}).get("model"),
            }
            for backend in backends
            if backend["engine"] == engine and backend["provider"] == provider
        ]

    described: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    decision_models: list[dict[str, Any]] = []
    for model in models:
        provider = (model.provider or "").strip().lower()
        is_local = provider in LOCAL_PROVIDERS
        is_decision = model.model_class == "decision"
        # Decision models are System One classifiers, never chat/task models.
        # Keep them visible with no execution targets so list consumers do not
        # accidentally assign one.
        default_engine = None if is_decision else (
            model.engine
            or default_engine_for_provider(
                provider, configured_default=configured_default
            )
        )
        engines = (
            []
            if is_decision
            else compatible_engines(provider, configured_default=configured_default)
        )
        for engine in engines:
            engine["backends"] = _backends_for(str(engine["engine"]), provider)
        described.append(
            {
                "id": model.id,
                "name": model.name,
                "provider": provider,
                "provider_label": provider_label(provider),
                "category": model.category,
                "model_class": model.model_class,
                "decision_model": is_decision,
                "supports_tools": model.supports_tools,
                "supports_vision": model.supports_vision,
                "context_window": model.context_window,
                "reasoning_efforts": model.reasoning_efforts,
                "notes": model.notes,
                "tags": model.tags,
                "default_engine": default_engine,
                "engines": engines,
            }
        )
        if is_decision:
            decision_models.append(
                {
                    "id": model.id,
                    "name": model.name,
                    "provider": provider,
                    "provider_label": provider_label(provider),
                    "supports_tools": model.supports_tools,
                }
            )
        for engine in engines:
            name = str(engine["engine"])
            engine_backends = engine["backends"]
            # The default engine can run with default routing (native
            # credentials or the provider's env base URL) when it has no
            # configured backend. When it does, the backend rows below are the
            # concrete default targets.
            if (
                name == default_engine
                and not engine_backends
                and (not is_local or name == "langchain")
            ):
                targets.append(
                    {
                        "model": model.id,
                        "provider": provider,
                        "engine": name,
                        "engine_display": engine["display_name"],
                        "backend_id": None,
                        "backend_name": None,
                        "available": bool(engine["available"]),
                        "default": True,
                    }
                )
            # Native providers can also run on another engine's native
            # credentials; local providers need a configured backend.
            elif not is_local:
                targets.append(
                    {
                        "model": model.id,
                        "provider": provider,
                        "engine": name,
                        "engine_display": engine["display_name"],
                        "backend_id": None,
                        "backend_name": None,
                        "available": bool(engine["available"]),
                        "default": False,
                    }
                )
            for index, backend in enumerate(engine_backends):
                is_default = (
                    backend["id"] == str(model.backend_id)
                    if model.backend_id
                    else (name == default_engine and index == 0)
                )
                targets.append(
                    {
                        "model": model.id,
                        "provider": provider,
                        "engine": name,
                        "engine_display": engine["display_name"],
                        "backend_id": backend["id"],
                        "backend_name": backend["name"],
                        "available": bool(engine["available"]) and backend["enabled"],
                        "default": is_default,
                    }
                )

    return {
        "provider_engine_map": provider_engine_map(
            configured_default=configured_default
        ),
        "models": described,
        "execution_targets": targets,
        "decision_models": decision_models,
    }


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
