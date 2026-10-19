"""Engine connection-config field schemas.

Describes the settings an engine **backend** can carry — base URLs, wire
protocols, injected model ids, gateway URLs — so the admin UI, the API, and
validation can render and check them without hardcoding per-engine knowledge.

Two families of field:

* ``secret`` fields are stored in the secret provider (``SecretRegistry``) and
  only their key **names** appear in ``backends.secret_refs``.
* every other field is non-secret and lives in the ``backends.config`` JSONB,
  with an optional environment-variable fallback resolved DB → env.

This module is deliberately dependency-free (it imports nothing from Lucent) so
the engine registry, the DB layer, and the web/API surfaces can all import it
without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldType = Literal["secret", "string", "url", "bool", "enum"]


@dataclass(frozen=True, slots=True)
class EngineConfigField:
    """One configurable field on an engine backend."""

    key: str
    label: str
    field_type: FieldType
    required: bool = False
    env_var: str | None = None
    choices: tuple[str, ...] = ()
    help_text: str = ""

    @property
    def is_secret(self) -> bool:
        return self.field_type == "secret"


def secret_key_for(engine: str, backend_name: str, field: str) -> str:
    """Secret-provider key for an engine-backend field.

    Shape: ``engine.<engine>.<backend_name>.<field>``. Only the returned key
    name is persisted in ``backends.secret_refs`` — never the value.
    """
    return f"engine.{engine}.{backend_name}.{field}"


# ── Per-engine schemas ─────────────────────────────────────────────────────
# Declared in full so the taxonomy, validation, and UI stay complete; the
# registry reports each engine's runtime availability.

_LANGCHAIN_FIELDS: tuple[EngineConfigField, ...] = ()

_COPILOT_FIELDS: tuple[EngineConfigField, ...] = (
    # The GitHub token lives as the Copilot *provider* credential (surfaced in the
    # Copilot engine card), not as a per-backend field. The fields below are BYOK —
    # inject an OpenAI-compatible / Anthropic / Azure endpoint so models reachable
    # only through that endpoint can run inside Copilot.
    EngineConfigField(
        key="base_url",
        label="Provider base URL",
        field_type="url",
        env_var="COPILOT_PROVIDER_BASE_URL",
        help_text="BYOK endpoint (OpenAI-compatible, Azure, Anthropic). Ollama: http://localhost:11434",
    ),
    EngineConfigField(
        key="provider_type",
        label="Provider type",
        field_type="enum",
        env_var="COPILOT_PROVIDER_TYPE",
        choices=("openai", "azure", "anthropic"),
    ),
    EngineConfigField(
        key="api_key",
        label="Provider API key",
        field_type="secret",
        env_var="COPILOT_PROVIDER_API_KEY",
    ),
    EngineConfigField(
        key="model",
        label="Model id",
        field_type="string",
        env_var="COPILOT_MODEL",
        help_text="Model identifier required by BYOK; needs tool-calling + ~128k context.",
    ),
    EngineConfigField(
        key="offline",
        label="Offline (skip GitHub)",
        field_type="bool",
        env_var="COPILOT_OFFLINE",
    ),
)

_CLAUDE_CODE_FIELDS: tuple[EngineConfigField, ...] = (
    EngineConfigField(
        key="gateway_url",
        label="Anthropic-format gateway URL",
        field_type="url",
        env_var="ANTHROPIC_BASE_URL",
        help_text=(
            "Claude Code speaks only the Anthropic Messages API. Point this at a "
            "translation gateway (LiteLLM / claude-code-router) to use open models."
        ),
    ),
    EngineConfigField(
        key="gateway_token",
        label="Gateway auth token",
        field_type="secret",
        env_var="ANTHROPIC_AUTH_TOKEN",
    ),
    EngineConfigField(
        key="model",
        label="Model id",
        field_type="string",
        env_var="ANTHROPIC_MODEL",
    ),
    EngineConfigField(
        key="fallback_model",
        label="Fallback model id",
        field_type="string",
        env_var="ANTHROPIC_SMALL_FAST_MODEL",
    ),
)

_CODEX_FIELDS: tuple[EngineConfigField, ...] = (
    EngineConfigField(
        key="api_key",
        label="API key",
        field_type="secret",
        env_var="OPENAI_API_KEY",
    ),
    EngineConfigField(
        key="base_url",
        label="Provider base URL",
        field_type="url",
        help_text="Responses-API-speaking endpoint; Ollama uses native OSS mode instead.",
    ),
    EngineConfigField(
        key="wire_api",
        label="Wire API",
        field_type="enum",
        choices=("responses",),
        help_text="Codex custom providers support only the 'responses' wire API.",
    ),
    EngineConfigField(
        key="oss_provider",
        label="Local (OSS) provider",
        field_type="enum",
        choices=("ollama", "lmstudio"),
        help_text="Use --oss with this local provider to run open models.",
    ),
    EngineConfigField(
        key="model",
        label="Model id",
        field_type="string",
    ),
)

ENGINE_CONFIG_SCHEMAS: dict[str, tuple[EngineConfigField, ...]] = {
    "langchain": _LANGCHAIN_FIELDS,
    "copilot": _COPILOT_FIELDS,
    "claude_code": _CLAUDE_CODE_FIELDS,
    "codex": _CODEX_FIELDS,
}


def engine_config_schema(engine: str) -> tuple[EngineConfigField, ...]:
    """Return the config fields for an engine (empty tuple if none)."""
    return ENGINE_CONFIG_SCHEMAS.get(engine, ())
