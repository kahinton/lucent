"""Global Lucent runtime settings and feature flags.

Safe, non-secret settings are allowlisted here and can be persisted in the
database per organization. Existing environment variables remain the fallback
source when no database value exists, which keeps container/deployment defaults
working while allowing admins to manage day-to-day settings from the UI.

This module deliberately exposes synchronous accessors because settings are
read inside hot database/query paths. Database values are loaded into an
in-process cache at startup and refreshed after settings UI changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

RuntimeSettingType = Literal["boolean", "integer", "float", "string", "json"]
RuntimeSettingSource = Literal["database", "environment", "default"]


@dataclass(frozen=True)
class RuntimeSettingDefinition:
    """Metadata and validation rules for an admin-editable setting."""

    key: str
    env_var: str
    value_type: RuntimeSettingType
    default: Any
    title: str
    section: str
    description: str
    help_text: str = ""
    min_value: int | float | None = None
    max_value: int | float | None = None
    editable: bool = True
    choices: tuple[str, ...] = ()
    sensitive: bool = False
    requires_restart: bool = False
    read_only_reason: str = ""


_RUNTIME_SETTING_DEFINITIONS: tuple[RuntimeSettingDefinition, ...] = (
    RuntimeSettingDefinition(
        key="memory.shadow_forget_enabled",
        env_var="LUCENT_SHADOW_FORGET_ENABLED",
        value_type="boolean",
        default=False,
        title="Shadow forgetting sidecar",
        section="Memory lifecycle",
        description="Record lifecycle scoring sidecar data while memory operations run.",
        help_text=(
            "Useful for rollout and diagnostics. Leave off unless you are "
            "evaluating memory lifecycle behavior."
        ),
    ),
    RuntimeSettingDefinition(
        key="memory.search_exclude_archived_enabled",
        env_var="LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED",
        value_type="boolean",
        default=False,
        title="Exclude archived memories from search",
        section="Memory search",
        description="Hide archived and forgotten memories from normal search results by default.",
        help_text="Callers can still explicitly include archived results when supported.",
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_enabled",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_ENABLED",
        value_type="boolean",
        default=False,
        title="Vitality-boosted search ranking",
        section="Memory search",
        description="Blend memory vitality into search ranking instead of using similarity alone.",
        help_text=(
            "This is a rollout flag. Enable after verifying vitality scores are "
            "healthy for the workspace."
        ),
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_alpha",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_ALPHA",
        value_type="float",
        default=0.15,
        title="Vitality boost weight",
        section="Memory search",
        description=(
            "Controls how strongly vitality nudges search ranking when vitality "
            "boost is enabled."
        ),
        min_value=0.0,
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_log_sample_rate",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_LOG_SAMPLE_RATE",
        value_type="float",
        default=0.0,
        title="Search comparison log sample rate",
        section="Observability",
        description="Fraction of searches that log old-vs-new ranking comparisons.",
        help_text=(
            "Use temporarily during rollout. 0 disables comparison logs; 1 logs "
            "every eligible search."
        ),
        min_value=0.0,
        max_value=1.0,
    ),
    RuntimeSettingDefinition(
        key="memory.search_vitality_boost_log_top_n",
        env_var="LUCENT_SEARCH_VITALITY_BOOST_LOG_TOP_N",
        value_type="integer",
        default=10,
        title="Search comparison top-N",
        section="Observability",
        description="Number of ranked results to compare in search rollout logs.",
        min_value=1,
        max_value=50,
    ),
    RuntimeSettingDefinition(
        key="requests.daemon_auto_approve",
        env_var="LUCENT_AUTO_APPROVE",
        value_type="boolean",
        default=False,
        title="Auto-approve daemon-created requests",
        section="Requests",
        description="Let daemon/cognitive requests start without waiting for human approval.",
        help_text=(
            "User, API, and scheduled requests are already auto-approved. This "
            "only affects daemon-originated work."
        ),
    ),
    RuntimeSettingDefinition(
        key="requests.skip_post_completion_review",
        env_var="LUCENT_SKIP_POST_REVIEW",
        value_type="boolean",
        default=False,
        title="Skip automatic post-completion review",
        section="Requests",
        description=(
            "Send completed requests straight to completed instead of an "
            "internal review step."
        ),
        help_text=(
            "Keep this off for safer autonomous work; enable only when review "
            "throughput is more important than quality gates."
        ),
    ),
    RuntimeSettingDefinition(
        key="requests.require_completion_approval",
        env_var="LUCENT_REQUIRE_APPROVAL",
        value_type="boolean",
        default=False,
        title="Require human approval for completed requests",
        section="Requests",
        description="Move completed requests to human review before final completion.",
        help_text="Separate from automatic post-completion review; this is a human sign-off gate.",
    ),
    RuntimeSettingDefinition(
        key="requests.review_models",
        env_var="LUCENT_REVIEW_MODELS",
        value_type="string",
        default="",
        title="Task review models",
        section="Requests",
        description="Comma-separated model IDs used for extra task-output review.",
        help_text="Leave blank to use the normal single-review flow.",
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="requests.review_model",
        env_var="LUCENT_REQUEST_REVIEW_MODEL",
        value_type="string",
        default="",
        title="Request review model",
        section="Requests",
        description="Model override for request-level post-completion review.",
        help_text="Leave blank to use the daemon/default model selector.",
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="requests.review_agent_type",
        env_var="LUCENT_REQUEST_REVIEW_AGENT_TYPE",
        value_type="string",
        default="request-review",
        title="Request review agent type",
        section="Requests",
        description="Preferred agent type for request-level post-completion review tasks.",
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="requests.review_fallback_agent_type",
        env_var="LUCENT_REQUEST_REVIEW_FALLBACK_AGENT_TYPE",
        value_type="string",
        default="code",
        title="Request review fallback agent type",
        section="Requests",
        description="Fallback agent type when the dedicated review agent is unavailable.",
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="models.default_model",
        env_var="LUCENT_DEFAULT_MODEL",
        value_type="string",
        default="",
        title="Default model",
        section="Models & LLM",
        description=(
            "Workspace-wide preferred default model when it is enabled in the "
            "model registry."
        ),
        help_text="Leave blank to let Lucent choose the first enabled general-purpose model.",
    ),
    RuntimeSettingDefinition(
        key="models.chat_model",
        env_var="LUCENT_CHAT_MODEL",
        value_type="string",
        default="",
        title="Chat model",
        section="Models & LLM",
        description="Preferred default model for chat sessions.",
        help_text="Leave blank to use the workspace default model.",
    ),
    RuntimeSettingDefinition(
        key="models.daemon_model",
        env_var="LUCENT_DAEMON_MODEL",
        value_type="string",
        default="",
        title="Daemon model",
        section="Models & LLM",
        description="Preferred default model for daemon sessions and autonomous work.",
        help_text="Leave blank to use the workspace default model.",
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="models.llm_engine",
        env_var="LUCENT_LLM_ENGINE",
        value_type="string",
        default="copilot",
        title="LLM engine",
        section="Models & LLM",
        description="Default engine for model execution when a model does not override it.",
        choices=("copilot", "langchain"),
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="models.validation_mode",
        env_var="LUCENT_MODEL_VALIDATION",
        value_type="string",
        default="strict",
        title="Model validation mode",
        section="Models & LLM",
        description="Whether unknown model IDs are rejected or allowed.",
        choices=("strict", "lenient"),
    ),
    RuntimeSettingDefinition(
        key="chat.mcp_url",
        env_var="LUCENT_CHAT_MCP_URL",
        value_type="string",
        default="http://localhost:8766/mcp",
        title="Chat MCP URL",
        section="Chat",
        description="MCP server URL used by chat sessions for tool access.",
    ),
    RuntimeSettingDefinition(
        key="chat.timeout_seconds",
        env_var="LUCENT_CHAT_TIMEOUT",
        value_type="integer",
        default=300,
        title="Chat timeout",
        section="Chat",
        description="Maximum seconds a chat model call may run.",
        min_value=30,
    ),
    RuntimeSettingDefinition(
        key="chat.session_experience_summary_enabled",
        env_var="LUCENT_SESSION_EXPERIENCE_SUMMARY_ENABLED",
        value_type="boolean",
        default=True,
        title="Session experience summaries",
        section="Chat",
        description="Capture meaningful chat sessions as experience memories.",
    ),
    RuntimeSettingDefinition(
        key="chat.session_experience_model",
        env_var="LUCENT_SESSION_EXPERIENCE_MODEL",
        value_type="string",
        default="",
        title="Session experience model",
        section="Chat",
        description="Optional model override for writing chat-session experience summaries.",
        help_text="Leave blank to use the chat/default model.",
    ),
    RuntimeSettingDefinition(
        key="chat.session_experience_timeout_seconds",
        env_var="LUCENT_SESSION_EXPERIENCE_TIMEOUT",
        value_type="integer",
        default=180,
        title="Session experience timeout",
        section="Chat",
        description="Maximum seconds for the session experience summary model call.",
        min_value=30,
    ),
    RuntimeSettingDefinition(
        key="daemon.max_sessions",
        env_var="LUCENT_MAX_SESSIONS",
        value_type="integer",
        default=3,
        title="Max daemon sessions",
        section="Daemon",
        description="Maximum concurrent sub-agent sessions per daemon instance.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.interval_minutes",
        env_var="LUCENT_DAEMON_INTERVAL",
        value_type="integer",
        default=15,
        title="Daemon cognitive interval",
        section="Daemon",
        description="Minutes between daemon cognitive cycles.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.roles",
        env_var="LUCENT_DAEMON_ROLES",
        value_type="string",
        default="all",
        title="Daemon roles",
        section="Daemon",
        description=(
            "Enabled daemon loops: all, or comma-separated "
            "cognitive/dispatcher/scheduler/autonomic."
        ),
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.mcp_url",
        env_var="LUCENT_MCP_URL",
        value_type="string",
        default="http://localhost:8766/mcp",
        title="Daemon MCP URL",
        section="Daemon",
        description="MCP server URL used by daemon-run sessions.",
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.mcp_api_key",
        env_var="LUCENT_API_KEY",
        value_type="string",
        default="",
        title="Daemon MCP API key",
        section="Daemon",
        description="Fallback API key for daemon MCP access.",
        sensitive=True,
        editable=False,
        read_only_reason="Manage daemon API keys through the daemon bootstrap/API-key flow.",
    ),
    RuntimeSettingDefinition(
        key="daemon.allow_git_commit",
        env_var="LUCENT_ALLOW_GIT_COMMIT",
        value_type="boolean",
        default=True,
        title="Allow daemon git commits",
        section="Daemon",
        description="Let daemon task sessions create local git commits when a task requires it.",
        help_text=(
            "Disable only when autonomous tasks must leave all repository changes "
            "uncommitted for an operator to handle manually."
        ),
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.allow_git_push",
        env_var="LUCENT_ALLOW_GIT_PUSH",
        value_type="boolean",
        default=True,
        title="Allow daemon git pushes",
        section="Daemon",
        description="Let daemon task sessions push committed changes to a remote repository.",
        help_text=(
            "Only applies when daemon git commits are enabled. Agents are still instructed "
            "to push only when the task explicitly requires remote persistence and the "
            "target repo/branch has been verified."
        ),
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.stale_heartbeat_minutes",
        env_var="LUCENT_STALE_HEARTBEAT_MINUTES",
        value_type="integer",
        default=30,
        title="Stale daemon heartbeat minutes",
        section="Daemon",
        description="Minutes before a daemon heartbeat is considered stale.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.session_timeout_seconds",
        env_var="LUCENT_SESSION_TIMEOUT",
        value_type="integer",
        default=3600,
        title="Daemon session timeout",
        section="Daemon",
        description="Overall timeout in seconds for a daemon model session.",
        min_value=60,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.session_idle_timeout_seconds",
        env_var="LUCENT_SESSION_IDLE_TIMEOUT",
        value_type="integer",
        default=300,
        title="Daemon session idle timeout",
        section="Daemon",
        description="Idle timeout in seconds for daemon model sessions.",
        min_value=30,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.max_result_length",
        env_var="LUCENT_MAX_RESULT_LENGTH",
        value_type="integer",
        default=8000,
        title="Max daemon result length",
        section="Daemon",
        description="Maximum characters stored from sub-agent results.",
        min_value=1000,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.watchdog_timeout_seconds",
        env_var="LUCENT_WATCHDOG_TIMEOUT",
        value_type="integer",
        default=3600,
        title="Daemon watchdog timeout seconds",
        section="Daemon",
        description="Seconds without daemon log activity before watchdog intervention.",
        min_value=60,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.dispatch_poll_seconds",
        env_var="LUCENT_DISPATCH_POLL_SECONDS",
        value_type="integer",
        default=60,
        title="Dispatch poll seconds",
        section="Daemon",
        description="How often the dispatcher polls if PostgreSQL LISTEN misses a signal.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.scheduler_check_seconds",
        env_var="LUCENT_SCHEDULER_CHECK_SECONDS",
        value_type="integer",
        default=60,
        title="Scheduler check seconds",
        section="Daemon",
        description="How often the daemon scheduler checks for due schedules.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.autonomic_interval_cycles",
        env_var="LUCENT_AUTONOMIC_INTERVAL",
        value_type="integer",
        default=8,
        title="Autonomic interval cycles",
        section="Daemon",
        description="Cognitive cycles between autonomic maintenance runs.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.learning_interval_cycles",
        env_var="LUCENT_LEARNING_INTERVAL",
        value_type="integer",
        default=16,
        title="Learning interval cycles",
        section="Daemon",
        description="Cognitive cycles between learning extraction runs.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.autonomic_minutes",
        env_var="LUCENT_AUTONOMIC_MINUTES",
        value_type="integer",
        default=120,
        title="Autonomic interval minutes",
        section="Daemon",
        description="Time-based interval for autonomic maintenance runs.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.learning_minutes",
        env_var="LUCENT_LEARNING_MINUTES",
        value_type="integer",
        default=240,
        title="Learning interval minutes",
        section="Daemon",
        description="Time-based interval for learning extraction runs.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.vitality_scoring_minutes",
        env_var="LUCENT_VITALITY_SCORING_MINUTES",
        value_type="integer",
        default=360,
        title="Vitality scoring minutes",
        section="Daemon",
        description="Interval for memory lifecycle vitality scoring.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.shadow_forget_scoring_minutes",
        env_var="LUCENT_SHADOW_FORGET_SCORING_MINUTES",
        value_type="integer",
        default=360,
        title="Shadow forget scoring minutes",
        section="Daemon",
        description="Interval for shadow-forgetting score computation.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.shadow_forget_offset_minutes",
        env_var="LUCENT_SHADOW_FORGET_OFFSET_MINUTES",
        value_type="integer",
        default=15,
        title="Shadow forget offset minutes",
        section="Daemon",
        description="Offset from vitality scoring before shadow-forgetting runs.",
        min_value=0,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="daemon.compression_minutes",
        env_var="LUCENT_COMPRESSION_MINUTES",
        value_type="integer",
        default=1440,
        title="Experience compression minutes",
        section="Daemon",
        description="Interval for daily experience memory compression.",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="server.rate_limit_per_minute",
        env_var="LUCENT_RATE_LIMIT_PER_MINUTE",
        value_type="integer",
        default=100,
        title="API rate limit",
        section="Server",
        description="Default requests per minute per API key/IP bucket.",
        min_value=1,
    ),
    RuntimeSettingDefinition(
        key="server.login_rate_limit",
        env_var="LUCENT_LOGIN_RATE_LIMIT",
        value_type="integer",
        default=5,
        title="Login rate limit",
        section="Server",
        description="Failed login attempts per IP per minute before throttling.",
        min_value=1,
    ),
    RuntimeSettingDefinition(
        key="server.trusted_proxies",
        env_var="LUCENT_TRUSTED_PROXIES",
        value_type="string",
        default="",
        title="Trusted proxies",
        section="Server",
        description="Comma-separated proxy IPs/CIDRs trusted for X-Forwarded-For parsing.",
    ),
    RuntimeSettingDefinition(
        key="server.host",
        env_var="LUCENT_HOST",
        value_type="string",
        default="0.0.0.0",
        title="Server host",
        section="Bootstrap",
        description="Network interface the server binds to at process start.",
        editable=False,
        requires_restart=True,
        read_only_reason=(
            "This is needed before the database is available; configure it in "
            "environment."
        ),
    ),
    RuntimeSettingDefinition(
        key="server.port",
        env_var="LUCENT_PORT",
        value_type="integer",
        default=8766,
        title="Server port",
        section="Bootstrap",
        description="Port the server binds to at process start.",
        editable=False,
        requires_restart=True,
        read_only_reason=(
            "This is needed before the database is available; configure it in "
            "environment."
        ),
    ),
    RuntimeSettingDefinition(
        key="server.cors_origins",
        env_var="LUCENT_CORS_ORIGINS",
        value_type="string",
        default="",
        title="CORS origins",
        section="Bootstrap",
        description="Comma-separated browser origins allowed to call the API.",
        editable=False,
        requires_restart=True,
        read_only_reason="CORS middleware is built before DB settings are loaded.",
    ),
    RuntimeSettingDefinition(
        key="auth.provider",
        env_var="LUCENT_AUTH_PROVIDER",
        value_type="string",
        default="basic",
        title="Authentication provider",
        section="Authentication",
        description="Authentication backend used for sign-in.",
        choices=("basic", "api_key"),
        editable=False,
        requires_restart=True,
        read_only_reason="Authentication bootstrap must come from environment.",
    ),
    RuntimeSettingDefinition(
        key="auth.session_ttl_hours",
        env_var="LUCENT_SESSION_TTL_HOURS",
        value_type="integer",
        default=24,
        title="Session TTL hours",
        section="Authentication",
        description="Web session cookie lifetime in hours.",
        min_value=1,
        editable=False,
        requires_restart=True,
        read_only_reason="Session constants are initialized before DB settings are loaded.",
    ),
    RuntimeSettingDefinition(
        key="auth.secure_cookies",
        env_var="LUCENT_SECURE_COOKIES",
        value_type="boolean",
        default=True,
        title="Secure cookies",
        section="Authentication",
        description="Send session cookies only over HTTPS.",
        editable=False,
        requires_restart=True,
        read_only_reason=(
            "Cookie security is a bootstrap setting and should be "
            "environment-controlled."
        ),
    ),
    RuntimeSettingDefinition(
        key="deployment.mode",
        env_var="LUCENT_MODE",
        value_type="string",
        default="personal",
        title="Deployment mode",
        section="Bootstrap",
        description="Deployment mode: personal or team.",
        choices=("personal", "team"),
        editable=False,
        requires_restart=True,
        read_only_reason="Mode controls startup behavior and must come from environment.",
    ),
    RuntimeSettingDefinition(
        key="deployment.license_key",
        env_var="LUCENT_LICENSE_KEY",
        value_type="string",
        default="",
        title="License key",
        section="Bootstrap",
        description="License key for team mode.",
        sensitive=True,
        editable=False,
        requires_restart=True,
        read_only_reason="License material is not stored in runtime_settings.",
    ),
    RuntimeSettingDefinition(
        key="secrets.provider",
        env_var="LUCENT_SECRET_PROVIDER",
        value_type="string",
        default="auto",
        title="Secret provider",
        section="Secrets & credentials",
        description="Secret storage backend selected at startup.",
        choices=("auto", "builtin", "transit", "vault", "aws", "azure"),
        editable=False,
        requires_restart=True,
        read_only_reason="Secret-provider bootstrap must happen before DB settings are trusted.",
    ),
    RuntimeSettingDefinition(
        key="secrets.signing_secret",
        env_var="LUCENT_SIGNING_SECRET",
        value_type="string",
        default="",
        title="Signing secret override",
        section="Secrets & credentials",
        description="Optional HMAC signing secret override for cookies/impersonation.",
        sensitive=True,
        editable=False,
        read_only_reason="Secrets are managed by the secret provider, not runtime_settings.",
    ),
    RuntimeSettingDefinition(
        key="secrets.builtin_key",
        env_var="LUCENT_SECRET_KEY",
        value_type="string",
        default="",
        title="Builtin secret-provider key",
        section="Secrets & credentials",
        description="Encryption key for the builtin secret provider.",
        sensitive=True,
        editable=False,
        requires_restart=True,
        read_only_reason="Never persist encryption keys in runtime_settings.",
    ),
    RuntimeSettingDefinition(
        key="credentials.github_token",
        env_var="GITHUB_TOKEN",
        value_type="string",
        default="",
        title="GitHub token",
        section="Secrets & credentials",
        description="GitHub token used for Copilot SDK and GitHub integrations.",
        sensitive=True,
        editable=False,
        read_only_reason="Use Settings → Connections or the secret provider for credentials.",
    ),
    RuntimeSettingDefinition(
        key="credentials.openai_api_key",
        env_var="OPENAI_API_KEY",
        value_type="string",
        default="",
        title="OpenAI API key",
        section="Secrets & credentials",
        description="OpenAI API key for LangChain engine usage.",
        sensitive=True,
        editable=False,
        read_only_reason="Use Settings → Secrets/Connections for provider credentials.",
    ),
    RuntimeSettingDefinition(
        key="credentials.anthropic_api_key",
        env_var="ANTHROPIC_API_KEY",
        value_type="string",
        default="",
        title="Anthropic API key",
        section="Secrets & credentials",
        description="Anthropic API key for LangChain engine usage.",
        sensitive=True,
        editable=False,
        read_only_reason="Use Settings → Secrets/Connections for provider credentials.",
    ),
    RuntimeSettingDefinition(
        key="credentials.google_api_key",
        env_var="GOOGLE_API_KEY",
        value_type="string",
        default="",
        title="Google API key",
        section="Secrets & credentials",
        description="Google/Gemini API key for LangChain engine usage.",
        sensitive=True,
        editable=False,
        read_only_reason="Use Settings → Secrets/Connections for provider credentials.",
    ),
    RuntimeSettingDefinition(
        key="database.url",
        env_var="DATABASE_URL",
        value_type="string",
        default="",
        title="Database URL",
        section="Bootstrap",
        description="PostgreSQL connection URL used before settings can be loaded.",
        sensitive=True,
        editable=False,
        requires_restart=True,
        read_only_reason="Database bootstrap cannot be read from the database it connects to.",
    ),
    RuntimeSettingDefinition(
        key="database.postgres_user",
        env_var="POSTGRES_USER",
        value_type="string",
        default="lucent",
        title="Postgres user",
        section="Bootstrap",
        description="Docker Compose Postgres user.",
        editable=False,
        requires_restart=True,
        read_only_reason="Database container settings are managed outside the app.",
    ),
    RuntimeSettingDefinition(
        key="database.postgres_db",
        env_var="POSTGRES_DB",
        value_type="string",
        default="lucent",
        title="Postgres database",
        section="Bootstrap",
        description="Docker Compose Postgres database name.",
        editable=False,
        requires_restart=True,
        read_only_reason="Database container settings are managed outside the app.",
    ),
    RuntimeSettingDefinition(
        key="database.postgres_password",
        env_var="POSTGRES_PASSWORD",
        value_type="string",
        default="",
        title="Postgres password",
        section="Secrets & credentials",
        description="Docker Compose Postgres password.",
        sensitive=True,
        editable=False,
        read_only_reason="Database passwords are never stored in runtime_settings.",
    ),
    RuntimeSettingDefinition(
        key="database.daemon_password",
        env_var="DAEMON_DB_PASSWORD",
        value_type="string",
        default="",
        title="Daemon DB password",
        section="Secrets & credentials",
        description="Password for the least-privilege daemon database role.",
        sensitive=True,
        editable=False,
        read_only_reason="Database passwords are never stored in runtime_settings.",
    ),
    # ------------------------------------------------------------------ #
    # Pattern 1 (search_memories tool_error) hardening: MCP request/timeout
    # tuning and DB pool sizing are exposed as runtime settings so operators
    # can adjust them without code changes. Read by lucent.llm.mcp_bridge
    # and lucent.db.pool / lucent.db.memory respectively.
    # ------------------------------------------------------------------ #
    RuntimeSettingDefinition(
        key="mcp.request_timeout_seconds",
        env_var="LUCENT_MCP_REQUEST_TIMEOUT_SECONDS",
        value_type="integer",
        default=120,
        title="MCP per-request read timeout",
        section="MCP",
        description=(
            "Bounded read_timeout_seconds passed to every MCP tool call. "
            "Memory read tools (search_memories, get_memory*) retry once on "
            "timeout before surfacing MCPTimeoutError."
        ),
        min_value=5,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="mcp.retry_backoff_seconds",
        env_var="LUCENT_MCP_RETRY_BACKOFF_SECONDS",
        value_type="float",
        default=0.5,
        title="MCP retry backoff",
        section="MCP",
        description="Sleep between the first and second attempt for idempotent memory read tools.",
        min_value=0.0,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="database.pool_min_size",
        env_var="LUCENT_DB_POOL_MIN_SIZE",
        value_type="integer",
        default=5,
        title="DB pool minimum size",
        section="Database",
        description="Lower bound on asyncpg pool size (default 5; was 2 prior to Pattern 1 fix).",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="database.pool_max_size",
        env_var="LUCENT_DB_POOL_MAX_SIZE",
        value_type="integer",
        default=25,
        title="DB pool maximum size",
        section="Database",
        description="Upper bound on asyncpg pool size (default 25; was 10 prior to Pattern 1 fix).",
        min_value=1,
        requires_restart=True,
    ),
    RuntimeSettingDefinition(
        key="database.search_pool_acquire_timeout_seconds",
        env_var="LUCENT_SEARCH_POOL_ACQUIRE_TIMEOUT",
        value_type="float",
        default=5.0,
        title="Search pool acquire timeout",
        section="Database",
        description=(
            "Bounded ``pool.acquire(timeout=...)`` used by MemoryDB.search/"
            "search_full. Failures raise PoolAcquireTimeoutError so callers can "
            "distinguish pool exhaustion from query-execution timeouts."
        ),
        min_value=0.5,
        requires_restart=False,
    ),
    RuntimeSettingDefinition(
        key="database.pg_trgm_similarity_threshold",
        env_var="LUCENT_PG_TRGM_SIMILARITY_THRESHOLD",
        value_type="float",
        default=0.1,
        title="pg_trgm similarity threshold",
        section="Database",
        description=(
            "set_limit() applied per asyncpg connection. Controls fuzzy "
            "search recall after the ILIKE fallback was removed."
        ),
        min_value=0.0,
        requires_restart=True,
    ),
)

_SETTING_DEFINITIONS_BY_KEY = {d.key: d for d in _RUNTIME_SETTING_DEFINITIONS}
_runtime_settings_by_org: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def runtime_setting_definitions() -> list[RuntimeSettingDefinition]:
    """Return all safe runtime settings exposed to admins."""
    return list(_RUNTIME_SETTING_DEFINITIONS)


def get_runtime_setting_definition(key: str) -> RuntimeSettingDefinition | None:
    """Return the allowlist definition for ``key`` if it exists."""
    return _SETTING_DEFINITIONS_BY_KEY.get(key)


def _normalize_org_id(organization_id: Any | None) -> str | None:
    if organization_id is None:
        return None
    value = str(organization_id).strip()
    return value or None


def _current_organization_id() -> str | None:
    try:
        from lucent.auth import get_current_user

        user = get_current_user()
    except Exception:
        return None
    if not user:
        return None
    return _normalize_org_id(user.get("organization_id"))


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Enter true or false.")


def _coerce_runtime_value(
    definition: RuntimeSettingDefinition,
    raw: Any,
    *,
    clamp_bounds: bool = False,
) -> Any:
    if definition.value_type == "boolean":
        return _parse_bool(raw)
    if definition.value_type == "integer":
        if isinstance(raw, bool):
            raise ValueError("Enter a whole number.")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a whole number.") from exc
    elif definition.value_type == "float":
        if isinstance(raw, bool):
            raise ValueError("Enter a number.")
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a number.") from exc
    elif definition.value_type == "string":
        value = str(raw)
    elif definition.value_type == "json":
        value = raw
    else:
        raise ValueError("Unsupported setting type.")

    if definition.value_type in {"integer", "float"}:
        if definition.min_value is not None and value < definition.min_value:
            if clamp_bounds:
                value = definition.min_value
            else:
                raise ValueError(f"Value must be at least {definition.min_value}.")
        if definition.max_value is not None and value > definition.max_value:
            if clamp_bounds:
                value = definition.max_value
            else:
                raise ValueError(f"Value must be at most {definition.max_value}.")

    if definition.choices and str(value) not in definition.choices:
        choices = ", ".join(definition.choices)
        if clamp_bounds:
            return definition.default
        raise ValueError(f"Value must be one of: {choices}.")

    return value


def validate_runtime_setting_value(key: str, raw: Any) -> Any:
    """Validate and coerce a user-provided runtime setting value."""
    definition = get_runtime_setting_definition(key)
    if not definition or not definition.editable:
        raise ValueError("Unknown or read-only setting.")
    return _coerce_runtime_value(definition, raw, clamp_bounds=False)


def _fallback_value(definition: RuntimeSettingDefinition) -> tuple[Any, RuntimeSettingSource]:
    raw = os.environ.get(definition.env_var)
    if raw is None:
        return definition.default, "default"
    try:
        return _coerce_runtime_value(definition, raw, clamp_bounds=True), "environment"
    except ValueError:
        return definition.default, "default"


def _org_cache(organization_id: Any | None = None) -> tuple[str | None, dict[str, Any]]:
    org_id = _normalize_org_id(organization_id) or _current_organization_id()
    if not org_id and len(_runtime_settings_by_org) == 1:
        org_id = next(iter(_runtime_settings_by_org))
    if not org_id:
        return None, {}
    return org_id, _runtime_settings_by_org.get(org_id, {})


def get_runtime_setting(
    key: str,
    *,
    organization_id: Any | None = None,
) -> Any:
    """Return a setting value using DB → env → default precedence."""
    definition = get_runtime_setting_definition(key)
    if not definition:
        raise KeyError(f"Unknown runtime setting: {key}")
    _org_id, cached = _org_cache(organization_id)
    if key in cached:
        try:
            return _coerce_runtime_value(definition, cached[key], clamp_bounds=True)
        except ValueError:
            pass
    value, _source = _fallback_value(definition)
    return value


def get_runtime_setting_source(
    key: str,
    *,
    organization_id: Any | None = None,
) -> RuntimeSettingSource:
    """Return which source currently supplies ``key``."""
    definition = get_runtime_setting_definition(key)
    if not definition:
        raise KeyError(f"Unknown runtime setting: {key}")
    _org_id, cached = _org_cache(organization_id)
    if key in cached:
        return "database"
    _value, source = _fallback_value(definition)
    return source


def set_runtime_setting_cache(
    organization_id: Any,
    key: str,
    value: Any,
) -> None:
    """Update the in-process DB settings cache for one setting."""
    definition = get_runtime_setting_definition(key)
    if not definition:
        return
    org_id = _normalize_org_id(organization_id)
    if not org_id:
        return
    _runtime_settings_by_org.setdefault(org_id, {})[key] = _coerce_runtime_value(
        definition,
        value,
        clamp_bounds=True,
    )


def clear_runtime_setting_cache(organization_id: Any | None = None, key: str | None = None) -> None:
    """Clear cached DB setting values.

    With no arguments the entire cache is cleared. Passing only an org clears
    that org. Passing both org and key clears one setting so env/default
    fallback is used immediately in this process.
    """
    org_id = _normalize_org_id(organization_id)
    if not org_id:
        _runtime_settings_by_org.clear()
        return
    if key is None:
        _runtime_settings_by_org.pop(org_id, None)
        return
    values = _runtime_settings_by_org.get(org_id)
    if values is not None:
        values.pop(key, None)
        if not values:
            _runtime_settings_by_org.pop(org_id, None)


async def load_runtime_settings_from_db(
    pool: Any,
    organization_id: Any | None = None,
) -> None:
    """Load DB-backed settings into the in-process cache.

    Startup calls this after migrations. Web settings updates adjust the cache
    directly so the current process sees changes immediately.
    """
    from lucent.db.runtime_settings import RuntimeSettingsRepository

    repo = RuntimeSettingsRepository(pool)
    rows = await repo.list_settings(organization_id=organization_id)

    if organization_id is not None:
        clear_runtime_setting_cache(organization_id)
    else:
        clear_runtime_setting_cache()

    for row in rows:
        key = row.get("key")
        if key not in _SETTING_DEFINITIONS_BY_KEY:
            continue
        set_runtime_setting_cache(row.get("organization_id"), key, row.get("value"))


def runtime_setting_snapshots(organization_id: Any) -> list[dict[str, Any]]:
    """Return UI-friendly setting snapshots for one organization."""
    snapshots: list[dict[str, Any]] = []
    org_id, cached = _org_cache(organization_id)
    for definition in _RUNTIME_SETTING_DEFINITIONS:
        value = get_runtime_setting(definition.key, organization_id=org_id)
        source = get_runtime_setting_source(definition.key, organization_id=org_id)
        env_raw = os.environ.get(definition.env_var)
        snapshots.append(
            {
                "definition": definition,
                "value": value,
                "display_value": _display_value(definition, value),
                "form_value": _format_form_value(value),
                "source": source,
                "db_value": cached.get(definition.key),
                "env_var_set": env_raw is not None,
                "env_raw": "***" if definition.sensitive and env_raw else env_raw,
                "default_value": definition.default,
                "default_display": _display_value(definition, definition.default),
            }
        )
    return snapshots


def runtime_settings_by_section(organization_id: Any) -> dict[str, list[dict[str, Any]]]:
    """Return setting snapshots grouped by display section."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for snapshot in runtime_setting_snapshots(organization_id):
        section = snapshot["definition"].section
        grouped.setdefault(section, []).append(snapshot)
    return grouped


def _format_form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _display_value(definition: RuntimeSettingDefinition, value: Any) -> str:
    if definition.sensitive:
        return "Configured (hidden)" if value else "Not configured"
    if value == "":
        return "Not set"
    return str(value)


def _setting_string(key: str, *, organization_id: Any | None = None) -> str:
    return str(get_runtime_setting(key, organization_id=organization_id)).strip()


def _setting_int(key: str, *, organization_id: Any | None = None) -> int:
    return int(get_runtime_setting(key, organization_id=organization_id))


def _setting_bool(key: str, *, organization_id: Any | None = None) -> bool:
    return bool(get_runtime_setting(key, organization_id=organization_id))


def default_model_id(*, organization_id: Any | None = None) -> str | None:
    """Deployment/admin-selected default model, or None when unset."""
    return _setting_string("models.default_model", organization_id=organization_id) or None


def chat_model_id(*, organization_id: Any | None = None) -> str | None:
    """Preferred chat model, or None when chat should use the default model."""
    return _setting_string("models.chat_model", organization_id=organization_id) or None


def daemon_model_id(*, organization_id: Any | None = None) -> str | None:
    """Preferred daemon model, or None when daemon should use the default model."""
    return _setting_string("models.daemon_model", organization_id=organization_id) or None


def llm_engine_name(*, organization_id: Any | None = None) -> str:
    """Configured default LLM engine name."""
    return _setting_string("models.llm_engine", organization_id=organization_id).lower()


def model_validation_mode(*, organization_id: Any | None = None) -> str:
    """Model validation mode: strict or lenient."""
    return _setting_string("models.validation_mode", organization_id=organization_id).lower()


def chat_mcp_url(*, organization_id: Any | None = None) -> str:
    """MCP URL used by chat sessions."""
    return _setting_string("chat.mcp_url", organization_id=organization_id)


def chat_timeout_seconds(*, organization_id: Any | None = None) -> int:
    """Maximum runtime for chat model sessions."""
    return _setting_int("chat.timeout_seconds", organization_id=organization_id)


def session_experience_summary_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether meaningful chat sessions should be captured as experience memories."""
    return _setting_bool(
        "chat.session_experience_summary_enabled",
        organization_id=organization_id,
    )


def session_experience_model_id(*, organization_id: Any | None = None) -> str | None:
    """Model override for chat-session experience summaries, or None."""
    return _setting_string("chat.session_experience_model", organization_id=organization_id) or None


def session_experience_timeout_seconds(*, organization_id: Any | None = None) -> int:
    """Maximum runtime for session-experience summary model calls."""
    return _setting_int(
        "chat.session_experience_timeout_seconds",
        organization_id=organization_id,
    )


def daemon_max_sessions(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.max_sessions", organization_id=organization_id)


def daemon_interval_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.interval_minutes", organization_id=organization_id)


def daemon_roles(*, organization_id: Any | None = None) -> str:
    return _setting_string("daemon.roles", organization_id=organization_id)


def daemon_mcp_url(*, organization_id: Any | None = None) -> str:
    return _setting_string("daemon.mcp_url", organization_id=organization_id)


def daemon_mcp_api_key(*, organization_id: Any | None = None) -> str:
    return _setting_string("daemon.mcp_api_key", organization_id=organization_id)


def daemon_stale_heartbeat_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.stale_heartbeat_minutes", organization_id=organization_id)


def daemon_session_timeout_seconds(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.session_timeout_seconds", organization_id=organization_id)


def daemon_session_idle_timeout_seconds(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.session_idle_timeout_seconds", organization_id=organization_id)


def daemon_max_result_length(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.max_result_length", organization_id=organization_id)


def daemon_watchdog_timeout_seconds(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.watchdog_timeout_seconds", organization_id=organization_id)


def daemon_dispatch_poll_seconds(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.dispatch_poll_seconds", organization_id=organization_id)


def daemon_scheduler_check_seconds(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.scheduler_check_seconds", organization_id=organization_id)


def daemon_autonomic_interval_cycles(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.autonomic_interval_cycles", organization_id=organization_id)


def daemon_learning_interval_cycles(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.learning_interval_cycles", organization_id=organization_id)


def daemon_autonomic_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.autonomic_minutes", organization_id=organization_id)


def daemon_learning_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.learning_minutes", organization_id=organization_id)


def daemon_vitality_scoring_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.vitality_scoring_minutes", organization_id=organization_id)


def daemon_shadow_forget_scoring_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.shadow_forget_scoring_minutes", organization_id=organization_id)


def daemon_shadow_forget_offset_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.shadow_forget_offset_minutes", organization_id=organization_id)


def daemon_compression_minutes(*, organization_id: Any | None = None) -> int:
    return _setting_int("daemon.compression_minutes", organization_id=organization_id)


def daemon_git_commit_allowed(*, organization_id: Any | None = None) -> bool:
    return _setting_bool("daemon.allow_git_commit", organization_id=organization_id)


def daemon_git_push_allowed(*, organization_id: Any | None = None) -> bool:
    return _setting_bool("daemon.allow_git_push", organization_id=organization_id)


def api_rate_limit_per_minute(*, organization_id: Any | None = None) -> int:
    return _setting_int("server.rate_limit_per_minute", organization_id=organization_id)


def login_rate_limit(*, organization_id: Any | None = None) -> int:
    return _setting_int("server.login_rate_limit", organization_id=organization_id)


def trusted_proxies(*, organization_id: Any | None = None) -> str:
    return _setting_string("server.trusted_proxies", organization_id=organization_id)


def github_token(*, organization_id: Any | None = None) -> str:
    return _setting_string("credentials.github_token", organization_id=organization_id)


def completion_human_approval_required(*, organization_id: Any | None = None) -> bool:
    return _setting_bool("requests.require_completion_approval", organization_id=organization_id)


def review_model_ids(*, organization_id: Any | None = None) -> list[str]:
    raw = _setting_string("requests.review_models", organization_id=organization_id)
    return [item.strip() for item in raw.split(",") if item.strip()]


def request_review_model_id(*, organization_id: Any | None = None) -> str | None:
    return _setting_string("requests.review_model", organization_id=organization_id) or None


def request_review_agent_type(*, organization_id: Any | None = None) -> str:
    return _setting_string("requests.review_agent_type", organization_id=organization_id)


def request_review_fallback_agent_type(*, organization_id: Any | None = None) -> str:
    return _setting_string("requests.review_fallback_agent_type", organization_id=organization_id)


def shadow_forget_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether shadow forgetting sidecar reads/writes are enabled."""
    return bool(
        get_runtime_setting(
            "memory.shadow_forget_enabled",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether search ranking includes the vitality boost term."""
    return bool(
        get_runtime_setting(
            "memory.search_vitality_boost_enabled",
            organization_id=organization_id,
        )
    )


def search_exclude_archived_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether search excludes ``archived``/``forgotten`` lifecycle stages by default.

    Phase-2 M9 rollout flag, mirroring ``LUCENT_SEARCH_VITALITY_BOOST_ENABLED``.

    - Off (default): the search SQL is byte-identical to the pre-M9 baseline —
      ``include_archived`` is accepted on the API surface but has no effect on
      the emitted query, so production behavior is unchanged until an operator
      opts in.
    - On: queries with ``include_archived=False`` (the default) get a
      ``lifecycle_stage NOT IN ('archived', 'forgotten')`` WHERE-clause
      addition. ``include_archived=True`` callers continue to see all rows.
    """
    return bool(
        get_runtime_setting(
            "memory.search_exclude_archived_enabled",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_alpha(*, organization_id: Any | None = None) -> float:
    """Weight for vitality contribution in ranked memory search."""
    return float(
        get_runtime_setting(
            "memory.search_vitality_boost_alpha",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_log_sample_rate(*, organization_id: Any | None = None) -> float:
    """Sampling rate (0.0–1.0) for emitting legacy-vs-boosted top-N comparison logs.

    Phase-2 observability hook: when the vitality boost flag is enabled, a
    fraction of search calls additionally run the legacy ranking and emit a
    structured JSON log line comparing the top-N results. Defaults to ``0.0``
    (disabled) so production is unaffected unless an operator opts in.
    """
    return float(
        get_runtime_setting(
            "memory.search_vitality_boost_log_sample_rate",
            organization_id=organization_id,
        )
    )


def search_vitality_boost_log_top_n(*, organization_id: Any | None = None) -> int:
    """How many ranked results to compare in the boost-vs-legacy log line."""
    return int(
        get_runtime_setting(
            "memory.search_vitality_boost_log_top_n",
            organization_id=organization_id,
        )
    )


def daemon_auto_approve_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether daemon/cognitive requests bypass human approval."""
    return bool(
        get_runtime_setting(
            "requests.daemon_auto_approve",
            organization_id=organization_id,
        )
    )


def post_completion_review_enabled(*, organization_id: Any | None = None) -> bool:
    """Whether completed requests go through the automatic review step."""
    skip_review = bool(
        get_runtime_setting(
            "requests.skip_post_completion_review",
            organization_id=organization_id,
        )
    )
    return not skip_review
