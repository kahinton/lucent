"""Normalize provider-native LLM token usage into Lucent's ledger schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _value(source: Any, *names: str) -> Any:
    for name in names:
        value = source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)
        if value is not None:
            return value
    return None


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def normalize_token_usage(usage: Any) -> dict[str, Any] | None:
    """Return explicit provider usage fields, or ``None`` when none were reported."""
    if usage is None:
        return None
    input_details = _value(usage, "input_token_details", "input_tokens_details") or {}
    output_details = _value(usage, "output_token_details", "output_tokens_details") or {}
    raw_values = {
        "input_tokens": _value(usage, "input_tokens", "inputTokens", "prompt_tokens"),
        "output_tokens": _value(usage, "output_tokens", "outputTokens", "completion_tokens"),
        "cache_read_tokens": _value(
            usage,
            "cache_read_tokens",
            "cacheReadTokens",
            "cached_input",
            "cachedInput",
            "cache_read_input_tokens",
        ),
        "cache_write_tokens": _value(
            usage,
            "cache_write_tokens",
            "cacheWriteTokens",
            "cache_creation_input_tokens",
            "cache_creation_tokens",
        ),
        "reasoning_tokens": _value(usage, "reasoning_tokens", "reasoningTokens"),
    }
    raw_values["cache_read_tokens"] = raw_values["cache_read_tokens"] or _value(
        input_details,
        "cache_read",
        "cache_read_tokens",
        "cacheReadTokens",
        "cached_tokens",
        "cachedTokens",
    )
    raw_values["cache_write_tokens"] = raw_values["cache_write_tokens"] or _value(
        input_details,
        "cache_creation",
        "cache_write",
        "cache_write_tokens",
        "cache_creation_input_tokens",
    )
    raw_values["reasoning_tokens"] = raw_values["reasoning_tokens"] or _value(
        output_details, "reasoning", "reasoning_tokens", "reasoningTokens"
    )
    if not any(value is not None for value in raw_values.values()):
        return None

    normalized = {
        field: _token_count(raw_values[field]) or 0 for field in _TOKEN_FIELDS
    }
    provider_call_id = _value(
        usage, "provider_call_id", "providerCallId", "api_call_id", "apiCallId"
    )
    provider_model = _value(usage, "model")
    normalized["provider_call_id"] = str(provider_call_id) if provider_call_id else None
    normalized["provider_metadata"] = (
        {"provider_model": str(provider_model)} if provider_model else {}
    )
    return normalized
