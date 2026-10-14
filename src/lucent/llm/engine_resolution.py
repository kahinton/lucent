"""Backend-aware engine resolution.

Turns a model id (plus optional task/request context) into the engine that
should run it. This is the single place that consults the ``backends`` table,
so callers get backend-aware routing without duplicating the precedence rules.

Precedence:
  1. An explicitly requested backend (API override).
  2. A backend pinned on the task/workflow (``tasks.backend_id``).
  3. The model's own backend binding or its pinned (engine, provider) match.
  4. Task-type auto-routing (opt-in via ``models.engine_auto_route``).
  5. The existing routing in :mod:`lucent.llm.factory` — model ``engine``
     override, runtime override, provider default, then the configured default.

When no backend applies, the result is identical to calling
``get_engine_for_model`` directly, so enabling backends never changes behavior
for models that have none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lucent.llm import engine_registry
from lucent.llm.engine import LLMEngine
from lucent.logging import get_logger

logger = get_logger("llm.engine_resolution")


@dataclass(frozen=True)
class EngineResolution:
    """The engine chosen for a model, plus how it was chosen."""

    engine: LLMEngine
    backend: dict[str, Any] | None
    source: str  # requested_backend | task_backend | model_backend | auto_route | default
    requested_kind: str | None = None


def _find_backend(candidates: list[dict], backend_id: str | None) -> dict | None:
    if not backend_id:
        return None
    return next(
        (b for b in candidates if str(b["id"]) == str(backend_id) and b.get("is_enabled", True)),
        None,
    )


def _preferred_kind(*, agent_type: str | None, title: str | None, description: str | None) -> str:
    """Engine kind a task wants: coding-agent for agentic work, else general."""
    from lucent.model_registry import _infer_task_category

    category, _reason = _infer_task_category(
        agent_type=agent_type, title=title, description=description
    )
    return "coding_agent" if category == "agentic" else "llm_access"


def _pick_by_kind(candidates: list[dict], *, provider: str | None, kind: str) -> dict | None:
    """First enabled backend whose engine is of ``kind`` and matches the provider."""
    for backend in candidates:
        if not backend.get("is_enabled", True):
            continue
        try:
            spec = engine_registry.engine_spec(backend["engine"])
        except engine_registry.UnknownEngineError:
            continue
        if not spec.implemented or spec.kind != kind:
            continue
        if provider and backend["provider"] != provider:
            continue
        return backend
    return None


async def _load_backends(*, org_id: str | None, pool: Any) -> list[dict]:
    from lucent.db.backends import BackendRepository

    if pool is None:
        from lucent.db import get_pool

        pool = await get_pool()
    if pool is None:
        return []
    return await BackendRepository(pool).list(org_id=org_id, enabled_only=True)


async def resolve_engine(
    model_id: str,
    *,
    org_id: str | None = None,
    agent_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    task_backend_id: str | None = None,
    requested_backend: str | None = None,
    pool: Any = None,
) -> EngineResolution:
    """Resolve the engine for a model, honoring backends and auto-routing."""
    from lucent.model_registry import get_model

    model = get_model(model_id)
    if model and model.model_class == "decision":
        raise ValueError(
            f"Model '{model_id}' is a decision model, not a chat model. "
            "Use lucent.llm.decision.decide() for decision-model work."
        )

    try:
        candidates = await _load_backends(org_id=org_id, pool=pool)
    except Exception:
        logger.debug("Backend lookup failed; using default engine", exc_info=True)
        candidates = []

    # 1. Explicit request override.
    backend = _find_backend(candidates, requested_backend)
    if backend:
        return EngineResolution(
            engine_registry.get_engine(backend["engine"]), backend, "requested_backend"
        )

    # 2. Per-task / per-workflow pin.
    backend = _find_backend(candidates, task_backend_id)
    if backend:
        return EngineResolution(
            engine_registry.get_engine(backend["engine"]), backend, "task_backend"
        )

    # 3. The model's own backend binding or (engine, provider) match.
    if model is not None:
        from lucent.db.backends import resolve_backend_for_model

        pseudo = {
            "id": model.id,
            "provider": model.provider,
            "engine": model.engine,
            "backend_id": model.backend_id,
        }
        backend = resolve_backend_for_model(pseudo, org_id=org_id, candidates=candidates)
        if backend:
            return EngineResolution(
                engine_registry.get_engine(backend["engine"]), backend, "model_backend"
            )

    # 4. Auto-route by task type (opt-in).
    if model is not None and _auto_route_enabled(org_id):
        kind = _preferred_kind(agent_type=agent_type, title=title, description=description)
        backend = _pick_by_kind(candidates, provider=model.provider, kind=kind)
        if backend:
            return EngineResolution(
                engine_registry.get_engine(backend["engine"]),
                backend,
                "auto_route",
                requested_kind=kind,
            )

    # 5. Existing routing (engine override / runtime override / provider default).
    # Import the package-level export so callers/tests that patch
    # ``lucent.llm.get_engine_for_model`` keep working.
    from lucent.llm import get_engine_for_model

    return EngineResolution(get_engine_for_model(model_id), None, "default")


def _auto_route_enabled(org_id: str | None) -> bool:
    try:
        from lucent.settings import engine_auto_route

        return engine_auto_route(organization_id=org_id)
    except Exception:
        return False
