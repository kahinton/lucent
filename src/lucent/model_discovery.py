"""Provider-backed model discovery and database synchronization.

LangChain initializes chat models across providers, but it does not expose a
single provider-agnostic catalog API. Discovery therefore uses each configured
provider's own model-listing endpoint, plus the Copilot SDK's ``list_models``
method for Copilot-hosted models.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from lucent.db.models import ModelRepository
from lucent.logging import get_logger

logger = get_logger("model_discovery")


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
    engine: str | None = None
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


def _clean_model_id(model_id: str) -> str:
    """Normalize provider model IDs into Lucent model IDs."""
    model_id = model_id.strip()
    if model_id.startswith("models/"):
        return model_id.split("/", 1)[1]
    return model_id


def _display_name(model_id: str) -> str:
    """Best-effort human-readable name from a model ID."""
    return re.sub(r"[-_:]+", " ", _clean_model_id(model_id)).strip().title()


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

    def configured_providers(self, providers: list[str] | None = None) -> list[str]:
        """Return providers with enough local configuration to attempt discovery."""
        requested = [p.strip().lower() for p in providers or [] if p.strip()]
        if requested:
            return requested

        out: list[str] = []
        if os.environ.get("OPENAI_API_KEY"):
            out.append("openai")
        if os.environ.get("ANTHROPIC_API_KEY"):
            out.append("anthropic")
        if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
            out.append("google")
        if os.environ.get("OLLAMA_HOST"):
            out.append("ollama")
        if os.environ.get("GITHUB_TOKEN") or os.environ.get("COPILOT_CLI_PATH"):
            out.append("copilot")
        return out

    async def discover(self, providers: list[str] | None = None) -> list[ProviderDiscoveryResult]:
        """Discover models from configured or explicitly requested providers."""
        provider_ids = self.configured_providers(providers)
        if not provider_ids:
            return []

        results: list[ProviderDiscoveryResult] = []
        for provider in provider_ids:
            try:
                models = await self._discover_provider(provider)
                results.append(
                    ProviderDiscoveryResult(provider=provider, configured=True, models=models)
                )
            except Exception as exc:
                logger.warning("Model discovery failed for %s: %s", provider, exc)
                results.append(
                    ProviderDiscoveryResult(
                        provider=provider,
                        configured=True,
                        error=str(exc),
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
        results = await self.discover(providers)
        provider_summaries: list[dict[str, Any]] = []
        total_models = 0
        total_upserted = 0

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

            sync_result = await self.repo.sync_discovered_models(
                provider=result.provider,
                models=[m.to_repo_kwargs() for m in result.models],
                org_id=org_id,
                disable_missing=disable_missing,
            )
            summary.update(sync_result)
            total_models += len(result.models)
            total_upserted += sync_result.get("upserted", 0)
            provider_summaries.append(summary)

        return {
            "providers": provider_summaries,
            "provider_count": len(provider_summaries),
            "discovered_count": total_models,
            "upserted_count": total_upserted,
            "errors": [p for p in provider_summaries if p.get("error")],
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _discover_provider(self, provider: str) -> list[DiscoveredModel]:
        if provider == "openai":
            return await self._discover_openai()
        if provider == "anthropic":
            return await self._discover_anthropic()
        if provider in ("google", "google_genai", "gemini"):
            return await self._discover_google()
        if provider == "ollama":
            return await self._discover_ollama()
        if provider in ("copilot", "github"):
            return await self._discover_copilot()
        raise ValueError(f"Unsupported model provider: {provider}")

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

    async def _discover_openai(self) -> list[DiscoveredModel]:
        api_key = os.environ.get("OPENAI_API_KEY", "")
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
                    tags=_infer_tags(model_id, category),
                    discovery_metadata=dict(row),
                )
            )
        return models

    async def _discover_anthropic(self) -> list[DiscoveredModel]:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
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
                capabilities = row.get("capabilities") or {}
                image_input = capabilities.get("image_input") or {}
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
                        tags=_infer_tags(model_id, category),
                        discovery_metadata=dict(row),
                    )
                )
            if not isinstance(data, dict) or not data.get("has_more"):
                break
            after_id = data.get("last_id")
            if not after_id:
                break
        return models

    async def _discover_google(self) -> list[DiscoveredModel]:
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
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
                        tags=_infer_tags(model_id, category),
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
        models: list[DiscoveredModel] = []
        for row in rows:
            model_id = str(row.get("model") or row.get("name") or "")
            if not model_id:
                continue
            show: dict[str, Any] = {}
            try:
                show_data = await self._post_json(f"{api_base}/show", json={"model": model_id})
                show = show_data if isinstance(show_data, dict) else {}
            except Exception:
                show = {}
            category = _infer_category(model_id)
            details = row.get("details") or {}
            model_info = show.get("model_info") or {}
            context_window = int(
                model_info.get("llama.context_length")
                or model_info.get("qwen2.context_length")
                or model_info.get("gemma.context_length")
                or 0
            )
            capabilities = show.get("capabilities") or []
            supports_vision = "vision" in capabilities or "llava" in model_id.lower()
            tags = _infer_tags(model_id, category, local=True)
            if details.get("parameter_size"):
                tags.append(str(details["parameter_size"]).lower())
            models.append(
                DiscoveredModel(
                    id=model_id,
                    provider="ollama",
                    name=_display_name(model_id),
                    category=category,
                    api_model_id=model_id,
                    context_window=context_window,
                    supports_tools=True,
                    supports_vision=supports_vision,
                    notes="Discovered from local Ollama server.",
                    tags=sorted(set(tags)),
                    engine="langchain",
                    discovery_metadata={"tags_row": dict(row), "show": show},
                )
            )
        return models

    async def _discover_copilot(self) -> list[DiscoveredModel]:
        try:
            from copilot import CopilotClient, SubprocessConfig

            from lucent.llm.copilot_engine import resolve_copilot_cli_path
        except ImportError as exc:
            raise ValueError("github-copilot-sdk is not installed") from exc

        config_kwargs: dict[str, Any] = {"log_level": "warning"}
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if github_token:
            config_kwargs["github_token"] = github_token
        cli_path = resolve_copilot_cli_path()
        if cli_path:
            config_kwargs["cli_path"] = cli_path

        client = CopilotClient(config=SubprocessConfig(**config_kwargs))
        try:
            await client.start()
            rows = await client.list_models()
        finally:
            await client.stop()

        models: list[DiscoveredModel] = []
        for row in rows:
            model_id = row.id
            category = _infer_category(model_id, row.name)
            capabilities = row.capabilities
            limits = capabilities.limits
            supports = capabilities.supports
            metadata = row.to_dict()
            tags = _infer_tags(model_id, category)
            if supports.reasoning_effort:
                tags.append("reasoning-effort")
            models.append(
                DiscoveredModel(
                    id=model_id,
                    provider="copilot",
                    name=row.name,
                    category=category,
                    api_model_id=model_id,
                    context_window=int(
                        limits.max_context_window_tokens or limits.max_prompt_tokens or 0
                    ),
                    supports_tools=True,
                    supports_vision=bool(supports.vision),
                    notes="Discovered from GitHub Copilot SDK model list.",
                    tags=sorted(set(tags)),
                    engine="copilot",
                    discovery_metadata=metadata,
                )
            )
        return models
