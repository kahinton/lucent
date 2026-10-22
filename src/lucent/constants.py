"""Shared constants for the Lucent platform.

Canonical value sets used across API models, DB repositories, and MCP tools.
"""

# Named constants for each valid request source.
REQUEST_SOURCE_USER = "user"
REQUEST_SOURCE_COGNITIVE = "cognitive"
REQUEST_SOURCE_API = "api"
REQUEST_SOURCE_DAEMON = "daemon"
REQUEST_SOURCE_SCHEDULE = "schedule"

# Valid source values for requests.
VALID_REQUEST_SOURCES: frozenset[str] = frozenset(
    {
        REQUEST_SOURCE_USER,
        REQUEST_SOURCE_COGNITIVE,
        REQUEST_SOURCE_API,
        REQUEST_SOURCE_DAEMON,
        REQUEST_SOURCE_SCHEDULE,
    }
)

# Regex pattern matching any valid request source (for Pydantic Field patterns).
REQUEST_SOURCE_PATTERN: str = "^(" + "|".join(sorted(VALID_REQUEST_SOURCES)) + ")$"

# Canonical status values for requests.
REQUEST_STATUS_PENDING = "pending"
REQUEST_STATUS_PLANNED = "planned"
REQUEST_STATUS_IN_PROGRESS = "in_progress"
REQUEST_STATUS_REVIEW = "review"
REQUEST_STATUS_NEEDS_REWORK = "needs_rework"
REQUEST_STATUS_COMPLETED = "completed"
REQUEST_STATUS_FAILED = "failed"
REQUEST_STATUS_CANCELLED = "cancelled"
REQUEST_STATUS_REJECTION_PROCESSING = "rejection_processing"

VALID_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        REQUEST_STATUS_PENDING,
        REQUEST_STATUS_PLANNED,
        REQUEST_STATUS_IN_PROGRESS,
        REQUEST_STATUS_REVIEW,
        REQUEST_STATUS_NEEDS_REWORK,
        REQUEST_STATUS_COMPLETED,
        REQUEST_STATUS_FAILED,
        REQUEST_STATUS_CANCELLED,
        REQUEST_STATUS_REJECTION_PROCESSING,
    }
)

# PostgreSQL advisory-lock namespace for request decomposition backfill.
DECOMPOSITION_LOCK_NAMESPACE = 0x4C434D50

# Advisory-lock namespace that serializes a background report turn against a
# user chat turn for the same LLM session, across replicas.
SUBAGENT_REPORT_LOCK_NAMESPACE = 0x4C535242  # "LSRB"

# Who owns decomposition of a request (migration 143).
#
# 'planner' — the daemon's decomposition composer may break this request into
#             tasks while it has none. This is the default for requests the
#             daemon itself or an automation created.
# 'author'  — a caller (chat) already authored the breakdown. The composer must
#             never add tasks, and authorship is sticky so later single-task
#             additions cannot hand the request back to the composer.
DECOMPOSITION_MODE_PLANNER = "planner"
DECOMPOSITION_MODE_AUTHOR = "author"

VALID_DECOMPOSITION_MODES: frozenset[str] = frozenset(
    {DECOMPOSITION_MODE_PLANNER, DECOMPOSITION_MODE_AUTHOR}
)

# Task statuses that mean the work is no longer running or queued.
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)

# Task statuses that a hard cancel must actively interrupt.
INTERRUPTIBLE_TASK_STATUSES: frozenset[str] = frozenset({"claimed", "running"})
