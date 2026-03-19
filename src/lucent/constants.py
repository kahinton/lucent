"""Shared constants for the Lucent platform.

Canonical value sets used across API models, DB repositories, and MCP tools.
"""

# Valid source values for requests.
VALID_REQUEST_SOURCES: frozenset[str] = frozenset(
    {"user", "cognitive", "api", "daemon", "schedule"}
)

# Regex pattern matching any valid request source (for Pydantic Field patterns).
REQUEST_SOURCE_PATTERN: str = "^(" + "|".join(sorted(VALID_REQUEST_SOURCES)) + ")$"

# Canonical status values for requests.
REQUEST_STATUS_PENDING = "pending"
REQUEST_STATUS_PLANNED = "planned"
REQUEST_STATUS_IN_PROGRESS = "in_progress"
REQUEST_STATUS_COMPLETED = "completed"
REQUEST_STATUS_FAILED = "failed"
REQUEST_STATUS_CANCELLED = "cancelled"

VALID_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        REQUEST_STATUS_PENDING,
        REQUEST_STATUS_PLANNED,
        REQUEST_STATUS_IN_PROGRESS,
        REQUEST_STATUS_COMPLETED,
        REQUEST_STATUS_FAILED,
        REQUEST_STATUS_CANCELLED,
    }
)
