"""Repository for scheduled tasks.

Supports one-time, interval, and cron schedules.
Each run creates a tracked request for full lineage.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from asyncpg import Pool

_REPO_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPO_IN_TEXT_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
_FILE_HEADING_RE = re.compile(r"(?im)^\s*#+\s*(?:File|Filename):\s*`?([^`\n]+?)`?\s*$")
_DIRECTORY_HEADING_RE = re.compile(
    r"(?im)^\s*#+\s*(?:Directory|Module):\s*`?([^`\n]+?)`?\s*$"
)
_REPOSITORY_HEADING_RE = re.compile(
    r"(?im)^\s*#+\s*(?:Repository:\s*)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+[^\n]*(?:Repository|repo)\s+(?:Overview|architecture)"
)
_PATH_IN_TEXT_RE = re.compile(
    r"`((?:\.github|daemon|deploy|docker|docs|examples|scripts|src|tests|migrations|app)/[^`\s,)]+)`"
)
_NON_CATEGORY_TAGS = {
    "codebase",
    "codebase-knowledge",
    "daemon",
    "lesson-extracted",
    "needs-review",
    "technical",
    "validated",
}


def _clean_path(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip("`'\" ")
    cleaned = cleaned.removeprefix("./")
    return cleaned or None


def _looks_like_file_path(path: str | None) -> bool:
    if not path:
        return False
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return "." in name and not path.endswith("/")


def _parent_directory(filename: str) -> str | None:
    if "/" not in filename:
        return None
    return filename.rsplit("/", 1)[0] + "/"


def _normalize_directory(path: str | None) -> str | None:
    cleaned = _clean_path(path)
    if not cleaned:
        return None
    if _looks_like_file_path(cleaned):
        return _parent_directory(cleaned)
    return cleaned.rstrip("/") + "/"


def _category_from(tags: list[str], metadata: dict[str, Any], directory: str | None) -> str:
    existing = str(metadata.get("category") or "").strip()
    if existing:
        return existing
    for tag in tags:
        normalized = tag.strip().lower()
        if (
            normalized
            and normalized not in _NON_CATEGORY_TAGS
            and not _REPO_TAG_RE.match(normalized)
        ):
            return normalized[:64]
    if directory:
        top = directory.split("/", 1)[0]
        return {
            ".github": "workflow",
            "daemon": "daemon",
            "docs": "documentation",
            "tests": "testing",
            "src": "architecture",
        }.get(top, top or "architecture")
    return "architecture"


def _derive_codebase_metadata(row: dict[str, Any]) -> dict[str, Any] | None:
    """Derive repo/directory/filename/category for a technical memory.

    Returns ``None`` when there is not enough evidence that this is codebase
    knowledge. That keeps general technical lessons out of repo hierarchy
    normalization instead of forcing guessed metadata onto them.
    """
    raw_metadata = row.get("metadata") or {}
    if isinstance(raw_metadata, str):
        try:
            raw_metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            raw_metadata = {}
    metadata = dict(raw_metadata or {})
    tags = [str(t) for t in (row.get("tags") or [])]
    content = str(row.get("content") or "")

    repo = str(metadata.get("repo") or "").strip() or None
    if not repo:
        repo = next((tag for tag in tags if _REPO_TAG_RE.match(tag)), None)
    if not repo:
        match = _REPO_IN_TEXT_RE.search(content[:1000])
        repo = match.group(1) if match else None
    if not repo:
        return None

    filename = _clean_path(str(metadata.get("filename") or "") or None)
    directory = _normalize_directory(str(metadata.get("directory") or "") or None)
    file_heading = None
    file_match = _FILE_HEADING_RE.search(content[:1000])
    if file_match:
        file_heading = _clean_path(file_match.group(1))
    directory_heading = None
    directory_match = _DIRECTORY_HEADING_RE.search(content[:1000])
    if directory_match:
        directory_heading = _normalize_directory(directory_match.group(1))
    repo_heading = bool(_REPOSITORY_HEADING_RE.search(content[:1000]))

    if file_heading:
        filename = file_heading
        directory = _parent_directory(filename)
    elif directory_heading:
        filename = None
        directory = directory_heading
    elif repo_heading:
        filename = None
        directory = None

    if not filename and not directory and not repo_heading:
        match = _PATH_IN_TEXT_RE.search(content[:1200])
        if match and _looks_like_file_path(match.group(1)):
            filename = _clean_path(match.group(1))
    if filename:
        directory = _parent_directory(filename)
    elif not directory and not repo_heading:
        if directory_heading:
            directory = directory_heading
        else:
            match = _PATH_IN_TEXT_RE.search(content[:1200])
            if match:
                directory = _normalize_directory(match.group(1))

    normalized = dict(metadata)
    normalized["repo"] = repo
    normalized["directory"] = directory
    normalized["filename"] = filename
    normalized["category"] = _category_from(tags, metadata, directory)
    return normalized

ALLOWED_SCHEDULE_COLUMNS = frozenset(
    {
        "title",
        "description",
        "enabled",
        "agent_type",
        "model",
        "reasoning_effort",
        "prompt",
        "task_template",
        "sandbox_config",
        "sandbox_template_id",
        "schedule_type",
        "cron_expression",
        "interval_seconds",
        "next_run_at",
        "priority",
        "timezone",
        "max_runs",
        "expires_at",
    }
)


SYSTEM_SCHEDULE_PROTECTION_MSG = (
    "Built-in system schedules cannot be modified by the daemon. "
    "Update the on-disk source file instead."
)


def _parse_cron(expression: str, after: datetime) -> datetime:
    """Calculate next run time from a cron expression (minute hour dom month dow).

    Supports: *, specific values, ranges (1-5), steps (*/15), and lists (1,3,5).
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {expression}")

    def _expand(field: str, min_val: int, max_val: int) -> set[int]:
        values = set()
        for item in field.split(","):
            if item == "*":
                return set(range(min_val, max_val + 1))
            if "/" in item:
                base, step = item.split("/", 1)
                step = int(step)
                start = min_val if base == "*" else int(base)
                values.update(range(start, max_val + 1, step))
            elif "-" in item:
                lo, hi = item.split("-", 1)
                values.update(range(int(lo), int(hi) + 1))
            else:
                values.add(int(item))
        return values

    minutes = _expand(parts[0], 0, 59)
    hours = _expand(parts[1], 0, 23)
    doms = _expand(parts[2], 1, 31)
    months = _expand(parts[3], 1, 12)
    # Cron DOW: 0=Sunday, 6=Saturday.  Python weekday(): 0=Monday, 6=Sunday.
    # Convert cron → Python so the comparison on line 55 is correct.
    cron_dows = _expand(parts[4], 0, 6)
    dows = {(d - 1) % 7 for d in cron_dows}

    # Standard cron: when both DOM and DOW are explicitly set (not *),
    # fire on DOM OR DOW. When either is *, AND is equivalent.
    dom_restricted = parts[2] != "*"
    dow_restricted = parts[4] != "*"
    use_or = dom_restricted and dow_restricted

    # Walk forward minute by minute from after, capped at 366 days
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366)

    while candidate < limit:
        day_match = (
            (candidate.day in doms or candidate.weekday() in dows)
            if use_or
            else (candidate.day in doms and candidate.weekday() in dows)
        )
        if (
            candidate.month in months
            and day_match
            and candidate.hour in hours
            and candidate.minute in minutes
        ):
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(f"No next run found within 366 days for: {expression}")


# Common timezone aliases that may not be in all tzdata packages
_TZ_ALIASES = {
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "US/Alaska": "America/Anchorage",
    "US/Hawaii": "Pacific/Honolulu",
}


def _next_cron_utc(expression: str, after_utc: datetime, tz_name: str = "UTC") -> datetime:
    """Calculate next cron run in the schedule's timezone, return as UTC."""
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, Exception):
        # Fall back to IANA name if a legacy alias was used
        canonical = _TZ_ALIASES.get(tz_name, "UTC")
        tz = ZoneInfo(canonical)
    after_local = after_utc.astimezone(tz)
    next_local = _parse_cron(expression, after_local)
    return next_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc)


class ScheduleRepository:
    """Manages scheduled tasks and their run history."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def _check_system_schedule_protection(
        self,
        schedule_id: str,
        org_id: str,
        requester_role: str | None,
    ) -> None:
        """Raise ValueError if a daemon tries to modify a system schedule.

        System (built-in) schedules have their source of truth on disk or in the
        seeding logic.  The daemon should not modify them — changes would be
        overwritten on restart.
        """
        if requester_role != "daemon":
            return
        async with self.pool.acquire() as conn:
            is_sys = await conn.fetchval(
                "SELECT is_system FROM schedules "
                "WHERE id = $1::uuid AND organization_id = $2::uuid",
                schedule_id, org_id,
            )
        if is_sys:
            raise ValueError(SYSTEM_SCHEDULE_PROTECTION_MSG)

    # ── System schedules ──────────────────────────────────────────────────

    async def ensure_system_schedule(
        self,
        title: str,
        org_id: str,
        description: str,
        agent_type: str,
        schedule_type: str,
        interval_seconds: int | None = None,
        cron_expression: str | None = None,
        priority: str = "low",
        prompt: str = "",
        created_by: str | None = None,
    ) -> dict:
        """Create a system schedule if it doesn't exist, or refresh and return it.

        System schedules are identified by title + org_id + is_system=true.
        Their source-controlled definition fields are refreshed by startup
        seeding; runtime state such as enabled/next_run_at is preserved.
        """
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow(
                """SELECT * FROM schedules
                   WHERE title = $1 AND organization_id = $2::uuid AND is_system = true""",
                title,
                org_id,
            )
            if existing:
                row = await conn.fetchrow(
                    """UPDATE schedules SET
                           description = $3,
                           agent_type = $4,
                           schedule_type = $5,
                           interval_seconds = $6,
                           cron_expression = $7,
                           priority = $8,
                           prompt = $9,
                           updated_at = NOW()
                       WHERE id = $1::uuid AND organization_id = $2::uuid
                         AND is_system = true
                       RETURNING *""",
                    str(existing["id"]),
                    org_id,
                    description,
                    agent_type,
                    schedule_type,
                    interval_seconds,
                    cron_expression,
                    priority,
                    prompt,
                )
                return dict(row)

            now = datetime.now(timezone.utc)
            if schedule_type == "interval" and interval_seconds:
                next_run_at = now + timedelta(seconds=interval_seconds)
            elif schedule_type == "cron" and cron_expression:
                next_run_at = _next_cron_utc(cron_expression, now, "UTC")
            else:
                next_run_at = now

            row = await conn.fetchrow(
                """INSERT INTO schedules
                   (title, organization_id, description, agent_type, schedule_type,
                    interval_seconds, cron_expression, next_run_at, priority, prompt,
                    created_by, is_system)
                   VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::uuid, true)
                   RETURNING *""",
                title,
                org_id,
                description,
                agent_type,
                schedule_type,
                interval_seconds,
                cron_expression,
                next_run_at,
                priority,
                prompt,
                created_by,
            )
            return dict(row)

    # ── Schedules ─────────────────────────────────────────────────────────

    async def create_schedule(
        self,
        title: str,
        org_id: str,
        schedule_type: str = "once",
        description: str = "",
        agent_type: str = "code",
        model: str | None = None,
        reasoning_effort: str | None = None,
        task_template: dict | None = None,
        sandbox_config: dict | None = None,
        sandbox_template_id: str | None = None,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        next_run_at: datetime | None = None,
        priority: str = "medium",
        timezone_str: str = "UTC",
        max_runs: int | None = None,
        expires_at: datetime | None = None,
        created_by: str | None = None,
        prompt: str = "",
    ) -> dict:
        # Calculate next_run_at if not provided
        if next_run_at is None:
            now = datetime.now(timezone.utc)
            if schedule_type == "once":
                next_run_at = now
            elif schedule_type == "interval" and interval_seconds:
                next_run_at = now + timedelta(seconds=interval_seconds)
            elif schedule_type == "cron" and cron_expression:
                next_run_at = _next_cron_utc(cron_expression, now, timezone_str)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO schedules
                   (title, organization_id, description, agent_type, model, task_template,
                    reasoning_effort, sandbox_config, sandbox_template_id, schedule_type,
                    cron_expression, interval_seconds, next_run_at, priority, timezone,
                    max_runs, expires_at, created_by, prompt)
                   VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb, $7, $8::jsonb,
                       $9::uuid, $10, $11, $12, $13, $14, $15, $16,
                       $17, $18::uuid, $19)
                   RETURNING *""",
                title,
                org_id,
                description,
                agent_type,
                model,
                json.dumps(task_template or {}),
                reasoning_effort,
                json.dumps(sandbox_config) if isinstance(sandbox_config, dict)
                else sandbox_config if isinstance(sandbox_config, str) and sandbox_config
                else None,
                sandbox_template_id,
                schedule_type,
                cron_expression,
                interval_seconds,
                next_run_at,
                priority,
                timezone_str,
                max_runs,
                expires_at,
                created_by,
                prompt,
            )
            return dict(row)

    async def get_schedule(self, schedule_id: str, org_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM schedules WHERE id = $1::uuid AND organization_id = $2::uuid",
                schedule_id,
                org_id,
            )
            return dict(row) if row else None

    async def list_schedules(
        self,
        org_id: str,
        status: str | None = None,
        enabled: bool | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        async with self.pool.acquire() as conn:
            conditions = ["organization_id = $1::uuid"]
            params: list[Any] = [org_id]
            idx = 2

            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if enabled is not None:
                conditions.append(f"enabled = ${idx}")
                params.append(enabled)
                idx += 1

            where = " AND ".join(conditions)
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total FROM schedules WHERE {where}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0

            params.extend([limit, offset])
            rows = await conn.fetch(
                f"""SELECT * FROM schedules
                    WHERE {where}
                    ORDER BY
                        CASE WHEN enabled AND status = 'active' THEN 0 ELSE 1 END,
                        next_run_at ASC NULLS LAST
                    LIMIT ${idx} OFFSET ${idx + 1}""",
                *params,
            )
            return {
                "items": [dict(r) for r in rows],
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(rows) < total_count,
            }

    async def update_schedule(
        self, schedule_id: str, org_id: str, *, requester_role: str | None = None, **fields,
    ) -> dict | None:
        await self._check_system_schedule_protection(schedule_id, org_id, requester_role)
        if not fields:
            return await self.get_schedule(schedule_id, org_id)

        sets = []
        params: list[Any] = []
        idx = 1
        for key, val in fields.items():
            if key not in ALLOWED_SCHEDULE_COLUMNS:
                raise ValueError(f"Invalid schedule update column: {key}")
            if key == "task_template":
                sets.append(f"task_template = ${idx}::jsonb")
                params.append(json.dumps(val))
            elif key == "sandbox_template_id":
                sets.append(f"sandbox_template_id = ${idx}::uuid")
                params.append(val)
            else:
                sets.append(f"{key} = ${idx}")
                params.append(val)
            idx += 1

        sets.append(f"updated_at = ${idx}")
        params.append(datetime.now(timezone.utc))
        idx += 1

        params.extend([schedule_id, org_id])
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""UPDATE schedules SET {", ".join(sets)}
                    WHERE id = ${idx}::uuid AND organization_id = ${idx + 1}::uuid
                    RETURNING *""",
                *params,
            )
            return dict(row) if row else None

    async def toggle_schedule(
        self, schedule_id: str, org_id: str, enabled: bool,
        *, requester_role: str | None = None,
    ) -> dict | None:
        return await self.update_schedule(
            schedule_id, org_id, requester_role=requester_role, enabled=enabled,
        )

    async def delete_schedule(self, schedule_id: str, org_id: str) -> bool:
        async with self.pool.acquire() as conn:
            # System schedules cannot be deleted — only modified or disabled
            is_sys = await conn.fetchval(
                (
                    "SELECT is_system FROM schedules "
                    "WHERE id = $1::uuid AND organization_id = $2::uuid"
                ),
                schedule_id,
                org_id,
            )
            if is_sys:
                raise ValueError("System schedules cannot be deleted. Disable it instead.")
            result = await conn.execute(
                (
                    "DELETE FROM schedules "
                    "WHERE id = $1::uuid AND organization_id = $2::uuid "
                    "AND (is_system IS NOT TRUE)"
                ),
                schedule_id,
                org_id,
            )
            return result == "DELETE 1"

    # ── Due schedules (called by daemon) ──────────────────────────────────

    async def get_due_schedules(self, org_id: str | None = None) -> list[dict]:
        """Return schedules whose next_run_at is in the past and are active."""
        async with self.pool.acquire() as conn:
            conditions = [
                "enabled = true",
                "status = 'active'",
                "next_run_at <= now()",
            ]
            params: list[Any] = []
            idx = 1

            if org_id:
                conditions.append(f"organization_id = ${idx}::uuid")
                params.append(org_id)
                idx += 1

            rows = await conn.fetch(
                f"""SELECT * FROM schedules
                    WHERE {" AND ".join(conditions)}
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 0
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low' THEN 3
                            ELSE 4
                        END,
                        next_run_at ASC
                    LIMIT 10""",
                *params,
            )
            return [dict(r) for r in rows]

    # ── Built-in schedule eligibility checks ───────────────────────────────

    async def built_in_schedule_has_work(
        self,
        title: str,
        org_id: str,
        *,
        schedule_id: str | None = None,
    ) -> bool | None:
        """Return whether a built-in schedule has actionable work.

        ``None`` means the schedule is not a known model-backed built-in and
        should proceed through the normal generic trigger path.
        """
        if title == "Experience Compression":
            return await self.experience_compression_has_work(org_id)
        if title == "Learning Extraction":
            return await self.learning_extraction_has_work(org_id, schedule_id=schedule_id)
        if title == "Memory Consolidation":
            return await self.memory_consolidation_has_work(org_id)
        if title == "Procedural Consolidation":
            return await self.procedural_consolidation_has_work(org_id)
        if title == "Memory Vitality Scoring":
            return await self.memory_vitality_scoring_has_work(org_id)
        if title == "Shadow Forget Scoring":
            return await self.shadow_forget_scoring_has_work(org_id)
        if title == "Cognitive Planning":
            return await self.cognitive_planning_has_work(org_id, schedule_id=schedule_id)
        return None

    async def experience_compression_has_work(self, org_id: str) -> bool:
        """True when there are compressible experience memories before today."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memories
                WHERE organization_id = $1::uuid
                  AND type = 'experience'
                  AND deleted_at IS NULL
                  AND COALESCE(lifecycle_stage, 'active') = 'active'
                  AND created_at < date_trunc('day', now())
                  AND NOT (
                      COALESCE(tags, '{}'::text[])
                      && ARRAY[
                          'daily-digest', 'pinned', 'do_not_consolidate',
                          'heartbeat', 'state', 'telemetry'
                      ]::text[]
                  )
                """,
                org_id,
            )
        return int(count or 0) > 0

    async def learning_extraction_has_work(
        self,
        org_id: str,
        *,
        schedule_id: str | None = None,
    ) -> bool:
        """True when recent results/feedback/rejection lessons need extraction."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                WITH last_run AS (
                    SELECT COALESCE(
                        (
                            SELECT max(completed_at)
                            FROM schedule_runs
                            WHERE schedule_id = $2::uuid
                              AND status = 'completed'
                              AND completed_at IS NOT NULL
                        ),
                        now() - interval '7 days'
                    ) AS since
                    WHERE $2::uuid IS NOT NULL
                    UNION ALL
                    SELECT now() - interval '7 days'
                    WHERE $2::uuid IS NULL
                )
                SELECT COUNT(*)
                FROM memories, last_run
                WHERE organization_id = $1::uuid
                  AND deleted_at IS NULL
                  AND COALESCE(lifecycle_stage, 'active') = 'active'
                  AND updated_at >= last_run.since
                  AND (
                      COALESCE(tags, '{}'::text[])
                      && ARRAY[
                          'daemon-result', 'rejection-lesson',
                          'feedback-rejected', 'feedback-approved', 'validated'
                      ]::text[]
                  )
                  AND NOT ('lesson-extracted' = ANY(COALESCE(tags, '{}'::text[])))
                  AND NOT (
                      COALESCE(tags, '{}'::text[])
                      && ARRAY['heartbeat', 'state', 'telemetry']::text[]
                  )
                """,
                org_id,
                schedule_id,
            )
        return int(count or 0) > 0

    async def memory_consolidation_has_work(self, org_id: str) -> bool:
        """True when technical memories need metadata normalization or merge."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                WITH candidates AS (
                    SELECT id, metadata
                    FROM memories
                    WHERE organization_id = $1::uuid
                      AND type = 'technical'
                      AND shared IS TRUE
                      AND deleted_at IS NULL
                      AND COALESCE(lifecycle_stage, 'active') = 'active'
                      AND NOT (
                          COALESCE(tags, '{}'::text[])
                          && ARRAY[
                              'pinned', 'do_not_consolidate', 'daily-digest',
                              'maintenance', 'task-report', 'heartbeat',
                              'state', 'telemetry'
                          ]::text[]
                      )
                      AND (
                          nullif(metadata->>'repo', '') IS NOT NULL
                          OR EXISTS (
                              SELECT 1
                              FROM unnest(COALESCE(tags, '{}'::text[])) tag
                              WHERE tag ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
                          )
                          OR content ~* '(^|\\n)\\s*#+\\s*(Repository|Directory|Module|File|Filename):'
                          OR content ~ '`(\\.github|daemon|deploy|docker|docs|examples|scripts|src|tests|migrations|app)/'
                      )
                ),
                normalization AS (
                    SELECT COUNT(*) AS n
                    FROM candidates
                    WHERE nullif(metadata->>'repo', '') IS NULL
                       OR NOT (metadata ? 'directory')
                       OR NOT (metadata ? 'filename')
                       OR nullif(metadata->>'category', '') IS NULL
                ),
                duplicates AS (
                    SELECT COUNT(*) AS n
                    FROM (
                        SELECT lower(metadata->>'repo') AS repo,
                               lower(COALESCE(metadata->>'directory', '')) AS directory,
                               lower(COALESCE(metadata->>'filename', '')) AS filename
                        FROM candidates
                        WHERE nullif(metadata->>'repo', '') IS NOT NULL
                          AND metadata ? 'directory'
                          AND metadata ? 'filename'
                        GROUP BY repo, directory, filename
                        HAVING COUNT(*) > 1
                    ) dupes
                )
                SELECT (SELECT n FROM normalization) + (SELECT n FROM duplicates)
                """,
                org_id,
            )
        return int(count or 0) > 0

    async def run_memory_consolidation_metadata_normalization(
        self,
        org_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Run deterministic metadata normalization for codebase technical memories.

        This handles the high-signal, non-LLM part of Memory Consolidation:
        making sure every codebase technical memory has explicit
        ``repo``, ``directory``, ``filename``, and ``category`` metadata keys.
        Semantic duplicate merging remains a separate concern.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, tags, metadata
                FROM memories
                WHERE organization_id = $1::uuid
                  AND type = 'technical'
                  AND shared IS TRUE
                  AND deleted_at IS NULL
                  AND COALESCE(lifecycle_stage, 'active') = 'active'
                  AND NOT (
                      COALESCE(tags, '{}'::text[])
                      && ARRAY[
                          'pinned', 'do_not_consolidate', 'daily-digest',
                          'maintenance', 'task-report', 'heartbeat',
                          'state', 'telemetry'
                      ]::text[]
                  )
                  AND (
                      nullif(metadata->>'repo', '') IS NOT NULL
                      OR EXISTS (
                          SELECT 1
                          FROM unnest(COALESCE(tags, '{}'::text[])) tag
                          WHERE tag ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
                      )
                      OR content ~* '(^|\\n)\\s*#+\\s*(Repository|Directory|Module|File|Filename):'
                      OR content ~ '`(\\.github|daemon|deploy|docker|docs|examples|scripts|src|tests|migrations|app)/'
                  )
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                org_id,
                limit,
            )

            planned = 0
            executed = 0
            skipped = 0
            updated_ids: list[str] = []
            for row in rows:
                planned += 1
                normalized = _derive_codebase_metadata(dict(row))
                if not normalized:
                    skipped += 1
                    continue
                if normalized == dict(row["metadata"] or {}):
                    continue
                updated = await conn.fetchval(
                    """
                    UPDATE memories
                    SET metadata = $2::text::jsonb,
                        updated_at = now(),
                        version = version + 1
                    WHERE id = $1::uuid
                      AND deleted_at IS NULL
                    RETURNING id::text
                    """,
                    str(row["id"]),
                    json.dumps(normalized),
                )
                if updated:
                    executed += 1
                    updated_ids.append(str(updated))

            remaining_normalization = await conn.fetchval(
                """
                WITH candidates AS (
                    SELECT id, metadata
                    FROM memories
                    WHERE organization_id = $1::uuid
                      AND type = 'technical'
                      AND shared IS TRUE
                      AND deleted_at IS NULL
                      AND COALESCE(lifecycle_stage, 'active') = 'active'
                      AND NOT (
                          COALESCE(tags, '{}'::text[])
                          && ARRAY[
                              'pinned', 'do_not_consolidate', 'daily-digest',
                              'maintenance', 'task-report', 'heartbeat',
                              'state', 'telemetry'
                          ]::text[]
                      )
                      AND (
                          nullif(metadata->>'repo', '') IS NOT NULL
                          OR EXISTS (
                              SELECT 1
                              FROM unnest(COALESCE(tags, '{}'::text[])) tag
                              WHERE tag ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
                          )
                          OR content ~* '(^|\\n)\\s*#+\\s*(Repository|Directory|Module|File|Filename):'
                          OR content ~ '`(\\.github|daemon|deploy|docker|docs|examples|scripts|src|tests|migrations|app)/'
                      )
                )
                SELECT COUNT(*)
                FROM candidates
                WHERE nullif(metadata->>'repo', '') IS NULL
                   OR NOT (metadata ? 'directory')
                   OR NOT (metadata ? 'filename')
                   OR nullif(metadata->>'category', '') IS NULL
                """,
                org_id,
            )
            remaining_duplicate_groups = await conn.fetchval(
                """
                WITH candidates AS (
                    SELECT metadata
                    FROM memories
                    WHERE organization_id = $1::uuid
                      AND type = 'technical'
                      AND shared IS TRUE
                      AND deleted_at IS NULL
                      AND COALESCE(lifecycle_stage, 'active') = 'active'
                      AND nullif(metadata->>'repo', '') IS NOT NULL
                      AND metadata ? 'directory'
                      AND metadata ? 'filename'
                      AND NOT (
                          COALESCE(tags, '{}'::text[])
                          && ARRAY[
                              'pinned', 'do_not_consolidate', 'daily-digest',
                              'maintenance', 'task-report', 'heartbeat',
                              'state', 'telemetry'
                          ]::text[]
                      )
                )
                SELECT COUNT(*)
                FROM (
                    SELECT lower(metadata->>'repo') AS repo,
                           lower(COALESCE(metadata->>'directory', '')) AS directory,
                           lower(COALESCE(metadata->>'filename', '')) AS filename
                    FROM candidates
                    GROUP BY repo, directory, filename
                    HAVING COUNT(*) > 1
                ) dupes
                """,
                org_id,
            )

        return {
            "event_type": "memory_consolidation.metadata_normalization",
            "planned_operations": planned,
            "executed_write_operations": executed,
            "skipped_non_codebase": skipped,
            "remaining_normalization_candidates": int(remaining_normalization or 0),
            "remaining_duplicate_groups": int(remaining_duplicate_groups or 0),
            "updated_memory_ids": updated_ids,
        }

    async def procedural_consolidation_has_work(self, org_id: str) -> bool:
        """True when legacy procedural/skill entries need consolidation."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                WITH procedural_memories AS (
                    SELECT id, lower(left(content, 160)) AS fingerprint
                    FROM memories
                    WHERE organization_id = $1::uuid
                      AND deleted_at IS NULL
                      AND COALESCE(lifecycle_stage, 'active') = 'active'
                      AND (
                          type = 'procedural'
                          OR COALESCE(tags, '{}'::text[])
                             && ARRAY[
                                 'procedure', 'procedural', 'skill',
                                 'daemon-task-procedure'
                             ]::text[]
                      )
                      AND NOT (
                          COALESCE(tags, '{}'::text[])
                          && ARRAY['pinned', 'do_not_consolidate']::text[]
                      )
                ),
                flagged_memories AS (
                    SELECT COUNT(*) AS n
                    FROM memories
                    WHERE organization_id = $1::uuid
                      AND deleted_at IS NULL
                      AND COALESCE(lifecycle_stage, 'active') = 'active'
                      AND COALESCE(tags, '{}'::text[])
                          && ARRAY[
                              'needs-merge', 'needs-archive',
                              'needs-canonicalization'
                          ]::text[]
                ),
                duplicate_memories AS (
                    SELECT COUNT(*) AS n
                    FROM (
                        SELECT fingerprint
                        FROM procedural_memories
                        GROUP BY fingerprint
                        HAVING COUNT(*) > 1
                    ) dupes
                ),
                duplicate_skills AS (
                    SELECT COUNT(*) AS n
                    FROM (
                        SELECT lower(name) AS name_key
                        FROM skill_definitions
                        WHERE organization_id = $1::uuid
                          AND status NOT IN ('rejected', 'archived')
                        GROUP BY lower(name)
                        HAVING COUNT(*) > 1
                    ) dupes
                )
                SELECT (SELECT n FROM flagged_memories)
                     + (SELECT n FROM duplicate_memories)
                     + (SELECT n FROM duplicate_skills)
                """,
                org_id,
            )
        return int(count or 0) > 0

    async def memory_vitality_scoring_has_work(self, org_id: str) -> bool:
        """True when a non-forgotten memory needs missing or stale vitality."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memories
                WHERE organization_id = $1::uuid
                  AND deleted_at IS NULL
                  AND COALESCE(lifecycle_stage, 'active') != 'forgotten'
                  AND (
                      vitality_computed_at IS NULL
                      OR vitality_computed_at < updated_at
                      OR vitality_computed_at < now() - interval '6 hours'
                  )
                """,
                org_id,
            )
        return int(count or 0) > 0

    async def shadow_forget_scoring_has_work(self, org_id: str) -> bool:
        """True when shadow forgetting is enabled and fresh sidecar scores are missing."""
        from lucent.settings import shadow_forget_enabled

        if not shadow_forget_enabled():
            return False
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memories m
                WHERE m.organization_id = $1::uuid
                  AND m.deleted_at IS NULL
                  AND COALESCE(m.lifecycle_stage, 'active') != 'forgotten'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memory_shadow_scores s
                      WHERE s.memory_id = m.id
                        AND s.strategy = 'gcp-v1'
                        AND s.computed_at >= now() - interval '6 hours'
                  )
                """,
                org_id,
            )
        return int(count or 0) > 0

    async def cognitive_planning_has_work(
        self,
        org_id: str,
        *,
        schedule_id: str | None = None,
    ) -> bool:
        """True when cognitive fan-out has work it can actually process.

        The scheduled cognitive task is dispatched through the daemon's
        per-user goal/rejection fan-out path.  Dispatcher-visible requests,
        reviews, proposed definitions, and generic feedback are handled by
        other loops; counting them here creates empty Cognitive Planning
        requests that immediately discover ``targets=0``.  Keep this predicate
        aligned with ``LucentDaemon._run_cognitive_planning_fanout``.
        """
        async with self.pool.acquire() as conn:
            signal_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM requests
                WHERE organization_id = $1::uuid
                  AND status = 'rejection_processing'
                """,
                org_id,
            )
        if int(signal_count or 0) > 0:
            return True

        from lucent.db.requests import RequestRepository

        targets = await RequestRepository(self.pool).list_planning_targets(org_id, limit=1)
        return bool(targets)

    async def mark_schedule_run(
        self, schedule_id: str, request_id: str | None = None, *, force: bool = False
    ) -> dict | None:
        """Record a run and advance the schedule's next_run_at.

        Returns None if the schedule was already advanced (idempotency guard).
        Pass force=True to bypass the time check (e.g. manual trigger via API).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                sched = await conn.fetchrow(
                    "SELECT * FROM schedules WHERE id = $1::uuid FOR UPDATE",
                    schedule_id,
                )
                if not sched:
                    raise ValueError(f"Schedule {schedule_id} not found")

                now = datetime.now(timezone.utc)

                # Idempotency guard: never fire a non-active schedule
                if sched["status"] != "active":
                    return None

                if not force:
                    # Time-based guard: if next_run_at is null (no future run
                    # scheduled) or in the future (already advanced by another
                    # cycle), skip to prevent duplicate runs.
                    if sched["next_run_at"] is None or sched["next_run_at"] > now:
                        return None
                new_run_count = (sched["run_count"] or 0) + 1

                # Create the run record
                run = await conn.fetchrow(
                    """INSERT INTO schedule_runs (schedule_id, request_id, status, started_at)
                       VALUES ($1::uuid, $2::uuid, 'running', $3)
                       RETURNING *""",
                    schedule_id,
                    request_id,
                    now,
                )

                # Calculate next run
                next_run = None
                new_status = sched["status"]

                if sched["schedule_type"] == "once":
                    new_status = "completed"
                elif sched["schedule_type"] == "interval":
                    next_run = now + timedelta(seconds=sched["interval_seconds"])
                elif sched["schedule_type"] == "cron" and sched["cron_expression"]:
                    try:
                        tz_name = sched.get("timezone") or "UTC"
                        next_run = _next_cron_utc(sched["cron_expression"], now, tz_name)
                    except ValueError:
                        new_status = "expired"

                # Check if we've hit max_runs
                if sched["max_runs"] and new_run_count >= sched["max_runs"]:
                    new_status = "completed"
                    next_run = None

                # Check expiration
                if sched["expires_at"] and next_run and next_run > sched["expires_at"]:
                    new_status = "expired"
                    next_run = None

                await conn.execute(
                    """UPDATE schedules SET
                       last_run_at = $2, run_count = $3,
                       next_run_at = $4, status = $5, updated_at = $2
                       WHERE id = $1::uuid""",
                    schedule_id,
                    now,
                    new_run_count,
                    next_run,
                    new_status,
                )

                return dict(run)

    async def complete_run(self, run_id: str, result: str | None = None) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE schedule_runs SET
                   status = 'completed', completed_at = now(), result = $2
                   WHERE id = $1::uuid RETURNING *""",
                run_id,
                result,
            )
            return dict(row) if row else None

    async def fail_run(self, run_id: str, error: str | None = None) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE schedule_runs SET
                   status = 'failed', completed_at = now(), error = $2
                   WHERE id = $1::uuid RETURNING *""",
                run_id,
                error,
            )
            return dict(row) if row else None

    async def link_run_to_request(self, run_id: str, request_id: str) -> None:
        """Set the request_id on a schedule run after the request is created."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE schedule_runs SET request_id = $2::uuid WHERE id = $1::uuid",
                run_id,
                request_id,
            )

    # ── Run history ───────────────────────────────────────────────────────

    async def list_runs(self, schedule_id: str, limit: int = 25, offset: int = 0) -> dict:
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM schedule_runs WHERE schedule_id = $1::uuid",
                schedule_id,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                """SELECT * FROM schedule_runs
                   WHERE schedule_id = $1::uuid
                   ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
                schedule_id,
                limit,
                offset,
            )
            return {
                "items": [dict(r) for r in rows],
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(rows) < total_count,
            }

    async def get_schedule_with_runs(self, schedule_id: str, org_id: str) -> dict | None:
        """Load a schedule with its recent run history."""
        sched = await self.get_schedule(schedule_id, org_id)
        if not sched:
            return None
        sched["runs"] = (await self.list_runs(schedule_id))["items"]
        return sched

    # ── Summary ───────────────────────────────────────────────────────────

    async def get_summary(self, org_id: str) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                   count(*) as total,
                   count(*) FILTER (WHERE enabled AND status = 'active') as active,
                   count(*) FILTER (WHERE NOT enabled OR status = 'paused') as paused,
                   count(*) FILTER (WHERE status = 'completed') as completed,
                   count(*) FILTER (
                       WHERE next_run_at <= now() AND enabled
                       AND status = 'active'
                   ) as due_now,
                   count(*) FILTER (WHERE schedule_type = 'once') as one_time,
                   count(*) FILTER (WHERE schedule_type = 'interval') as interval,
                   count(*) FILTER (WHERE schedule_type = 'cron') as cron
                   FROM schedules WHERE organization_id = $1::uuid""",
                org_id,
            )
            return dict(row)
