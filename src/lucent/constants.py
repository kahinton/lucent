"""Shared constants for the Lucent platform.

Canonical value sets used across API models, DB repositories, and MCP tools.
"""

# Valid source values for requests.
VALID_REQUEST_SOURCES: frozenset[str] = frozenset(
    {"user", "cognitive", "api", "daemon", "schedule"}
)

# Regex pattern matching any valid request source (for Pydantic Field patterns).
REQUEST_SOURCE_PATTERN: str = "^(" + "|".join(sorted(VALID_REQUEST_SOURCES)) + ")$"
