"""Provider-backed model discovery and database synchronization.

LangChain initializes chat models across providers, but it does not expose a
single provider-agnostic catalog API. Discovery therefore uses each configured
provider's own model-listing endpoint, plus the Copilot SDK's ``list_models``
method for Copilot-hosted models.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from lucent.db.models import ModelRepository
from lucent.llm.langchain_providers import CUSTOM_PROVIDER
from lucent.logging import get_logger
from lucent.secrets import SecretRegistry, SecretScope

logger = get_logger("model_discovery")


@dataclass(frozen=True, slots=True)
class ModelProviderCredentialDefinition:
    """Credential metadata for one discoverable model provider."""

    provider: str
    name: str
    credential_label: str
    secret_key: str
    env_vars: tuple[str, ...]
    help_text: str = ""


MODEL_PROVIDER_CREDENTIALS: tuple[ModelProviderCredentialDefinition, ...] = (
    ModelProviderCredentialDefinition(
        provider="copilot",
        name="GitHub Copilot",
        credential_label="GitHub token",
        secret_key="model_providers.copilot.github_token",
        env_vars=("GITHUB_TOKEN",),
        help_text="Used by the Copilot SDK to discover models and run Copilot-hosted models.",
    ),
    ModelProviderCredentialDefinition(
        provider="openai",
        name="OpenAI",
        credential_label="API key",
        secret_key="model_providers.openai.api_key",
        env_vars=("OPENAI_API_KEY",),
    ),
    ModelProviderCredentialDefinition(
        provider="anthropic",
        name="Anthropic",
        credential_label="API key",
        secret_key="model_providers.anthropic.api_key",
        env_vars=("ANTHROPIC_API_KEY",),
    ),
    ModelProviderCredentialDefinition(
        provider="google",
        name="Google Gemini",
        credential_label="API key",
        secret_key="model_providers.google.api_key",
        env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ),
)

_PROVIDER_CREDENTIALS_BY_PROVIDER = {
    definition.provider: definition for definition in MODEL_PROVIDER_CREDENTIALS
}


def model_provider_credential_definition(
    provider: str,
) -> ModelProviderCredentialDefinition | None:
    """Credential metadata for a provider, or None when it carries no key.

    Covers every provider in the LangChain catalog, not only the four Lucent
    ships a key form for: an admin who installs ``langchain-groq`` must be able
    to save a Groq key the same way they save an OpenAI one. The canonical secret
    key shape (``model_providers.<provider>.api_key``) is shared by discovery, the
    engine, and the UI, so a provider can never be configured in two places.
    """
    from lucent.llm.langchain_providers import LANGCHAIN_PROVIDERS, normalize_provider

    key = normalize_provider(provider)
    definition = _PROVIDER_CREDENTIALS_BY_PROVIDER.get(key)
    if definition is not None:
        return definition
    from lucent.llm.langchain_providers import provider_is_simple_key

    spec = LANGCHAIN_PROVIDERS.get(key)
    if spec is None or not spec.api_key_env or not provider_is_simple_key(spec.name):
        # Local servers (ollama/custom) authenticate with nothing, or with a
        # backend-scoped key; providers that also need an endpoint, deployment,
        # or region are configured on an engine backend. Neither has a
        # workspace-level provider credential.
        return None
    return ModelProviderCredentialDefinition(
        provider=spec.name,
        name=spec.label,
        credential_label="API key",
        secret_key=f"model_providers.{spec.name}.api_key",
        env_vars=spec.api_key_env,
        help_text=(
            f"Used by the LangChain engine for {spec.label} models. Requires the "
            f"'{spec.package}' package."
        ),
    )


def model_provider_credential_definitions(
    *, include_catalog: bool = False
) -> list[ModelProviderCredentialDefinition]:
    """Return provider credential definitions for UI/API configuration surfaces.

    ``include_catalog=True`` appends every other LangChain provider that takes an
    API key, so the provider-credentials panel covers what the engine can
    actually reach rather than only what Lucent ships by default.
    """
    definitions = list(MODEL_PROVIDER_CREDENTIALS)
    if not include_catalog:
        return definitions
    from lucent.llm.langchain_providers import (
        LANGCHAIN_PROVIDERS,
        provider_is_simple_key,
        provider_package_installed,
    )

    known = {definition.provider for definition in definitions}
    for spec in LANGCHAIN_PROVIDERS.values():
        if spec.name in known or not spec.api_key_env:
            continue
        # Only advertise a provider this runtime can actually instantiate, and
        # only those a single key is enough for — the rest need an endpoint,
        # deployment, or region and belong on an engine backend instead.
        if not provider_is_simple_key(spec.name):
            continue
        if not provider_package_installed(spec.name):
            continue
        definitions.append(
            ModelProviderCredentialDefinition(
                provider=spec.name,
                name=spec.label,
                credential_label="API key",
                secret_key=f"model_providers.{spec.name}.api_key",
                env_vars=spec.api_key_env,
                help_text=f"Requires the '{spec.package}' package.",
            )
        )
        known.add(spec.name)
    return definitions


def model_provider_secret_scope(organization_id: str) -> SecretScope:
    """System-managed secret scope for workspace model-provider credentials."""
    return SecretScope(organization_id=str(organization_id), system_managed=True)


async def resolve_provider_credential(provider: str, *, org_id: str | None = None) -> str:
    """Resolve a model-provider API key: saved secret (DB) first, then env vars.

    Shared by discovery, the LangChain engine, and the native coding-engine paths
    so a provider key entered once under Settings → Models → LLM Providers serves
    every engine that uses that provider.
    """
    definition = model_provider_credential_definition(provider)
    if not definition:
        return ""
    if org_id:
        try:
            secret_provider = SecretRegistry.get()
            value = await secret_provider.get(
                definition.secret_key, model_provider_secret_scope(org_id)
            )
            if value and value.strip():
                return value.strip()
        except KeyError:
            pass
        except Exception:
            logger.debug("Failed to read saved %s credential", provider, exc_info=True)
    for env_var in definition.env_vars:
        env_value = os.environ.get(env_var, "").strip()
        if env_value:
            return env_value
    return ""


@dataclass(slots=True)
class DiscoveredModel:
    """Normalized model metadata discovered from a provider catalog."""

    id: str
    provider: str
    name: str
    category: str = "general"
    api_model_id: str = ""
    context_window: int = 0
    supports_tools: bool = True
    supports_vision: bool = False
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    reasoning_efforts: list[str] = field(default_factory=list)
    engine: str | None = None
    # Concrete engine backend this model runs on (endpoint + credentials). Set
    # for providers whose catalog belongs to one configured backend — a custom
    # OpenAI-compatible endpoint serves only the models it loads, so the binding
    # is part of the discovery result, not an admin follow-up step.
    backend_id: str | None = None
    model_class: str = "language"
    discovery_metadata: dict[str, Any] = field(default_factory=dict)

    def to_repo_kwargs(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["model_id"] = payload.pop("id")
        return payload


@dataclass(slots=True)
class ProviderDiscoveryResult:
    """Result for one provider discovery run."""

    provider: str
    configured: bool
    models: list[DiscoveredModel] = field(default_factory=list)
    error: str | None = None


#: Providers whose catalog comes from a coding engine rather than a vendor HTTP
#: endpoint. Their discovery is gated on engine availability, because asking an
#: engine that cannot run here may terminate the process rather than raise.
_ENGINE_CATALOG_PROVIDERS: frozenset[str] = frozenset({"copilot", "codex"})


def _engine_catalog_available(engine: str) -> bool:
    """Whether an engine advertises a catalog it can enumerate right now.

    Used so discovery asks a coding engine for its models only when the engine
    can actually answer — an uninstalled SDK should not make every run log a
    provider failure.
    """
    try:
        from lucent.llm.engine_registry import engine_availability, engine_spec

        spec = engine_spec(engine)
        if not spec.implemented or spec.discover is None:
            return False
        if not spec.capabilities.discovery:
            return False
        return engine_availability(engine).available
    except Exception:  # noqa: BLE001 — unknown or unprobeable engine
        return False


def _clean_model_id(model_id: str) -> str:
    """Normalize provider model IDs into Lucent model IDs."""
    model_id = model_id.strip()
    if model_id.startswith("models/"):
        return model_id.split("/", 1)[1]
    return model_id


def _display_name(model_id: str) -> str:
    """Best-effort human-readable name from a model ID."""
    cleaned = _clean_model_id(model_id)
    # Self-hosted servers name a model after its weight file: llama.cpp reports
    # "qwen2.5-coder-7b-instruct-q4_k_m.gguf". Neither the extension nor the
    # quantisation suffix belongs in a display name.
    cleaned = re.sub(r"\.gguf$", "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"[-_:]+", " ", cleaned).strip().title()


def _infer_category(model_id: str, name: str = "") -> str:
    text = f"{model_id} {name}".lower()
    if any(marker in text for marker in ("codex", "agent", "code")):
        return "agentic"
    if any(marker in text for marker in ("haiku", "mini", "nano", "flash", "fast")):
        return "fast"
    if any(
        marker in text
        for marker in ("opus", "pro", "reason", "thinking", "gpt-5", "o1", "o3", "o4")
    ):
        return "reasoning"
    return "general"


def _infer_tags(model_id: str, category: str, *, local: bool = False) -> list[str]:
    tags = {category}
    lowered = model_id.lower()
    if local:
        tags.add("local")
    if category in {"agentic", "general"} or any(x in lowered for x in ("code", "coder")):
        tags.add("coding")
    if any(x in lowered for x in ("flash", "mini", "nano", "fast")):
        tags.add("fast")
    if any(x in lowered for x in ("vision", "llava")):
        tags.add("vision")
    return sorted(tags)


def _trained_context_length(show: dict[str, Any], tags_row: dict[str, Any]) -> int:
    """Extract the model's trained context length (capability ceiling).

    Ollama scopes context keys by architecture (``<arch>.context_length``) in
    ``/api/show``'s ``model_info``; the exact prefix varies per architecture, so
    scan for any such key rather than a hardcoded allowlist. Falls back to the
    ``details.context_length`` hint from ``/api/tags`` when ``model_info`` is
    unavailable. Never uses ``/api/ps`` — that reports the runtime allocation,
    which is hardware-dependent and not the trained limit.
    """
    model_info = show.get("model_info") or {}
    arch = model_info.get("general.architecture")
    if arch and model_info.get(f"{arch}.context_length"):
        return int(model_info[f"{arch}.context_length"])
    for key, value in model_info.items():
        if key.endswith(".context_length"):
            return int(value)
    details = tags_row.get("details") or {}
    return int(details.get("context_length") or 0)


_REASONING_EFFORT_METADATA_KEYS = {
    "reasoningeffort",
    "reasoningefforts",
    "reasoningeffortlevels",
    "supportedreasoningefforts",
    "supportedreasoningeffortlevels",
    "thinkinglevel",
    "thinkinglevels",
    "supportedthinkinglevels",
    "effort",
    "efforts",
    "effortlevels",
    "supportedefforts",
    "supportedeffortlevels",
}

# Ollama reports only a boolean ``thinking`` capability from /api/tags and
# /api/show. Its chat API accepts these standardized values for models with that
# capability, but does not return a per-model enum in discovery responses.
_OLLAMA_REASONING_EFFORTS = ("low", "medium", "high")

# Decision-capable (System One) markers. Ollama 0.35+ does not yet document
# which capability string marks a decision model in /api/tags//api/show
# entries, so classify via two signals: any decision/systemone capability the
# server reports, plus the known System One model families. Capability
# detection wins over the family list so newly-published decision models are
# classified without a Lucent update.
_DECISION_CAPABILITY_MARKERS = {"decision", "systemone"}
_DECISION_MODEL_FAMILIES = ("nimble", "tev1", "clef")

# Version of Lucent's derivation logic for discovered fields (classification,
# category, capabilities mapping). When it changes, rows whose catalog digest
# is unchanged must still be refreshed once, because their stored derived
# fields were computed by an older version of this module. Bump on any change
# to discovery-derived field semantics.
_DERIVED_FIELDS_VERSION = "1"


def _metadata_key_name(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _normalize_discovered_reasoning_efforts(value: Any) -> list[str]:
    """Normalize provider-reported reasoning levels without a Lucent enum.

    Provider catalogs are not standardized. Some expose a direct list, some use
    ``values``/``options`` wrappers, and some only expose a boolean capability.
    Boolean support is intentionally ignored here: it tells us a knob exists, not
    which exact values are accepted for the model.
    """
    values: list[str] = []

    def add(raw: Any) -> None:
        if raw is None or isinstance(raw, bool):
            return
        if isinstance(raw, str):
            for part in raw.split(","):
                effort = part.strip().lower()
                if effort and effort not in values:
                    values.append(effort)
            return
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                add(item)
            return
        if isinstance(raw, dict):
            for candidate_key in (
                "values",
                "options",
                "levels",
                "supported_values",
                "supportedValues",
                "enum",
            ):
                if candidate_key in raw:
                    add(raw[candidate_key])
            return

    add(value)
    return values


def _extract_reasoning_efforts_from_metadata(*metadata_objects: Any) -> list[str]:
    """Extract selectable reasoning levels from provider-supplied metadata."""
    efforts: list[str] = []

    def merge(values: list[str]) -> None:
        for effort in values:
            if effort not in efforts:
                efforts.append(effort)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = _metadata_key_name(str(key))
                if normalized_key in _REASONING_EFFORT_METADATA_KEYS:
                    merge(_normalize_discovered_reasoning_efforts(nested))
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for metadata in metadata_objects:
        walk(metadata)
    return efforts


def _is_generation_model(model_id: str) -> bool:
    """Filter out obvious non-chat/generation model families."""
    lowered = model_id.lower()
    excluded = (
        "embedding",
        "embed",
        "whisper",
        "tts",
        "dall-e",
        "moderation",
        "babbage",
        "davinci",
        "image",
        "audio",
        "transcribe",
    )
    return not any(part in lowered for part in excluded)


class ModelDiscoveryService:
    """Discovers models from configured providers and syncs them to the DB."""

    def __init__(self, pool, *, timeout: float = 20.0):
        self.pool = pool
        self.repo = ModelRepository(pool)
        self.timeout = timeout
        self._show_semaphore = asyncio.Semaphore(8)
        # Digest-skip handoff from _discover_ollama() to sync(): ids whose
        # catalog digest was unchanged this run (timestamp-only updates).
        self._last_unchanged_model_ids: set[str] = set()

    async def configured_providers_async(
        self,
        providers: list[str] | None = None,
        *,
        org_id: str | None = None,
    ) -> list[str]:
        """Return providers configured via DB-backed secrets or env fallbacks."""
        requested = [p.strip().lower() for p in providers or [] if p.strip()]
        if requested:
            return requested

        out: list[str] = []
        for definition in MODEL_PROVIDER_CREDENTIALS:
            if not await self._provider_credential(definition.provider, org_id=org_id):
                continue
            # A coding engine's catalog is only worth asking for when the engine
            # can actually run here. `copilot` in particular terminates the
            # process when its CLI is missing, so an unattended background sweep
            # must not reach it.
            if definition.provider in _ENGINE_CATALOG_PROVIDERS:
                if not _engine_catalog_available(definition.provider):
                    continue
            out.append(definition.provider)
        if os.environ.get("COPILOT_CLI_PATH") and "copilot" not in out:
            out.append("copilot")
        if os.environ.get("OLLAMA_HOST"):
            out.append("ollama")
        # Custom endpoints are configured per organization as backends, not as a
        # credential, so the credential loop above never sees them. Include the
        # provider whenever the org has an endpoint to ask.
        if await self._has_custom_endpoint(org_id=org_id):
            out.append(CUSTOM_PROVIDER)
        # Coding engines that report their own catalogs. Codex is discovered
        # through the SDK's own list, so it only appears when the SDK is
        # installed; a failure there is reported per-provider, not fatally.
        if "codex" not in out and _engine_catalog_available("codex"):
            out.append("codex")
        return out

    async def _has_custom_endpoint(self, *, org_id: str | None = None) -> bool:
        """Whether the organization has an enabled OpenAI-compatible endpoint."""
        try:
            from lucent.db.backends import BackendRepository

            rows = await BackendRepository(self.pool).list(
                org_id=org_id, provider=CUSTOM_PROVIDER, enabled_only=True
            )
        except Exception:  # noqa: BLE001 — a probe must not break discovery
            return False
        return any(
            str((row.get("config") or {}).get("base_url") or "").strip() for row in rows
        )

    async def provider_configuration_statuses(
        self,
        *,
        org_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return UI-safe provider configuration status without secret values."""
        statuses: list[dict[str, Any]] = []
        for definition in model_provider_credential_definitions(include_catalog=True):
            source = "none"
            configured = False
            error: str | None = None
            try:
                if org_id and await self._provider_secret(definition.provider, org_id=org_id):
                    source = "database"
                    configured = True
                elif any(os.environ.get(env_var) for env_var in definition.env_vars):
                    source = "environment"
                    configured = True
                elif definition.provider == "copilot" and os.environ.get("COPILOT_CLI_PATH"):
                    source = "environment"
                    configured = True
            except Exception:
                error = "Stored credential could not be read. Save a new credential."

            statuses.append(
                {
                    "provider": definition.provider,
                    "name": definition.name,
                    "credential_label": definition.credential_label,
                    "help_text": definition.help_text,
                    "env_vars": list(definition.env_vars),
                    "secret_key": definition.secret_key,
                    "configured": configured,
                    "source": source,
                    "error": error,
                }
            )
        if os.environ.get("OLLAMA_HOST"):
            statuses.append(
                {
                    "provider": "ollama",
                    "name": "Ollama",
                    "credential_label": "Base URL",
                    "help_text": "Configured through OLLAMA_HOST for local model discovery.",
                    "env_vars": ["OLLAMA_HOST"],
                    "secret_key": "",
                    "configured": True,
                    "source": "environment",
                    "error": None,
                }
            )
        return statuses

    async def _provider_secret(self, provider: str, *, org_id: str | None = None) -> str:
        definition = model_provider_credential_definition(provider)
        if not definition or not org_id:
            return ""
        try:
            secret_provider = SecretRegistry.get()
        except KeyError:
            return ""
        value = await secret_provider.get(
            definition.secret_key,
            model_provider_secret_scope(org_id),
        )
        return (value or "").strip()

    async def _provider_credential(self, provider: str, *, org_id: str | None = None) -> str:
        definition = model_provider_credential_definition(provider)
        if not definition:
            return ""
        secret = await self._provider_secret(provider, org_id=org_id)
        if secret:
            return secret
        for env_var in definition.env_vars:
            value = os.environ.get(env_var, "").strip()
            if value:
                return value
        return ""

    async def discover(
        self,
        providers: list[str] | None = None,
        *,
        org_id: str | None = None,
    ) -> list[ProviderDiscoveryResult]:
        """Discover models from configured or explicitly requested providers."""
        provider_ids = await self.configured_providers_async(providers, org_id=org_id)
        if not provider_ids:
            return []

        results: list[ProviderDiscoveryResult] = []
        provider_results = await asyncio.gather(
            *(
                self._discover_provider_safe(provider, org_id=org_id)
                for provider in provider_ids
            )
        )
        for provider, outcome in zip(provider_ids, provider_results):
            models, error = outcome
            if error is not None:
                logger.warning("Model discovery failed for %s: %s", provider, error)
            results.append(
                ProviderDiscoveryResult(
                    provider=provider,
                    configured=True,
                    models=models,
                    error=error,
                )
            )
        return results

    async def sync(
        self,
        *,
        providers: list[str] | None = None,
        org_id: str | None = None,
        disable_missing: bool = False,
    ) -> dict[str, Any]:
        """Discover models and upsert them into ``models``.

        Manual/custom rows are preserved by repository-level merge semantics.
        ``disable_missing`` only affects rows previously discovered from a
        provider; manual rows are never disabled by provider discovery.
        """
        results = await self.discover(providers, org_id=org_id)
        provider_summaries: list[dict[str, Any]] = []
        total_models = 0
        total_upserted = 0
        unchanged_ids: set[str] = set(self._last_unchanged_model_ids)
        self._last_unchanged_model_ids = set()

        for result in results:
            summary: dict[str, Any] = {
                "provider": result.provider,
                "configured": result.configured,
                "discovered": len(result.models),
                "upserted": 0,
                "disabled_missing": 0,
            }
            if result.error:
                summary["error"] = result.error
                provider_summaries.append(summary)
                continue

            unchanged_for_provider = {
                m.id for m in result.models if m.id in unchanged_ids
            }
            if unchanged_for_provider:
                await self.repo.touch_discovered_at(sorted(unchanged_for_provider))
            models_to_sync = [
                m for m in result.models if m.id not in unchanged_for_provider
            ]
            if models_to_sync:
                sync_result = await self.repo.sync_discovered_models(
                    provider=result.provider,
                    models=[m.to_repo_kwargs() for m in models_to_sync],
                    org_id=org_id,
                    disable_missing=disable_missing,
                )
                summary.update(sync_result)
                summary["skipped_unchanged"] = len(unchanged_for_provider)
            else:
                summary["upserted"] = 0
                summary["skipped_unchanged"] = len(unchanged_for_provider)
            total_models += len(result.models)
            total_upserted += summary.get("upserted", 0)
            provider_summaries.append(summary)

        return {
            "providers": provider_summaries,
            "provider_count": len(provider_summaries),
            "discovered_count": total_models,
            "upserted_count": total_upserted,
            "errors": [p for p in provider_summaries if p.get("error")],
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _discover_provider(
        self,
        provider: str,
        *,
        org_id: str | None = None,
    ) -> list[DiscoveredModel]:
        # API providers are discovered here because the calls are plain HTTP
        # against each vendor's catalog endpoint and have nothing to do with an
        # engine. Everything that *is* an engine harness (Copilot, Claude Code,
        # Codex) reports its own list through the registry hook below, so this
        # module never has to learn another engine name.
        if provider in ("copilot", "github"):
            # Historical alias: `github` was the provider name before the
            # Copilot engine existed. Normalize it rather than branch twice.
            provider = "copilot"
        if provider == "openai":
            return await self._discover_openai(org_id=org_id)
        if provider == "anthropic":
            return await self._discover_anthropic(org_id=org_id)
        if provider in ("google", "google_genai", "gemini"):
            return await self._discover_google(org_id=org_id)
        if provider == "ollama":
            return await self._discover_ollama()
        if provider == CUSTOM_PROVIDER:
            return await self._discover_custom_endpoints(org_id=org_id)

        engine_models = await self._discover_via_engine_hook(provider, org_id=org_id)
        if engine_models is not None:
            return engine_models
        raise ValueError(f"Unsupported model provider: {provider}")

    async def _discover_via_engine_hook(
        self,
        provider: str,
        *,
        org_id: str | None = None,
    ) -> list[DiscoveredModel] | None:
        """Discover a provider through its engine's hook, or ``None`` if none.

        A provider name that matches an engine name with a ``discover`` hook is
        that engine's own catalog (Codex reports Codex models). Returning
        ``None`` means "not an engine catalog", so the caller can report the
        provider as unsupported.
        """
        from lucent.llm.engine_registry import ENGINE_REGISTRY, engine_availability

        spec = ENGINE_REGISTRY.get(provider)
        if spec is None or spec.discover is None:
            return None
        # Never invoke a hook for an engine this runtime cannot run. Some SDKs
        # fail by terminating the process instead of raising (the Copilot client
        # does exactly that when the CLI is missing), and a background sweep must
        # not take the server down with it.
        availability = engine_availability(provider)
        if not availability.available:
            raise ValueError(
                f"{provider} cannot enumerate its catalog: {availability.detail}"
            )
        rows = await spec.discover(org_id=org_id)
        models: list[DiscoveredModel] = []
        for row in rows or []:
            model_id = _clean_model_id(str(row["id"]))
            name = str(row.get("name") or model_id)
            category = _infer_category(model_id, name)
            tags = set(_infer_tags(model_id, category))
            if row.get("supports_vision"):
                tags.add("vision")
            if row.get("supports_reasoning_effort"):
                tags.add("reasoning-effort")
            metadata = dict(row.get("discovery_metadata") or {})
            reasoning_efforts = list(row.get("reasoning_efforts") or [])
            if not reasoning_efforts and metadata:
                # Engines report reasoning support in their own shapes; reuse the
                # provider-agnostic extractor rather than requiring each one to
                # normalize.
                reasoning_efforts = _extract_reasoning_efforts_from_metadata(metadata)
            models.append(
                DiscoveredModel(
                    id=model_id,
                    provider=provider,
                    name=name,
                    category=category,
                    api_model_id=str(row.get("api_model_id") or model_id),
                    context_window=int(row.get("context_window") or 0),
                    supports_tools=bool(row.get("supports_tools", True)),
                    supports_vision=bool(row.get("supports_vision", False)),
                    notes=str(row.get("notes") or ""),
                    tags=sorted(tags),
                    reasoning_efforts=reasoning_efforts,
                    engine=str(row.get("engine") or provider),
                    discovery_metadata=metadata,
                )
            )
        return models

    async def _discover_provider_safe(
        self,
        provider: str,
        *,
        org_id: str | None = None,
    ) -> tuple[list[DiscoveredModel], str | None]:
        """Discover one provider, returning (models, error) so one provider
        failing never blocks the others."""
        try:
            return await self._discover_provider(provider, org_id=org_id), None
        except Exception as exc:
            return [], str(exc)

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post_json(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=json or {})
            resp.raise_for_status()
            return resp.json()

    def _summarize_ollama_show(self, show: dict[str, Any]) -> dict[str, Any]:
        """Extract the fields the app actually uses from an ``/api/show`` payload.

        ``/api/show`` responses carry the full tensors array and the modelfile
        string — tens to hundreds of KB per model that nothing in Lucent reads.
        Persisting a compact summary instead keeps ``discovery_metadata`` small
        while preserving context-window, capability, and provenance details.
        """
        capabilities = [str(cap) for cap in (show.get("capabilities") or [])]
        model_info = show.get("model_info") or {}
        context_info = {
            key: value
            for key, value in model_info.items()
            if key.endswith(".context_length")
        }
        summary: dict[str, Any] = {
            "capabilities": capabilities,
            "model_info": context_info,
        }
        parameters = str(show.get("parameters") or "").strip()
        if parameters:
            summary["parameters"] = parameters
        license_info = show.get("license")
        if license_info:
            # Keep only a short identification line — full license text runs
            # ~10KB per model and nothing in Lucent reads it.
            license_text = (
                license_info
                if isinstance(license_info, str)
                else " ".join(str(part) for part in license_info)
            )
            short_license = license_text.strip().splitlines()[0].strip()[:200]
            if short_license:
                summary["license"] = short_license
        return summary

    async def _discover_openai(self, *, org_id: str | None = None) -> list[DiscoveredModel]:
        api_key = await self._provider_credential("openai", org_id=org_id)
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not configured")
        data = await self._get_json(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        rows = data.get("data", []) if isinstance(data, dict) else []
        models: list[DiscoveredModel] = []
        for row in rows:
            model_id = str(row.get("id") or "")
            if not model_id or not _is_generation_model(model_id):
                continue
            category = _infer_category(model_id)
            tags = _infer_tags(model_id, category)
            reasoning_efforts = _extract_reasoning_efforts_from_metadata(row)
            models.append(
                DiscoveredModel(
                    id=model_id,
                    provider="openai",
                    name=_display_name(model_id),
                    category=category,
                    api_model_id=model_id,
                    supports_tools=True,
                    supports_vision=any(x in model_id.lower() for x in ("gpt-4", "gpt-5", "o")),
                    notes=(
                        "Discovered from OpenAI model catalog. "
                        f"Owner: {row.get('owned_by', 'unknown')}"
                    ),
                    tags=tags,
                    reasoning_efforts=reasoning_efforts,
                    discovery_metadata=dict(row),
                )
            )
        return models

    async def _discover_anthropic(self, *, org_id: str | None = None) -> list[DiscoveredModel]:
        api_key = await self._provider_credential("anthropic", org_id=org_id)
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        after_id: str | None = None
        models: list[DiscoveredModel] = []
        while True:
            params = {"limit": 100}
            if after_id:
                params["after_id"] = after_id
            data = await self._get_json(
                "https://api.anthropic.com/v1/models",
                headers=headers,
                params=params,
            )
            rows = data.get("data", []) if isinstance(data, dict) else []
            for row in rows:
                model_id = str(row.get("id") or "")
                if not model_id:
                    continue
                name = str(row.get("display_name") or _display_name(model_id))
                category = _infer_category(model_id, name)
                tags = _infer_tags(model_id, category)
                capabilities = row.get("capabilities") or {}
                image_input = capabilities.get("image_input") or {}
                reasoning_efforts = _extract_reasoning_efforts_from_metadata(row)
                models.append(
                    DiscoveredModel(
                        id=model_id,
                        provider="anthropic",
                        name=name,
                        category=category,
                        api_model_id=model_id,
                        context_window=int(row.get("max_input_tokens") or 0),
                        supports_tools=True,
                        supports_vision=bool(image_input.get("supported")),
                        notes="Discovered from Anthropic model catalog.",
                        tags=tags,
                        reasoning_efforts=reasoning_efforts,
                        discovery_metadata=dict(row),
                    )
                )
            if not isinstance(data, dict) or not data.get("has_more"):
                break
            after_id = data.get("last_id")
            if not after_id:
                break
        return models

    async def _discover_google(self, *, org_id: str | None = None) -> list[DiscoveredModel]:
        api_key = await self._provider_credential("google", org_id=org_id)
        if not api_key:
            raise ValueError("GOOGLE_API_KEY or GEMINI_API_KEY is not configured")
        page_token: str | None = None
        models: list[DiscoveredModel] = []
        while True:
            params: dict[str, Any] = {"key": api_key, "pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            data = await self._get_json(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params=params,
            )
            rows = data.get("models", []) if isinstance(data, dict) else []
            for row in rows:
                methods = (
                    row.get("supportedGenerationMethods")
                    or row.get("supported_actions")
                    or []
                )
                if "generateContent" not in methods:
                    continue
                model_id = _clean_model_id(str(row.get("baseModelId") or row.get("name") or ""))
                if not model_id or not _is_generation_model(model_id):
                    continue
                name = str(row.get("displayName") or _display_name(model_id))
                category = _infer_category(model_id, name)
                tags = _infer_tags(model_id, category)
                reasoning_efforts = _extract_reasoning_efforts_from_metadata(row)
                models.append(
                    DiscoveredModel(
                        id=model_id,
                        provider="google",
                        name=name,
                        category=category,
                        api_model_id=model_id,
                        context_window=int(row.get("inputTokenLimit") or 0),
                        supports_tools=True,
                        supports_vision=True,
                        notes=str(
                            row.get("description")
                            or "Discovered from Google Gemini model catalog."
                        ),
                        tags=tags,
                        reasoning_efforts=reasoning_efforts,
                        discovery_metadata=dict(row),
                    )
                )
            page_token = data.get("nextPageToken") if isinstance(data, dict) else None
            if not page_token:
                break
        return models

    async def _discover_ollama(self) -> list[DiscoveredModel]:
        base_url = os.environ.get("OLLAMA_HOST", "").rstrip("/")
        if not base_url:
            raise ValueError("OLLAMA_HOST is not configured")
        if base_url.endswith("/api"):
            api_base = base_url
        else:
            api_base = f"{base_url}/api"
        data = await self._get_json(f"{api_base}/tags")
        rows = data.get("models", []) if isinstance(data, dict) else []

        # Persisted digests let unchanged models skip /api/show and the content
        # write entirely: capability and context values are static per digest.
        try:
            discovery_state = await self.repo.list_discovery_state()
        except Exception as exc:
            logger.warning("Model discovery could not read prior digests: %s", exc)
            discovery_state = {}

        async def fetch_show(model_id: str) -> dict[str, Any]:
            async with self._show_semaphore:
                return await self._post_json(f"{api_base}/show", json={"model": model_id})

        async def discover_row(row: dict[str, Any]) -> DiscoveredModel | None:
            model_id = str(row.get("model") or row.get("name") or "")
            if not model_id:
                return None
            details = row.get("details") or {}
            digest = str(row.get("digest") or "")
            previous = discovery_state.get(model_id) or {}
            metadata: dict[str, Any] = {}
            show: dict[str, Any] = {}
            unchanged = bool(digest) and previous.get("digest") == digest
            if unchanged and previous.get("derived_fields_version") != _DERIVED_FIELDS_VERSION:
                # Same catalog digest, but the stored derived fields
                # (classification, category, capability mapping) were computed
                # by an older derivation version — re-derive them once.
                logger.info(
                    "Model discovery derivation changed for %s; refreshing", model_id
                )
                unchanged = False
            if unchanged:
                # Digest match: /api/show would return identical data. Reuse the
                # persisted summary and only refresh the timestamp downstream.
                self._last_unchanged_model_ids.add(model_id)
                show = previous.get("show_summary") or {}
                metadata = dict(previous)
            else:
                try:
                    show_data = await asyncio.wait_for(
                        fetch_show(model_id), timeout=self.timeout
                    )
                    show = show_data if isinstance(show_data, dict) else {}
                except Exception as exc:
                    # Transient failure must not drop an existing row's data:
                    # fall back to the persisted summary for this pass.
                    logger.warning(
                        "Model discovery /api/show failed for %s: %s", model_id, exc
                    )
                    show = previous.get("show_summary") or {}
                metadata = {
                    "digest": digest,
                    "derived_fields_version": _DERIVED_FIELDS_VERSION,
                    "modified_at": row.get("modified_at"),
                    "tags_row": {
                        key: row[key]
                        for key in ("size", "parameter_size", "quantization_level")
                        if row.get(key) is not None
                    },
                    "show_summary": self._summarize_ollama_show(show) if show else {},
                }
            capabilities = {
                str(capability).lower()
                for capability in [
                    *(row.get("capabilities") or []),
                    *(show.get("capabilities") or []),
                ]
            }
            base_name = model_id.partition(":")[0].lower()
            is_decision = bool(
                capabilities & _DECISION_CAPABILITY_MARKERS
                or base_name.startswith(_DECISION_MODEL_FAMILIES)
            )
            supports_vision = "vision" in capabilities or "llava" in model_id.lower()
            reasoning_efforts = (
                list(_OLLAMA_REASONING_EFFORTS)
                if "thinking" in capabilities and not is_decision
                else []
            )
            if is_decision:
                # Decision models answer System One questions, not chat turns:
                # no chat-tool support, and their category reflects the
                # classification so admin/UI grouping separates them too.
                category = "decision"
                supports_tools = False
                tags = {"decision", "local"}
                if details.get("parameter_size"):
                    tags.add(str(details["parameter_size"]).lower())
                notes = (
                    "Decision model (System One: choice/noul/score via "
                    "/v1/systemone). Discovered from local Ollama server."
                )
                model_class = "decision"
            else:
                supports_tools = "tools" in capabilities
                category = _infer_category(model_id)
                tags = _infer_tags(model_id, category, local=True)
                if details.get("parameter_size"):
                    tags.append(str(details["parameter_size"]).lower())
                if supports_tools:
                    tags.append("tools")
                if reasoning_efforts:
                    tags.append("reasoning-effort")
                notes = "Discovered from local Ollama server."
                if reasoning_efforts:
                    notes += " Supports low, medium, and high thinking effort."
                if not supports_tools:
                    notes += " Ollama reports no structured tool-call support."
                model_class = "language"
            context_window = _trained_context_length(show, row)
            return DiscoveredModel(
                id=model_id,
                provider="ollama",
                name=_display_name(model_id),
                category=category,
                api_model_id=model_id,
                context_window=context_window,
                supports_tools=supports_tools,
                supports_vision=supports_vision,
                notes=notes,
                tags=sorted(set(tags)),
                reasoning_efforts=reasoning_efforts,
                # Default engine only — not a compatibility restriction. The
                # model can run on every engine in compatible_engines("ollama");
                # an explicit task/request backend pin overrides this default.
                engine="langchain",
                model_class=model_class,
                discovery_metadata=metadata,
            )

        rows = [row for row in rows if isinstance(row, dict)]
        results = await asyncio.gather(
            *(discover_row(row) for row in rows), return_exceptions=True
        )
        models: list[DiscoveredModel] = []
        for row, result in zip(rows, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Model discovery failed for %s: %s",
                    row.get("model") or row.get("name"),
                    result,
                )
                continue
            if result is not None:
                models.append(result)
        return models


    async def _discover_custom_endpoints(
        self, *, org_id: str | None = None
    ) -> list[DiscoveredModel]:
        """Discover models from the organization's OpenAI-compatible endpoints.

        A ``custom`` provider has no vendor catalog to ask: the catalog is
        whatever each configured endpoint serves. Every enabled ``custom``
        backend the organization can see is asked for ``GET <base>/v1/models``
        (llama.cpp's ``llama-server``, vLLM, LM Studio, LiteLLM and friends all
        answer it). Results are merged into one provider namespace because a
        model id is what a task names, not the endpoint behind it — the same id
        served by two endpoints stays one row, and binding is done per model via
        ``models.backend_id``.

        A single unreachable endpoint is reported via ``logger.warning`` and does
        not fail the others; discovery of every endpoint failing raises so the
        sync summary shows the provider as errored rather than silently empty.
        """
        from lucent.db.backends import BackendRepository

        try:
            backends = await BackendRepository(self.pool).list(
                org_id=org_id, provider=CUSTOM_PROVIDER, enabled_only=True
            )
        except Exception as exc:  # noqa: BLE001 — surface as a provider error
            raise ValueError(f"Could not read custom endpoint backends: {exc}") from exc

        targets = [
            backend
            for backend in backends
            if str((backend.get("config") or {}).get("base_url") or "").strip()
        ]
        if not targets:
            raise ValueError(
                "No custom endpoint is configured. Add one under "
                "Settings → Models → Custom endpoints."
            )

        discovered: dict[str, DiscoveredModel] = {}
        errors: list[str] = []
        for backend in targets:
            base_url = str((backend.get("config") or {}).get("base_url") or "").strip()
            name = str(backend.get("name") or base_url)
            try:
                rows = await self._list_openai_compatible_models(backend, base_url)
            except Exception as exc:  # noqa: BLE001 — one endpoint must not stop the rest
                logger.warning(
                    "Custom endpoint discovery failed for %s (%s): %s", name, base_url, exc
                )
                errors.append(f"{name}: {exc}")
                continue
            for model in rows:
                # First endpoint wins for a duplicate id, but record every
                # endpoint that serves it so the UI can show the choice.
                existing = discovered.get(model.id)
                if existing is None:
                    discovered[model.id] = model
                else:
                    served = list(
                        (existing.discovery_metadata.get("served_by") or [])
                    )
                    served.append({"backend_id": str(backend["id"]), "name": name})
                    existing.discovery_metadata["served_by"] = served

        if not discovered and errors:
            raise ValueError("; ".join(errors))
        if errors:
            logger.warning(
                "Custom endpoint discovery partially failed: %s", "; ".join(errors)
            )
        return list(discovered.values())

    async def _list_openai_compatible_models(
        self, backend: dict[str, Any], base_url: str
    ) -> list[DiscoveredModel]:
        """List models from one OpenAI-compatible endpoint."""
        url = self._openai_models_url(base_url)
        key = await self._backend_api_key(backend)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = await self._get_json(url, headers=headers or None)
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not rows and isinstance(data, dict) and isinstance(data.get("models"), list):
            # A few OSS servers use ``models`` instead of OpenAI's ``data``.
            rows = data["models"]

        models: list[DiscoveredModel] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(
                row.get("id") or row.get("model") or row.get("name") or ""
            ).strip()
            if not model_id:
                continue
            if not _is_generation_model(model_id):
                continue
            category = _infer_category(model_id)
            # Context window: llama.cpp reports it in ``meta.n_ctx_train`` (the
            # trained limit) alongside the runtime ``n_ctx``; vLLM uses
            # ``max_model_len``. Prefer the trained value — the runtime
            # allocation is per-deployment, not a model property.
            meta = row.get("meta") or {}
            models.append(
                DiscoveredModel(
                    id=model_id,
                    provider=CUSTOM_PROVIDER,
                    name=str(row.get("name") or _display_name(model_id)),
                    category=category,
                    api_model_id=model_id,
                    context_window=int(
                        row.get("context_length")
                        or row.get("max_model_len")
                        or row.get("context_window")
                        or (meta.get("n_ctx_train") if isinstance(meta, dict) else 0)
                        or 0
                    ),
                    # OpenAI-compatible servers do not advertise tool support in
                    # their model list. Assume yes (llama.cpp/vLLM both support
                    # it) and let an admin switch it off per model, mirroring how
                    # the Ollama path treats a missing capability.
                    supports_tools=bool(row.get("supports_tools", True)),
                    supports_vision=any(
                        marker in model_id.lower() for marker in ("vision", "llava", "vl")
                    ),
                    notes=(
                        "Discovered from a custom OpenAI-compatible endpoint "
                        f"({(backend.get('config') or {}).get('base_url')})."
                    ),
                    tags=sorted({"custom", "local", category}),
                    engine="langchain",
                    backend_id=str(backend["id"]),
                    discovery_metadata={
                        "endpoint": {
                            "backend_id": str(backend["id"]),
                            "name": backend.get("name"),
                            "base_url": base_url,
                        },
                        "served_by": [
                            {
                                "backend_id": str(backend["id"]),
                                "name": backend.get("name"),
                            }
                        ],
                        "catalog_row": {
                            key: row[key]
                            for key in ("id", "owned_by", "created")
                            if key in row
                        },
                    },
                )
            )
        return models

    @staticmethod
    def _openai_models_url(base_url: str) -> str:
        """Derive the model-list URL from what an admin typed.

        Operators paste every shape of base URL: ``http://llama:8080``,
        ``http://llama:8080/``, ``http://llama:8080/v1``, or a full
        ``.../v1/chat/completions``. Normalize rather than demanding one form.
        """
        base = base_url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions", "/models"):
            if base.endswith(suffix):
                base = base[: -len(suffix)].rstrip("/")
                break
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"

    async def _backend_api_key(self, backend: dict[str, Any]) -> str:
        """The API key a backend references, or "" when it carries none."""
        key = (backend.get("secret_refs") or {}).get("api_key")
        org_id = str(backend.get("organization_id") or "")
        if key and org_id:
            try:
                value = await SecretRegistry.get().get(
                    key, model_provider_secret_scope(org_id)
                )
                if value and value.strip():
                    return value.strip()
            except Exception:  # noqa: BLE001 — fall through to config
                logger.debug("Backend secret %s unreadable", key, exc_info=True)
        return str((backend.get("config") or {}).get("api_key") or "").strip()
