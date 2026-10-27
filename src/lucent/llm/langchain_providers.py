"""Catalog of model providers reachable through the LangChain engine.

Lucent's general LLM layer is provider-agnostic: ``init_chat_model()`` takes a
``model_provider`` id (``openai``, ``anthropic``, ``deepseek``, ``groq``, ...)
plus provider-specific kwargs. Before this module the engine only knew four
providers (``anthropic``/``openai``/``google``/``ollama``) and every other
provider was rejected at model-creation time, so an installed
``langchain-groq`` package was unusable.

This module is the single source of truth for **which providers LangChain can
serve in this build**, what integration package each needs, and which constructor
kwargs the integration accepts. Everything else (the engine registry's
provider→engine map, model validation, discovery, UIs) derives from it.

Two shapes of provider matter:

* **Hosted API** (``openai``, ``anthropic``, ``deepseek``, ...): a vendor
  endpoint reached with a provider API key.
* **Self-hosted / OpenAI-compatible** (``ollama``, ``custom``): a server the
  operator runs — Ollama, or ``llama.cpp``'s ``llama-server``, vLLM, LM Studio,
  a LiteLLM gateway. ``custom`` is the one that says "whatever
  OpenAI-compatible endpoint the admin configured on a backend"; it maps onto
  LangChain's ``openai`` integration with a per-backend ``base_url``.

The catalog is static on purpose. Importing it must never import LangChain (the
package is optional), so package availability is probed with
``importlib.util.find_spec`` and reported separately from the declared catalog.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.util import find_spec

#: Provider whose endpoint the admin supplies on a backend (llama.cpp, vLLM,
#: LM Studio, LiteLLM, ...). It is not a vendor: it has no API key of its own and
#: no catalog endpoint, so it is unusable without a backend row.
CUSTOM_PROVIDER = "custom"


@dataclass(frozen=True, slots=True)
class LangChainProviderSpec:
    """One provider the LangChain engine can reach."""

    name: str  # Lucent provider name, as stored in models.provider
    label: str
    #: The ``model_provider`` value ``init_chat_model()`` expects. Usually the
    #: same as ``name``; ``custom`` maps onto LangChain's ``openai`` integration.
    langchain_provider: str
    #: Integration package that must be importable.
    package: str
    #: Environment variables consulted when no credential is stored in the DB.
    api_key_env: tuple[str, ...] = ()
    #: Whether the integration accepts ``api_key=``.
    accepts_api_key: bool = True
    #: Whether the integration accepts ``base_url=`` (needed to point the
    #: provider at a self-hosted or gateway endpoint).
    accepts_base_url: bool = True
    #: Environment variable that can supply the base URL when a backend does not.
    base_url_env: str | None = None
    #: Base URL used when neither a backend nor the environment supplies one.
    default_base_url: str | None = None
    #: Whether a base URL is mandatory before this provider can run.
    requires_base_url: bool = False
    #: Whether a single API key is enough to use the provider. Providers whose
    #: integration also needs an endpoint, deployment, region, or project are
    #: configured through an engine backend instead — showing them a one-field
    #: key form would look configured and still fail.
    simple_key_only: bool = True
    notes: str = ""

    @property
    def canonical_provider(self) -> str:
        """Provider name to use for credentials and catalogs."""
        return self.name


# Order defines the order returned by ``langchain_provider_names()`` and shown in
# provider pickers. Hosted providers first (alphabetical), then local servers.
_SPECS: tuple[LangChainProviderSpec, ...] = (
    LangChainProviderSpec(
        name="anthropic",
        label="Anthropic API",
        langchain_provider="anthropic",
        package="langchain_anthropic",
        api_key_env=("ANTHROPIC_API_KEY",),
    ),
    LangChainProviderSpec(
        name="azure_ai",
        label="Azure AI",
        langchain_provider="azure_ai",
        package="langchain_azure_ai",
        api_key_env=("AZURE_AI_API_KEY",),
        accepts_base_url=False,
        simple_key_only=False,
        notes="Azure AI needs an endpoint and deployment; configure a backend.",
    ),
    LangChainProviderSpec(
        name="azure_openai",
        label="Azure OpenAI",
        langchain_provider="azure_openai",
        package="langchain_openai",
        api_key_env=("AZURE_OPENAI_API_KEY",),
        accepts_base_url=False,
        simple_key_only=False,
        notes="Azure OpenAI needs an endpoint and deployment; configure a backend.",
    ),
    LangChainProviderSpec(
        name="baseten",
        label="Baseten",
        langchain_provider="baseten",
        package="langchain_baseten",
        api_key_env=("BASETEN_API_KEY",),
    ),
    LangChainProviderSpec(
        name="bedrock",
        label="Amazon Bedrock",
        langchain_provider="bedrock",
        package="langchain_aws",
        api_key_env=("AWS_ACCESS_KEY_ID",),
        accepts_base_url=False,
        simple_key_only=False,
        notes="Bedrock authenticates with AWS credentials and a region.",
    ),
    LangChainProviderSpec(
        name="bedrock_converse",
        label="Amazon Bedrock (Converse)",
        langchain_provider="bedrock_converse",
        package="langchain_aws",
        api_key_env=("AWS_ACCESS_KEY_ID",),
        accepts_base_url=False,
        simple_key_only=False,
        notes="Bedrock authenticates with AWS credentials and a region.",
    ),
    LangChainProviderSpec(
        name="cohere",
        label="Cohere",
        langchain_provider="cohere",
        package="langchain_cohere",
        api_key_env=("COHERE_API_KEY",),
        accepts_base_url=False,
    ),
    LangChainProviderSpec(
        name="deepseek",
        label="DeepSeek",
        langchain_provider="deepseek",
        package="langchain_deepseek",
        api_key_env=("DEEPSEEK_API_KEY",),
    ),
    LangChainProviderSpec(
        name="fireworks",
        label="Fireworks AI",
        langchain_provider="fireworks",
        package="langchain_fireworks",
        api_key_env=("FIREWORKS_API_KEY",),
    ),
    LangChainProviderSpec(
        name="google",
        label="Google Gemini",
        langchain_provider="google_genai",
        package="langchain_google_genai",
        api_key_env=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        # ChatGoogleGenerativeAI does not take base_url/api_key in the same shape.
        accepts_base_url=False,
    ),
    LangChainProviderSpec(
        name="google_vertexai",
        label="Google Vertex AI",
        langchain_provider="google_vertexai",
        package="langchain_google_vertexai",
        api_key_env=(),
        accepts_api_key=False,
        accepts_base_url=False,
        simple_key_only=False,
        notes="Vertex AI authenticates with a GCP project and service credentials.",
    ),
    LangChainProviderSpec(
        name="groq",
        label="Groq",
        langchain_provider="groq",
        package="langchain_groq",
        api_key_env=("GROQ_API_KEY",),
    ),
    LangChainProviderSpec(
        name="huggingface",
        label="Hugging Face",
        langchain_provider="huggingface",
        package="langchain_huggingface",
        api_key_env=("HUGGINGFACEHUB_API_TOKEN", "HF_TOKEN"),
        accepts_base_url=False,
        simple_key_only=False,
        notes="ChatHuggingFace wraps an inference endpoint or a local model.",
    ),
    LangChainProviderSpec(
        name="ibm",
        label="IBM watsonx",
        langchain_provider="ibm",
        package="langchain_ibm",
        api_key_env=("WATSONX_APIKEY",),
        accepts_base_url=False,
        simple_key_only=False,
        notes="watsonx needs a project id and service URL; configure a backend.",
    ),
    LangChainProviderSpec(
        name="litellm",
        label="LiteLLM gateway",
        langchain_provider="litellm",
        package="langchain_litellm",
        api_key_env=("LITELLM_API_KEY",),
        base_url_env="LITELLM_BASE_URL",
        notes="One gateway in front of many vendors; set its base URL per backend.",
    ),
    LangChainProviderSpec(
        name="meta",
        label="Meta",
        langchain_provider="meta",
        package="langchain_meta",
        api_key_env=("META_API_KEY",),
    ),
    LangChainProviderSpec(
        name="mistralai",
        label="Mistral AI",
        langchain_provider="mistralai",
        package="langchain_mistralai",
        api_key_env=("MISTRAL_API_KEY",),
    ),
    LangChainProviderSpec(
        name="nvidia",
        label="NVIDIA NIM",
        langchain_provider="nvidia",
        package="langchain_nvidia_ai_endpoints",
        api_key_env=("NVIDIA_API_KEY",),
    ),
    LangChainProviderSpec(
        name="openai",
        label="OpenAI API",
        langchain_provider="openai",
        package="langchain_openai",
        api_key_env=("OPENAI_API_KEY",),
        base_url_env="OPENAI_BASE_URL",
        # No ``default_base_url``: ``langchain-openai`` already defaults to the
        # public API, and passing it explicitly would override an operator's own
        # OPENAI_BASE_URL handling in the client.
    ),
    LangChainProviderSpec(
        name="openrouter",
        label="OpenRouter",
        langchain_provider="openrouter",
        package="langchain_openrouter",
        api_key_env=("OPENROUTER_API_KEY",),
    ),
    LangChainProviderSpec(
        name="perplexity",
        label="Perplexity",
        langchain_provider="perplexity",
        package="langchain_perplexity",
        api_key_env=("PERPLEXITY_API_KEY",),
    ),
    LangChainProviderSpec(
        name="together",
        label="Together AI",
        langchain_provider="together",
        package="langchain_together",
        api_key_env=("TOGETHER_API_KEY",),
    ),
    LangChainProviderSpec(
        name="upstage",
        label="Upstage",
        langchain_provider="upstage",
        package="langchain_upstage",
        api_key_env=("UPSTAGE_API_KEY",),
    ),
    LangChainProviderSpec(
        name="xai",
        label="xAI",
        langchain_provider="xai",
        package="langchain_xai",
        api_key_env=("XAI_API_KEY",),
    ),
    # ── Self-hosted servers ───────────────────────────────────────────────
    LangChainProviderSpec(
        name="ollama",
        label="Ollama",
        langchain_provider="ollama",
        package="langchain_ollama",
        api_key_env=(),
        accepts_api_key=False,
        base_url_env="OLLAMA_HOST",
        default_base_url="http://localhost:11434",
    ),
    LangChainProviderSpec(
        name=CUSTOM_PROVIDER,
        label="Custom endpoint",
        # An OpenAI-compatible server: llama.cpp's llama-server, vLLM, LM Studio,
        # text-generation-webui, a LiteLLM gateway, ...
        langchain_provider="openai",
        package="langchain_openai",
        api_key_env=(),
        requires_base_url=True,
        notes=(
            "An OpenAI-compatible endpoint you host (llama.cpp, vLLM, LM Studio, "
            "LiteLLM...). Its address and key live on the engine backend."
        ),
    ),
)

LANGCHAIN_PROVIDERS: dict[str, LangChainProviderSpec] = {s.name: s for s in _SPECS}

#: Alternate provider names callers still pass (``google_genai`` is LangChain's
#: id for Gemini; ``gemini`` shows up in env vars and user input).
_PROVIDER_ALIASES: dict[str, str] = {
    "google_genai": "google",
    "gemini": "google",
    "github": "copilot",
}


class UnknownLangChainProviderError(ValueError):
    """Raised when a provider is not in the LangChain catalog."""


def normalize_provider(name: str | None) -> str:
    """Canonicalize a provider name (``google_genai`` → ``google``)."""
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return ""
    alias = _PROVIDER_ALIASES.get(cleaned)
    if alias and alias in LANGCHAIN_PROVIDERS:
        return alias
    return cleaned


def langchain_provider_spec(name: str | None) -> LangChainProviderSpec:
    """Return the spec for a provider, or raise :class:`UnknownLangChainProviderError`."""
    cleaned = normalize_provider(name)
    spec = LANGCHAIN_PROVIDERS.get(cleaned)
    if spec is None:
        raise UnknownLangChainProviderError(
            f"Unknown LangChain provider: {name!r}. Known providers: "
            + ", ".join(langchain_provider_names())
        )
    return spec


def is_langchain_provider(name: str | None) -> bool:
    """Whether a name refers to a provider the LangChain engine can serve."""
    return normalize_provider(name) in LANGCHAIN_PROVIDERS


def langchain_provider_names() -> list[str]:
    """Canonical provider names in catalog order."""
    return [spec.name for spec in _SPECS]


def provider_package_installed(name: str | None) -> bool:
    """Whether the provider's LangChain integration package is importable."""
    try:
        spec = langchain_provider_spec(name)
    except UnknownLangChainProviderError:
        return False
    return find_spec(spec.package) is not None


def langchain_model_provider(name: str | None) -> str:
    """The ``model_provider`` value to hand ``init_chat_model()``.

    Falls back to the provider name itself for providers outside the catalog so
    an operator-supplied custom provider id is passed through rather than
    silently replaced.
    """
    try:
        return langchain_provider_spec(name).langchain_provider
    except UnknownLangChainProviderError:
        return (name or "").strip().lower()


def resolve_provider_base_url(
    name: str | None,
    *,
    configured_base_url: str | None = None,
) -> str:
    """Resolve a provider base URL: explicit config → env → catalog default.

    Only providers flagged ``accepts_base_url`` have one; everything else
    returns an empty string so callers can tell "not configured" from "default".
    """
    try:
        spec = langchain_provider_spec(name)
    except UnknownLangChainProviderError:
        return (configured_base_url or "").strip().rstrip("/")
    if not spec.accepts_base_url:
        return ""
    if configured_base_url and configured_base_url.strip():
        return configured_base_url.strip().rstrip("/")
    if spec.base_url_env:
        env_value = os.environ.get(spec.base_url_env, "").strip()
        if env_value:
            return env_value.rstrip("/")
    return (spec.default_base_url or "").rstrip("/")


def provider_accepts_base_url(name: str | None) -> bool:
    """Whether the provider's integration accepts ``base_url=``."""
    try:
        return langchain_provider_spec(name).accepts_base_url
    except UnknownLangChainProviderError:
        return False


def provider_accepts_api_key(name: str | None) -> bool:
    """Whether the provider's integration accepts ``api_key=``."""
    try:
        return langchain_provider_spec(name).accepts_api_key
    except UnknownLangChainProviderError:
        return False


def provider_requires_base_url(name: str | None) -> bool:
    """Whether a provider cannot run without an explicit base URL."""
    try:
        return langchain_provider_spec(name).requires_base_url
    except UnknownLangChainProviderError:
        return False


def provider_is_self_hosted(name: str | None) -> bool:
    """Whether the provider is a server the operator runs (no vendor catalog).

    Used wherever code must decide between "ask the vendor" and "ask the
    configured endpoint" — model catalogs, execution targets, and validation.
    """
    key = normalize_provider(name)
    if key == CUSTOM_PROVIDER:
        return True
    try:
        from lucent.llm.local_providers import is_local_provider

        return is_local_provider(key)
    except Exception:  # pragma: no cover
        return key == "ollama"


def provider_api_key_env(name: str | None) -> tuple[str, ...]:
    """Environment variables that may supply the provider's API key.

    Used by UIs to tell an admin which env fallback applies to a saved credential.
    """
    try:
        return langchain_provider_spec(name).api_key_env
    except UnknownLangChainProviderError:
        return ()


def provider_api_key_env(name: str | None) -> tuple[str, ...]:
    """Environment variables that may supply the provider's API key."""
    try:
        return langchain_provider_spec(name).api_key_env
    except UnknownLangChainProviderError:
        return ()


def provider_is_simple_key(name: str | None) -> bool:
    """Whether a single API key is all this provider needs to be usable.

    Providers needing an endpoint/deployment/region are configured through an
    engine backend, so they must not appear in the one-field credential list.
    """
    try:
        return langchain_provider_spec(name).simple_key_only
    except UnknownLangChainProviderError:
        return False


def installed_langchain_provider_names() -> list[str]:
    """Catalog providers whose integration package is importable here."""
    return [spec.name for spec in _SPECS if provider_package_installed(spec.name)]
