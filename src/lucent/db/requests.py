"""Repository for request tracking and task queue.

Full lineage: request → tasks → events → memory links.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from asyncpg import Pool
from jsonschema import SchemaError
from jsonschema.validators import validator_for

from lucent.constants import (
    DECOMPOSITION_MODE_AUTHOR,
    DECOMPOSITION_MODE_PLANNER,
    INTERRUPTIBLE_TASK_STATUSES,
    REQUEST_STATUS_CANCELLED,
    REQUEST_STATUS_COMPLETED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_IN_PROGRESS,
    REQUEST_STATUS_NEEDS_REWORK,
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_PLANNED,
    REQUEST_STATUS_REVIEW,
    VALID_DECOMPOSITION_MODES,
    VALID_REQUEST_SOURCES,
    VALID_REQUEST_STATUSES,
)
from lucent.db.pool import scoped_acquire_on
from lucent.llm.decision import (
    MAX_QUESTIONS,
    MAX_CHOICE_OPTIONS,
    MAX_SCORE_LEVELS,
    MIN_CHOICE_OPTIONS,
    MIN_SCORE_LEVELS,
    QUESTION_TYPES,
    DecisionQuestion,
)

logger = logging.getLogger(__name__)

DEFAULT_TASK_LEASE_SECONDS = int(os.environ.get("LUCENT_TASK_LEASE_SECONDS", "1800"))
# How long a remote claim must be held before the controller may requeue it on
# the strength of the remote saying it is not working on it. Covers the gap
# between claiming a task and recording it as active in the client.
DEFAULT_REMOTE_ORPHAN_GRACE_SECONDS = int(
    os.environ.get("LUCENT_REMOTE_ORPHAN_GRACE_SECONDS", "90")
)
DEFAULT_INSTANCE_STALE_SECONDS = int(os.environ.get("LUCENT_INSTANCE_STALE_SECONDS", "1800"))

# Activity-staleness predicate for the stale-task reaper, shared by
# release_stale_tasks() and stale_task_reaper_has_work() so the preflight gate
# and the reaper can never drift apart. $1 is the stale threshold in seconds.
# A claimed/running task counts as stale only when it has NO activity within
# the threshold — where activity is a heartbeat from the owning daemon's
# heartbeat loop, a task-attributed tool-audit row, or a task event. Claim age
# alone never releases a task that is actively working.
_STALE_TASK_ACTIVITY_CLAUSE = """
                                 COALESCE(t.last_heartbeat_at, t.claimed_at)
                                     < NOW() - make_interval(secs := $1)
                                 AND NOT EXISTS (
                                     SELECT 1 FROM tool_call_audit_log a
                                     WHERE a.task_id = t.id
                                       AND a.created_at
                                           >= NOW() - make_interval(secs := $1)
                                 )
                                 AND NOT EXISTS (
                                     SELECT 1 FROM task_events te
                                     WHERE te.task_id = t.id
                                       AND te.created_at
                                           >= NOW() - make_interval(secs := $1)
                                 )
"""

# Advisory lock namespace for serializing memory-link operations on the same
# memory_id across concurrent transactions. Keyed alongside the memory UUID.
# Distinct from the request-decomposition advisory-lock namespace.
MEMORY_LINK_LOCK_NAMESPACE = 0x4C4D454D  # "LMEM" — Lucent MEMory link

# How recently a completed/cancelled request must have happened for a
# normalized-title match to be considered a duplicate of new work for the
# same goal. The cognitive planner runs periodically; if a request finished
# in the last day and another one with the same normalized title comes in,
# it's almost certainly the planner re-proposing work because the goal's
# milestone state hasn't been updated.
RECENT_COMPLETION_WINDOW_HOURS = 24

VALID_TASK_OUTPUT_TYPES = {
    "link",
    "github_issue",
    "github_pr",
    "email",
    "document",
    "file",
    "memory",
    "deployment",
    "artifact",
    "other",
}

_OUTPUT_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()\[\]{}\"']+|mailto:[^\s<>()\[\]{}\"']+)")
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}>'\""


def _normalize_title_for_dedup(title: str | None) -> str:
    """Normalize a request title for duplicate detection.

    Lowercases, replaces all runs of non-alphanumeric characters with a
    single space, and trims. This collapses superficial differences like
    hyphen-vs-space, em-dash, or extra punctuation that the planner often
    introduces between cycles.

    Examples:
        'Native forgetting M2: Propose strategy candidates'
        'Native-forgetting M2 — propose strategy candidates'
        → both normalize to 'native forgetting m2 propose strategy candidates'
    """
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()



def _coerce_metadata(metadata: Any) -> dict | None:
    """Normalize a memory.metadata value (dict or JSON string) into a dict."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return None
    return metadata if isinstance(metadata, dict) else None


def _infer_output_type_from_url(url: str | None, fallback: str = "link") -> str:
    """Infer a well-known output type from a URL when callers provide generic links."""
    if not url:
        return fallback if fallback in VALID_TASK_OUTPUT_TYPES else "link"
    try:
        parsed = urlparse(url)
    except Exception:
        return fallback if fallback in VALID_TASK_OUTPUT_TYPES else "link"

    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "github.com" or host.endswith(".github.com"):
        if "/pull/" in path:
            return "github_pr"
        if "/issues/" in path:
            return "github_issue"
    if parsed.scheme == "mailto":
        return "email"
    if "docs.google.com" in host or "notion.so" in host or path.endswith(('.md', '.pdf')):
        return "document"
    return fallback if fallback in VALID_TASK_OUTPUT_TYPES else "link"


def _normalize_task_output(output: dict[str, Any]) -> dict[str, Any]:
    """Normalize a task output artifact supplied by API/MCP/structured result."""
    if not isinstance(output, dict):
        raise ValueError("Each task output must be an object")

    url = str(output.get("url") or "").strip() or None
    external_id = str(output.get("external_id") or output.get("id") or "").strip() or None
    output_type = str(output.get("output_type") or output.get("type") or "link").strip().lower()
    output_type = _infer_output_type_from_url(url, output_type)
    if output_type not in VALID_TASK_OUTPUT_TYPES:
        raise ValueError(
            f"Invalid output_type '{output_type}'. Must be one of: "
            f"{', '.join(sorted(VALID_TASK_OUTPUT_TYPES))}"
        )

    title = str(output.get("title") or "").strip()
    if not title:
        if output_type == "github_pr":
            title = "GitHub pull request"
        elif output_type == "github_issue":
            title = "GitHub issue"
        elif output_type == "email":
            title = "Email"
        elif output_type == "document":
            title = "Document"
        elif url:
            title = url
        elif external_id:
            title = external_id
    if not title:
        raise ValueError("Task output title is required")
    if not url and not external_id and output_type != "other":
        raise ValueError("Task output requires url or external_id")

    metadata = output.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Task output metadata must be an object")

    return {
        "output_type": output_type,
        "provider": str(output.get("provider") or "").strip() or None,
        "title": title[:256],
        "description": str(output.get("description") or "").strip() or None,
        "url": url,
        "external_id": external_id,
        "mime_type": str(output.get("mime_type") or "").strip() or None,
        "metadata": metadata,
        "is_primary": bool(output.get("is_primary", False)),
    }


def _extract_outputs_from_structured_result(result_structured: Any) -> list[dict[str, Any]]:
    """Extract outputs/artifacts from validated structured task output when present."""
    if not isinstance(result_structured, dict):
        return []
    raw = result_structured.get("outputs") or result_structured.get("artifacts") or []
    if not isinstance(raw, list):
        return []
    return [_normalize_task_output(item) for item in raw]


def _extract_outputs_from_text(result_text: str | None) -> list[dict[str, Any]]:
    """Extract openable URLs from unstructured task output as artifact links.

    This is a reliability backstop: agents should still call record_task_output
    or return structured outputs, but links mentioned in the narrative result
    should not be invisible to users if the agent forgets.
    """
    if not result_text:
        return []

    outputs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for match in _OUTPUT_URL_RE.finditer(result_text):
        raw_url = match.group("url").rstrip(_TRAILING_URL_PUNCTUATION)
        if not raw_url or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)

        line_start = result_text.rfind("\n", 0, match.start()) + 1
        line_end = result_text.find("\n", match.end())
        if line_end == -1:
            line_end = len(result_text)
        line = result_text[line_start:line_end].strip()
        title_prefix = line[: max(0, match.start() - line_start)].strip(" -*•:—–\t")
        output_type = _infer_output_type_from_url(raw_url)
        if title_prefix and len(title_prefix) <= 120:
            title = title_prefix
        elif output_type == "github_pr":
            title = "GitHub pull request"
        elif output_type == "github_issue":
            title = "GitHub issue"
        elif output_type == "email":
            title = "Email"
        elif output_type == "document":
            title = "Document"
        else:
            title = raw_url

        outputs.append(
            _normalize_task_output(
                {
                    "output_type": output_type,
                    "title": title,
                    "url": raw_url,
                    "metadata": {"auto_extracted": True, "source": "task_result"},
                }
            )
        )
    return outputs


def _dedupe_task_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate normalized task outputs by URL/external identity."""
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for output in outputs:
        key_value = output.get("url") or output.get("external_id") or output.get("title")
        key = (output.get("output_type") or "link", str(key_value or "").lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(output)
    return deduped


def _next_active_milestone_index(metadata: dict | None) -> int | None:
    """Return the 1-based index of the first 'active' milestone, or None.

    None can mean: goal has no milestones array, OR every milestone is
    completed/abandoned. Use ``_milestones_summary`` if you need to
    distinguish those two cases.
    """
    if not metadata:
        return None
    milestones = metadata.get("milestones") or []
    if not isinstance(milestones, list):
        return None
    for i, m in enumerate(milestones, start=1):
        if isinstance(m, dict) and m.get("status") == "active":
            return i
    return None


def _milestones_summary(metadata: dict | None) -> dict:
    """Summarize a goal's milestone state for diagnostics and refusal output."""
    out = {
        "total": 0,
        "active_indexes": [],
        "completed_count": 0,
        "abandoned_count": 0,
    }
    if not metadata:
        return out
    milestones = metadata.get("milestones") or []
    if not isinstance(milestones, list):
        return out
    out["total"] = len(milestones)
    for i, m in enumerate(milestones, start=1):
        if not isinstance(m, dict):
            continue
        status = m.get("status")
        if status == "active":
            out["active_indexes"].append(i)
        elif status == "completed":
            out["completed_count"] += 1
        elif status == "abandoned":
            out["abandoned_count"] += 1
    return out


def _validate_structured_goal_milestone(
    goal_id: str,
    metadata: dict | None,
    milestone_index: int | None,
) -> dict | None:
    """Validate a (goal_id, milestone_index) pair against the goal's metadata.

    Returns ``None`` when the request may proceed, or a refusal dict
    describing why not. Used by the structured create_request path —
    callers that pass goal_id/goal_milestone_index get an exact,
    field-driven validation with no string parsing involved.

    Rules:
      * goal must not be metadata.status in ('completed', 'abandoned')
      * if the goal has a milestones array but milestone_index is None,
        refuse — milestone-having goals must name which milestone
      * if milestone_index is given, that milestone must exist and
        have status='active'
      * if the milestone has 'start_after' (ISO 8601 string), now() must
        be >= start_after — skip future-dated milestones
      * if the goal has milestones but none are active, refuse and tell
        the caller to mark the goal completed
    """
    if not isinstance(metadata, dict):
        # Goal exists but has no metadata — treat as plannable open-ended
        # if no milestone was requested; refuse if a milestone was named.
        if milestone_index is not None:
            return {
                "id": goal_id,
                "title": None,
                "status": "skipped",
                "reason": "milestone_not_found",
                "detail": (
                    f"goal_milestone_index={milestone_index} was specified "
                    f"but goal {goal_id} has no milestones array."
                ),
            }
        return None

    goal_status = metadata.get("status")
    if goal_status in ("completed", "abandoned"):
        return {
            "id": goal_id,
            "title": None,
            "status": "skipped",
            "reason": "goal_completed",
            "detail": (
                f"Goal {goal_id} has metadata.status={goal_status!r} and "
                "must not spawn new work."
            ),
        }

    milestones = metadata.get("milestones") or []
    if not isinstance(milestones, list):
        milestones = []

    # Goal has milestones but caller didn't name one — they must, so
    # validation can pin it to a specific entry. Open-ended goals with
    # an empty/missing array don't require milestone_index.
    if milestones and milestone_index is None:
        summary = _milestones_summary(metadata)
        return {
            "id": goal_id,
            "title": None,
            "status": "skipped",
            "reason": "milestone_required",
            "detail": (
                f"Goal {goal_id} has {len(milestones)} milestones — "
                "callers must specify goal_milestone_index. Active "
                f"milestones: {summary['active_indexes']}."
            ),
        }

    # No milestones at all + no index requested = open-ended goal, ok.
    if not milestones:
        return None

    if milestone_index is None or milestone_index < 1 or milestone_index > len(milestones):
        return {
            "id": goal_id,
            "title": None,
            "status": "skipped",
            "reason": "milestone_out_of_range",
            "detail": (
                f"goal_milestone_index={milestone_index} is invalid for "
                f"goal {goal_id} ({len(milestones)} milestones)."
            ),
        }

    milestone = milestones[milestone_index - 1]
    if not isinstance(milestone, dict):
        return {
            "id": goal_id,
            "title": None,
            "status": "skipped",
            "reason": "milestone_not_found",
            "detail": (
                f"Milestone at index {milestone_index} of goal {goal_id} "
                "is malformed."
            ),
        }

    mstatus = milestone.get("status")
    if mstatus != "active":
        summary = _milestones_summary(metadata)
        return {
            "id": goal_id,
            "title": None,
            "status": "skipped",
            "reason": "milestone_not_active",
            "detail": (
                f"Milestone {milestone_index} of goal {goal_id} has "
                f"status={mstatus!r}, not 'active'. Active milestones: "
                f"{summary['active_indexes']}."
            ),
        }

    # Optional date gate: 'start_after' (ISO 8601). If present and in the
    # future, the milestone is not yet plannable.
    start_after_raw = milestone.get("start_after")
    if start_after_raw:
        start_after_dt = _parse_iso_datetime(start_after_raw)
        if start_after_dt is not None:
            if start_after_dt > datetime.now(timezone.utc):
                return {
                    "id": goal_id,
                    "title": None,
                    "status": "skipped",
                    "reason": "milestone_not_yet_active",
                    "detail": (
                        f"Milestone {milestone_index} of goal {goal_id} "
                        f"has start_after={start_after_raw} which is in "
                        "the future. Skip until that date."
                    ),
                }

    return None


def _parse_iso_datetime(raw: Any) -> datetime | None:
    """Parse an ISO 8601 datetime/date string into a tz-aware datetime.

    Accepts:
      * 'YYYY-MM-DD' → midnight UTC
      * 'YYYY-MM-DDTHH:MM:SS[+TZ]' → as-given (UTC default)
      * trailing 'Z' (Zulu) is normalized to '+00:00' before parsing
    Returns None on parse failure or non-string input.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Normalize 'Z' suffix.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        if "T" in s or " " in s:
            dt = datetime.fromisoformat(s)
        else:
            # Date-only.
            dt = datetime.fromisoformat(s + "T00:00:00")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Approval statuses for the pre-work gate
APPROVAL_AUTO = "auto_approved"
APPROVAL_PENDING = "pending_approval"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"

# Sources subject to the auto-approve toggle.
# Schedule is excluded — scheduled requests are always auto-approved because
# schedules are either user-created or built-in system tasks.
_DAEMON_SOURCES = frozenset({"cognitive", "daemon"})


def _requires_approval(source: str, *, organization_id: str | None = None) -> bool:
    """Check if a request from this source needs human approval.

    User/API/schedule requests are always auto-approved.
    Cognitive/daemon requests require approval unless
    LUCENT_AUTO_APPROVE is set to true (default: false).
    """
    if source not in _DAEMON_SOURCES:
        return False
    from lucent.settings import daemon_auto_approve_enabled

    return not daemon_auto_approve_enabled(organization_id=organization_id)


def _requires_post_completion_review(*, organization_id: str | None = None) -> bool:
    """Check if completed requests should go through internal review.

    The daemon's post-completion review task is an automatic quality check
    (did the work accomplish what was requested?).  It always runs by default
    because it auto-approves or sends work back for rework — no human needed.

    Set LUCENT_SKIP_POST_REVIEW=true to bypass the automatic review task
    and send completed requests straight to 'completed' status.
    """
    from lucent.settings import post_completion_review_enabled

    return post_completion_review_enabled(organization_id=organization_id)


# Request states that task completion is allowed to advance out of.
#
# ``_check_request_completion`` runs on every task transition, so it must never
# be able to *regress* a decision that has already been made. ``completed``,
# ``cancelled`` and ``failed`` are settled outcomes a human or the review gate
# already chose; ``review`` is an explicit gate whose owner is the review task
# (or a human), not an incidental sibling task. ``rejection_processing`` is an
# approval-flow state that only the rejection path may leave.
_COMPLETION_ADVANCEABLE_STATUSES: tuple[str, ...] = (
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_PLANNED,
    REQUEST_STATUS_IN_PROGRESS,
    REQUEST_STATUS_NEEDS_REWORK,
)

# Matches the daemon's `_parse_review_decision` recognition rule. Kept in sync
# with it deliberately: two parsers that disagreed would let the reconciler
# apply a decision the live review path would not (or vice versa).
_REVIEW_DECISION_RE = re.compile(
    r"(?:REQUEST_REVIEW_DECISION|DECISION)\s*:\s*(APPROVED|NEEDS_REWORK)",
    re.IGNORECASE,
)

# Mirrors `TASK_IDS_TO_REWORK:` parsing in daemon/review/lifecycle.py. Without
# it the reconciler could apply a NEEDS_REWORK verdict but not reopen the tasks
# the verdict named — leaving the request labelled "needs rework" with every
# task still `completed` and nothing dispatchable, i.e. the same stall under a
# new name.
_REVIEW_TASK_IDS_RE = re.compile(
    r"TASK_IDS_TO_REWORK\s*:\s*(.+?)(?:\n[A-Z_ ]+\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# A task id as the daemon accepts it: bare hex/uuid, optionally wrapped.
_REVIEW_TASK_ID_RE = re.compile(r"[0-9a-fA-F-]{8,64}")

# Mirrors `FEEDBACK:` parsing in the daemon's review parser. Carried into the
# reopened task so the next attempt knows *why* it was sent back, exactly as
# the live rework path does.
_REVIEW_FEEDBACK_RE = re.compile(
    r"FEEDBACK\s*:\s*(.+?)(?:\n[A-Z_ ]+\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _recorded_review_decision(result: str | None) -> str | None:
    """Return the decision a completed review task recorded, or None.

    Only used to finish a decision that was already made but never landed on
    the request row. Returning None means "no usable decision on record" —
    callers must then leave the request for a human rather than guess.
    """
    match = _REVIEW_DECISION_RE.search(result or "")
    return match.group(1).upper() if match else None


def _recorded_review_task_ids(result: str | None) -> list[str]:
    """Return the task ids a NEEDS_REWORK verdict asked to reopen.

    Mirrors the daemon's parser so a reconciled rework re-queues the same tasks
    the live review path would have re-queued. An empty list means the verdict
    named no reopenable task, which callers treat as "cannot proceed
    automatically".
    """
    match = _REVIEW_TASK_IDS_RE.search(result or "")
    if not match:
        return []
    ids: list[str] = []
    for candidate in re.split(r"[\s,]+", match.group(1).strip()):
        cleaned = candidate.strip().strip("[](){}")
        if cleaned and _REVIEW_TASK_ID_RE.fullmatch(cleaned):
            ids.append(cleaned)
    return ids


def _recorded_review_feedback(result: str | None) -> str:
    """Return the reviewer's feedback text, or a neutral fallback.

    The live rework path stores this on the reopened task so the next attempt
    knows why it was sent back; a reconciled rework should not be less
    informative than a live one.
    """
    match = _REVIEW_FEEDBACK_RE.search(result or "")
    if match:
        text = match.group(1).strip()
        if text:
            return text[:10000]
    return "Reopened by the stuck-review reconciler."


_VALID_OUTPUT_FAILURE_POLICIES = {"fail", "fallback", "retry_then_fallback"}
_VALID_VALIDATION_STATUSES = {
    "not_applicable",
    "valid",
    "invalid",
    "extraction_failed",
    "fallback_used",
    "repair_succeeded",
}


def _validate_output_contract(output_contract: dict | None) -> None:
    """Validate output_contract shape and JSON Schema structure.

    Contract format:
      {
        "json_schema": {...},
        "on_failure": "fail|fallback|retry_then_fallback",  # optional
        "max_retries": 1,                                   # optional
      }
    """
    if output_contract is None:
        return
    if not isinstance(output_contract, dict):
        raise ValueError("output_contract must be an object")

    json_schema = output_contract.get("json_schema")
    if json_schema is None:
        raise ValueError("output_contract must include 'json_schema'")
    if not isinstance(json_schema, dict):
        raise ValueError("output_contract.json_schema must be an object")

    try:
        validator_cls = validator_for(json_schema)
        validator_cls.check_schema(json_schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid output_contract.json_schema: {exc.message}") from exc

    on_failure = output_contract.get("on_failure", "fallback")
    if on_failure not in _VALID_OUTPUT_FAILURE_POLICIES:
        valid = ", ".join(sorted(_VALID_OUTPUT_FAILURE_POLICIES))
        raise ValueError(
            f"Invalid output_contract.on_failure '{on_failure}'. "
            f"Must be one of: {valid}"
        )

    max_retries = output_contract.get("max_retries", 1)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("output_contract.max_retries must be an integer >= 0")


def _validate_decision_threshold_policy(
    threshold_policy: Any, questions: dict[str, str]
) -> tuple[str, dict[str, dict]]:
    """Validate a decision_config threshold_policy; returns (on_fail, thresholds).

    ``questions`` maps question name → type. Noul thresholds mean truth
    probabilities (bounded [0, 1]); score thresholds mean level-index
    positions (documented, not bounded) — choice questions are never
    thresholded because a choice answer is classification output, not a
    degree of confidence in a condition.
    """
    if not isinstance(threshold_policy, dict):
        raise ValueError("decision_config.threshold_policy must be an object")
    on_fail = threshold_policy.get("on_fail", "fail_task")
    if on_fail not in ("fail_task", "needs_review"):
        raise ValueError(
            "decision_config.threshold_policy.on_fail must be 'fail_task' "
            f"or 'needs_review'; got {on_fail!r}"
        )
    raw_thresholds = threshold_policy.get("questions")
    if not isinstance(raw_thresholds, dict) or not raw_thresholds:
        raise ValueError(
            "decision_config.threshold_policy.questions must be a non-empty "
            "object mapping question name to a minimum"
        )
    thresholds: dict[str, dict] = {}
    for name, minimum in raw_thresholds.items():
        if name not in questions:
            raise ValueError(
                "decision_config.threshold_policy references unknown question "
                f"'{name}'"
            )
        qtype = questions[name]
        if qtype == "choice":
            raise ValueError(
                f"Question '{name}' is a choice question — choice answers are "
                "classification output and cannot carry a threshold"
            )
        if isinstance(minimum, dict):
            minimum = minimum.get("min")
            if minimum is not None and isinstance(minimum, dict):
                raise ValueError(f"decision_config threshold for '{name}' is malformed")
        if minimum is None:
            raise ValueError(f"decision_config threshold for '{name}' requires a 'min'")
        try:
            minimum = float(minimum)
        except (TypeError, ValueError):
            raise ValueError(
                f"decision_config threshold for '{name}' must be a number; "
                f"got {minimum!r}"
            ) from None
        if qtype == "noul" and not 0.0 <= minimum <= 1.0:
            raise ValueError(
                f"decision_config threshold for '{name}' must be between 0 "
                f"and 1 for a noul question; got {minimum}"
            )
        thresholds[name] = {"min": minimum}
    return on_fail, thresholds


def validate_decision_questions(
    raw_questions: Any, *, label: str = "decision_config"
) -> dict[str, str]:
    """Validate the shared decision-question schema.

    Used by decision-step tasks (``label="decision_config"``) and fusion
    arbiter configs (``label="fusion arbiter"``). Returns the ordered
    name → type map so callers can validate thresholds against it.

    Raises ValueError with an author-facing message on any malformed shape.
    """
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError(
            f"{label}.questions must be a non-empty list of question objects"
        )
    if len(raw_questions) > MAX_QUESTIONS:
        raise ValueError(
            f"Too many {label} questions: {len(raw_questions)} "
            f"(server allows at most {MAX_QUESTIONS})."
        )

    # Array (not object) so order is preserved and duplicate names surface.
    questions: dict[str, str] = {}
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise ValueError(f"{label}.questions[{index}] must be an object")
        name = raw.get("name")
        qtype = raw.get("type")
        instructions = raw.get("instructions")
        if not name or not isinstance(name, str):
            raise ValueError(f"{label}.questions[{index}] requires a 'name'")
        if len(name) > 128:
            raise ValueError(
                f"decision question name '{name}' is too long (max 128 characters)"
            )
        if name in questions:
            raise ValueError(f"{label} has duplicate question name '{name}'")
        if qtype not in QUESTION_TYPES:
            raise ValueError(
                f"Question '{name}' has unknown type {qtype!r}. "
                f"Expected one of: {', '.join(sorted(QUESTION_TYPES))}."
            )
        if not instructions or not isinstance(instructions, str):
            raise ValueError(f"Question '{name}' requires instructions")
        try:
            DecisionQuestion(
                name=name,
                type=qtype,
                instructions=instructions,
                criteria=raw.get("criteria"),
            )
        except ValueError as exc:
            message = str(exc).replace("Decision request", label)
            raise ValueError(message) from None
        questions[name] = qtype

    # _validate_request's criteria checks run through DecisionQuestion above;
    # construction itself does not validate criteria ranges, so enforce them
    # here against the same client-side limits the client applies at send.
    for raw in raw_questions:
        name = raw["name"]
        qtype = raw["type"]
        criteria = raw.get("criteria")
        if qtype == "choice":
            options = criteria or {}
            if not isinstance(options, dict):
                raise ValueError(
                    f"Choice question '{name}' criteria must be an "
                    "option key → description mapping."
                )
            if not MIN_CHOICE_OPTIONS <= len(options) <= MAX_CHOICE_OPTIONS:
                raise ValueError(
                    f"Choice question '{name}' needs between {MIN_CHOICE_OPTIONS} "
                    f"and {MAX_CHOICE_OPTIONS} options; got {len(options)}."
                )
        elif qtype == "score":
            levels = criteria or []
            if not isinstance(levels, list):
                raise ValueError(
                    f"Score question '{name}' criteria must be an ordered "
                    "list of level labels."
                )
            if not MIN_SCORE_LEVELS <= len(levels) <= MAX_SCORE_LEVELS:
                raise ValueError(
                    f"Score question '{name}' needs between {MIN_SCORE_LEVELS} "
                    f"and {MAX_SCORE_LEVELS} levels; got {len(levels)}."
                )
    return questions


def validate_decision_config(config: dict | None) -> None:
    """Validate a decision_config payload for a decision-step task.

    Config format:
      {
        "questions": [ {"name", "type": "noul|choice|score",
                        "instructions", "criteria"} ],   # 1..64, ordered
        "state": {"<key>": "<literal>"}                  # optional
        "model": "<decision model id>",                  # optional
        "threshold_policy": {"questions": {name: {"min": f}},
                             "on_fail": "fail_task|needs_review"},
      }

    Raises ValueError with an author-facing message on any malformed shape.
    """
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("decision_config must be an object")

    raw_questions = config.get("questions")
    if raw_questions is None:
        raise ValueError("decision_config requires a 'questions' list")
    questions = validate_decision_questions(raw_questions, label="decision_config")

    state = config.get("state")
    if state is not None:
        if not isinstance(state, dict):
            raise ValueError("decision_config.state must be an object of strings")
        for key, value in state.items():
            if not isinstance(key, str) or not key:
                raise ValueError("decision_config.state keys must be non-empty strings")
            if not isinstance(value, str):
                raise ValueError(
                    f"decision_config.state['{key}'] must be a string "
                    "(literal state fields only)"
                )

    model = config.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("decision_config.model must be a non-empty model id")

    if "threshold_policy" in config:
        _validate_decision_threshold_policy(
            config["threshold_policy"], questions
        )

    unknown = set(config) - {
        "questions",
        "state",
        "model",
        "threshold_policy",
    }
    if unknown:
        raise ValueError(
            "decision_config has unknown key(s): "
            + ", ".join(sorted(unknown))
        )

    return None


_FUSION_MODES = ("best_of_n", "synthesize")


def validate_fusion_config(
    config: dict | None,
    *,
    max_candidates: int = 4,
    best_of_n_enabled: bool = True,
    synthesis_enabled: bool = True,
) -> None:
    """Validate a fusion_config payload for a chat task.

    Config format:
      {
        "mode": "best_of_n" | "synthesize",
        "candidates": {"models": ["<chat model id>", ...]}   # 2..max_candidates
                    | {"count": 2..max_candidates},          # exactly one of the two
        "arbiter": {                                          # required for best_of_n,
                    "model": "<decision model id>",           # optional for synthesize
                    "questions": [<decision questions>],      # required when arbiter present
                    "threshold_policy": {...}},               # optional
        "synthesis": {"model": "<chat model id or null>"},    # required iff synthesize
        "refine": {"model": "<chat model id or null>",        # optional, best_of_n only
                   "max_refine_passes": 1|2},
      }

    The flag parameters are the fusion.* runtime settings: authoring
    surfaces pass the real values so a disabled mode raises instead of
    persisting a config the dispatcher would silently ignore. Structural
    validation (no flags) happens again at the repo layer on every create.

    Raises ValueError with an author-facing message on any malformed shape.
    """
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("fusion_config must be an object")

    mode = config.get("mode")
    if mode not in _FUSION_MODES:
        raise ValueError(
            "fusion_config.mode must be 'best_of_n' or 'synthesize'; "
            f"got {mode!r}"
        )
    if mode == "best_of_n" and not best_of_n_enabled:
        raise ValueError(
            "Fusion best-of-n is not enabled — turn on the "
            "'fusion.best_of_n_enabled' runtime setting to use it"
        )
    if mode == "synthesize" and not synthesis_enabled:
        raise ValueError(
            "Fusion synthesis is not enabled — turn on the "
            "'fusion.synthesis_enabled' runtime setting to use it"
        )

    candidates = config.get("candidates")
    if not isinstance(candidates, dict):
        raise ValueError("fusion_config.candidates must be an object")
    unknown_candidate_keys = set(candidates) - {"models", "count"}
    if unknown_candidate_keys:
        raise ValueError(
            "fusion_config.candidates has unknown key(s): "
            + ", ".join(sorted(unknown_candidate_keys))
        )
    models = candidates.get("models")
    count = candidates.get("count")
    if (models is None) == (count is None):
        raise ValueError(
            "fusion_config.candidates must specify exactly one of 'models' "
            "(chat model ids) or 'count'"
        )
    if models is not None:
        if not isinstance(models, list) or len(models) < 2:
            raise ValueError(
                "fusion_config.candidates.models must be a list of at least "
                "2 chat model ids"
            )
        if len(models) > max_candidates:
            raise ValueError(
                f"fusion_config.candidates.models lists {len(models)} models; "
                f"at most {max_candidates} are allowed (the "
                f"'fusion.max_candidates' setting)"
            )
        for candidate_model in models:
            if not isinstance(candidate_model, str) or not candidate_model.strip():
                raise ValueError(
                    "fusion_config.candidates.models entries must be "
                    "non-empty model ids"
                )
        if len(set(models)) != len(models):
            raise ValueError(
                "fusion_config.candidates.models must not repeat a model"
            )
    else:
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 2 <= count <= max_candidates
        ):
            raise ValueError(
                f"fusion_config.candidates.count must be an integer between "
                f"2 and {max_candidates} (the 'fusion.max_candidates' setting)"
            )

    arbiter = config.get("arbiter")
    if mode == "best_of_n" and not isinstance(arbiter, dict):
        raise ValueError(
            "fusion mode 'best_of_n' requires an 'arbiter' object carrying "
            "the decision questions used to score the candidates"
        )
    if arbiter is not None:
        if not isinstance(arbiter, dict):
            raise ValueError("fusion_config.arbiter must be an object")
        arbiter_model = arbiter.get("model")
        if arbiter_model is not None and (
            not isinstance(arbiter_model, str) or not arbiter_model.strip()
        ):
            raise ValueError(
                "fusion_config.arbiter.model must be a non-empty decision "
                "model id"
            )
        arbiter_questions = arbiter.get("questions")
        if arbiter_questions is None:
            raise ValueError(
                "fusion_config.arbiter requires a 'questions' list — the "
                "questions the decision model answers to score each "
                "candidate"
            )
        question_types = validate_decision_questions(
            arbiter_questions, label="fusion arbiter"
        )
        if "threshold_policy" in arbiter:
            _validate_decision_threshold_policy(
                arbiter["threshold_policy"], question_types
            )

    synthesis = config.get("synthesis")
    if mode == "synthesize":
        if not isinstance(synthesis, dict):
            raise ValueError(
                "fusion mode 'synthesize' requires a 'synthesis' object"
            )
        synthesis_model = synthesis.get("model")
        if synthesis_model is not None and (
            not isinstance(synthesis_model, str) or not synthesis_model.strip()
        ):
            raise ValueError(
                "fusion_config.synthesis.model must be a non-empty model id"
            )
    elif "synthesis" in config and synthesis is not None:
        raise ValueError(
            "fusion_config.synthesis is only allowed with mode 'synthesize'"
        )

    refine = config.get("refine")
    if "refine" in config:
        if mode != "best_of_n":
            raise ValueError(
                "fusion_config.refine is only allowed with mode 'best_of_n'"
            )
        if refine is not None and not isinstance(refine, dict):
            raise ValueError("fusion_config.refine must be an object")
        passes = refine.get("max_refine_passes") if isinstance(refine, dict) else None
        if passes is None:
            passes = 1
        if (
            not isinstance(passes, int)
            or isinstance(passes, bool)
            or passes not in (1, 2)
        ):
            raise ValueError(
                "fusion_config.refine.max_refine_passes must be 1 or 2"
            )
        refine_model = refine.get("model") if isinstance(refine, dict) else None
        if refine_model is not None and (
            not isinstance(refine_model, str) or not refine_model.strip()
        ):
            raise ValueError(
                "fusion_config.refine.model must be a non-empty model id"
            )

    unknown = set(config) - {
        "mode",
        "candidates",
        "arbiter",
        "synthesis",
        "refine",
    }
    if unknown:
        raise ValueError(
            "fusion_config has unknown key(s): "
            + ", ".join(sorted(unknown))
        )

    return None


# The tasks a dispatcher is actually willing to claim: pending or planned, on
# an approved and non-terminal request, and past the sequence_order stage gate.
#
# Shared deliberately. The daemon's on-demand work discovery must ask the same
# question the dispatcher asks — counting raw `pending` rows would spawn workers
# for work that can never be claimed (a cancelled request's leftovers, or a
# stage-gated task), which then idle out and get respawned forever.
DISPATCHABLE_TASKS_FROM = "FROM tasks t JOIN requests r ON t.request_id = r.id"

DISPATCHABLE_TASK_PREDICATE = """t.organization_id = $1
                     AND t.status IN ('pending', 'planned')
                     AND t.remote_target_id IS NULL
                     AND t.cancel_requested_at IS NULL
                     AND r.approval_status IN ('auto_approved', 'approved')
                     AND r.status NOT IN ('cancelled', 'completed', 'failed')
                                         AND NOT EXISTS (
                                             SELECT 1 FROM tasks unfinished
                                             WHERE unfinished.request_id = t.request_id
                                                 AND unfinished.sequence_order < t.sequence_order
                                                 AND unfinished.status NOT IN (
                                                     'completed', 'failed', 'cancelled'
                                                 )
                                         )
                     AND NOT EXISTS (
                       SELECT 1 FROM (
                           SELECT DISTINCT sequence_order AS seq
                           FROM tasks
                           WHERE request_id = t.request_id
                             AND sequence_order < t.sequence_order
                       ) earlier_seqs
                       WHERE NOT EXISTS (
                           SELECT 1 FROM tasks t2
                           WHERE t2.request_id = t.request_id
                             AND t2.sequence_order = earlier_seqs.seq
                             AND CASE COALESCE(r.dependency_policy, 'strict')
                                 WHEN 'permissive'
                                   THEN t2.status IN ('completed', 'failed', 'cancelled')
                                 ELSE t2.status = 'completed'
                                 END
                       )
                     )"""


# Remote claims ask the same dispatch questions as local dispatch, with two
# deliberate differences: the task must be pinned to *this* remote
# (``$2``), and the "not pinned elsewhere" clause inverts. Keep this in sync
# with DISPATCHABLE_TASK_PREDICATE — a change to one is a change to the rules
# of "can this task run yet" for both.
REMOTE_DISPATCHABLE_TASK_PREDICATE = """t.organization_id = $1
                     AND t.remote_target_id = $2
                     AND t.status IN ('pending', 'planned')
                     AND t.cancel_requested_at IS NULL
                     AND r.approval_status IN ('auto_approved', 'approved')
                     AND r.status NOT IN ('cancelled', 'completed', 'failed')
                     AND NOT EXISTS (
                         SELECT 1 FROM tasks unfinished
                         WHERE unfinished.request_id = t.request_id
                             AND unfinished.sequence_order < t.sequence_order
                             AND unfinished.status NOT IN (
                                 'completed', 'failed', 'cancelled'
                             )
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM (
                           SELECT DISTINCT sequence_order AS seq
                           FROM tasks
                           WHERE request_id = t.request_id
                             AND sequence_order < t.sequence_order
                       ) earlier_seqs
                       WHERE NOT EXISTS (
                           SELECT 1 FROM tasks t2
                           WHERE t2.request_id = t.request_id
                             AND t2.sequence_order = earlier_seqs.seq
                             AND CASE COALESCE(r.dependency_policy, 'strict')
                                 WHEN 'permissive'
                                   THEN t2.status IN ('completed', 'failed', 'cancelled')
                                 ELSE t2.status = 'completed'
                                 END
                       )
                     )"""


async def _script_entitlement_error(
    pool, org_id: str, resolved_kind: str
) -> str | None:
    """Authoring-time ``custom_sandboxes`` gate for the ``script`` task kind.

    A script runs a program inside a sandbox, so its owning organization must
    hold the same capability a sandboxed agent task does. The MCP and API
    authoring paths already gate a payload that carries a sandbox template;
    this covers the creation paths that reach the repository directly — most
    importantly workflow/schedule action expansion, which materializes a
    stored action without going through either authoring gate.

    The shared gate helper is used deliberately: there is one answer to "is
    this organization entitled", and this is not a second one. Its missing-row
    default-deny contract applies unchanged. ``ValueError`` is the
    repository's validation channel, and every creation caller already maps
    it to its own error envelope, so the refusal keeps the capability in the
    message instead of becoming a generic authorization failure.
    """
    if resolved_kind != "script":
        return None
    from lucent.services.entitlements import (
        CAPABILITY_SANDBOXES,
        capability_gate_message,
    )

    return await capability_gate_message(
        org_id, CAPABILITY_SANDBOXES, pool=pool
    )


def _normalize_task_payload(
    *,
    agent_type: str | None,
    output_contract: dict | None,
    decision_config: dict | None,
    fusion_config: dict | None,
    task_kind: str | None,
    execution_config: dict | None,
    sandbox_config: dict | None,
    sandbox_template_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Validate one task payload and normalize it for insertion.

    Shared by :meth:`RequestRepository.create_task` and
    :meth:`RequestRepository.create_request_with_tasks` so the two authoring
    paths can never accept different shapes.

    Returns ``(resolved_kind, {"decision_config": ..., "fusion_config": ...,
    "execution_config": ...})`` with the generic kind/config mirrored onto the
    legacy columns every existing reader still uses.
    """
    from lucent.task_kinds import resolve_task_kind, validate_task_kind_payload

    resolved_kind = resolve_task_kind(
        kind=task_kind,
        agent_type=agent_type,
        decision_config=decision_config,
        fusion_config=fusion_config,
    )
    if execution_config is not None and (
        decision_config is not None or fusion_config is not None
    ):
        raise ValueError(
            "Provide execution_config or the legacy decision_config/"
            "fusion_config, not both"
        )
    if execution_config is None:
        if resolved_kind == "decision":
            execution_config = decision_config
        elif resolved_kind == "fusion":
            execution_config = fusion_config
    if resolved_kind == "decision":
        decision_config = execution_config
    elif resolved_kind == "fusion":
        fusion_config = execution_config
    elif resolved_kind == "script" and (
        decision_config is not None or fusion_config is not None
    ):
        raise ValueError(
            "A script task cannot carry decision_config/fusion_config — it runs "
            "a program directly"
        )
    elif resolved_kind == "agent" and (
        decision_config is not None or fusion_config is not None
    ):
        raise ValueError(
            "decision_config/fusion_config require their matching task kind"
        )
    validate_task_kind_payload(resolved_kind, execution_config)
    if resolved_kind == "script":
        from lucent.script_tasks import validate_script_task

        validate_script_task(
            config=execution_config,
            sandbox_template_id=sandbox_template_id,
            sandbox_config=sandbox_config,
            inline_sandbox_config=(
                sandbox_config is not None and sandbox_template_id is None
            ),
        )

    _validate_output_contract(output_contract)
    validate_decision_config(decision_config)
    # Structural validation only — authoring surfaces enforce the
    # fusion.* mode flags before this call.
    validate_fusion_config(fusion_config)
    if output_contract and decision_config:
        raise ValueError(
            "A decision-step task carries its own question schema — "
            "output_contract cannot be combined with decision_config"
        )
    if decision_config and agent_type != "decision-step":
        raise ValueError("decision_config requires agent_type 'decision-step'")
    if agent_type == "decision-step" and not decision_config:
        raise ValueError(
            "agent_type 'decision-step' requires decision_config "
            "carrying the questions to answer"
        )
    # Unlike decision_config, output_contract IS allowed with
    # fusion_config: the winner / synthesis result still goes through
    # the normal output-contract validation and repair tail.
    if fusion_config and decision_config:
        raise ValueError(
            "A task cannot carry both fusion_config and decision_config "
            "— they configure different executor kinds"
        )
    if fusion_config and agent_type == "decision-step":
        raise ValueError(
            "fusion_config requires a chat agent — decision-step tasks "
            "are executed by the decision model directly"
        )
    from lucent.secrets.utils import validate_sandbox_config_references

    validate_sandbox_config_references(sandbox_config)

    return resolved_kind, {
        "decision_config": decision_config,
        "fusion_config": fusion_config,
        "execution_config": execution_config,
    }


async def _insert_task_row(
    conn,
    *,
    request_id: str,
    org_id: str,
    title: str,
    description: str | None,
    agent_type: str | None,
    agent_definition_id: str | None,
    parent_task_id: str | None,
    priority: str,
    sequence_order: int,
    model: str | None,
    reasoning_effort: str | None,
    sandbox_template_id: str | None,
    sandbox_config: dict | None,
    requesting_user_id: str | None,
    output_contract: dict | None,
    decision_config: dict | None,
    fusion_config: dict | None,
    backend_id: str | None,
    task_kind: str,
    execution_config: dict | None,
    remote_target_id: str | None,
):
    """Insert one task row on an existing connection.

    A request-level execution pin is authoritative: every normal task uses the
    request's ``requested_model``/``backend_id`` even when the caller supplied a
    different default. Decision steps run on a decision-class model resolved by
    the dispatcher and must not inherit either pin.
    """
    return await conn.fetchrow(
        """INSERT INTO tasks
           (request_id, parent_task_id, title, description, agent_type,
            agent_definition_id, priority, sequence_order, organization_id,
            model, reasoning_effort, sandbox_template_id, sandbox_config,
            requesting_user_id, output_contract, decision_config,
            fusion_config, backend_id, task_kind, execution_config,
            remote_target_id)
           VALUES (
            $1, $2, $3, $4, $5::varchar, $6, $7, $8, $9,
            CASE WHEN $5::text = 'decision-step' THEN $10
                 ELSE COALESCE(
                     (SELECT requested_model FROM requests
                      WHERE id = $1 AND organization_id = $9),
                     $10
                 ) END,
            $11, $12, $13,
            COALESCE($14, (SELECT created_by FROM requests
                           WHERE id = $1 AND organization_id = $9)),
            $15, $16, $17,
            CASE WHEN $5::text = 'decision-step' THEN $18
                 ELSE COALESCE(
                     (SELECT backend_id FROM requests
                      WHERE id = $1 AND organization_id = $9),
                     $18
                 ) END,
            $19::varchar,
            $20,
            $21
           )
           RETURNING *""",
        UUID(request_id),
        UUID(parent_task_id) if parent_task_id else None,
        title,
        description,
        agent_type,
        UUID(agent_definition_id) if agent_definition_id else None,
        priority,
        sequence_order,
        UUID(org_id),
        model,
        reasoning_effort,
        UUID(sandbox_template_id) if sandbox_template_id else None,
        (
            json.dumps(sandbox_config)
            if isinstance(sandbox_config, dict)
            else sandbox_config
            if isinstance(sandbox_config, str) and sandbox_config
            else None
        ),
        UUID(requesting_user_id) if requesting_user_id else None,
        json.dumps(output_contract) if output_contract else None,
        json.dumps(decision_config) if decision_config else None,
        json.dumps(fusion_config) if fusion_config else None,
        UUID(backend_id) if backend_id else None,
        task_kind,
        json.dumps(execution_config) if execution_config else None,
        UUID(remote_target_id) if remote_target_id else None,
    )


class RequestRepository:
    """Manages requests, tasks, events, and memory links."""

    @staticmethod
    def _request_visibility_condition(
        requester_param: str,
        include_system_param: str,
        request_alias: str = "r",
    ) -> str:
        return f"""(
            {request_alias}.created_by = {requester_param}::uuid
            OR (
                {include_system_param}::boolean
                AND (
                    {request_alias}.created_by IS NULL
                    OR EXISTS (
                        SELECT 1 FROM users request_creator
                        WHERE request_creator.id = {request_alias}.created_by
                          AND (
                              request_creator.role = 'daemon'
                              OR request_creator.external_id LIKE 'daemon-service:%'
                          )
                    )
                )
            )
        )"""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def get_active_goal_linked_memory_ids(
        self,
        memory_ids: list[UUID],
        *,
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
        statuses: list[str],
    ) -> set[UUID]:
        """Return memories currently linked to active request work as goals."""
        if not memory_ids:
            return set()

        placeholders = ", ".join(f"${idx + 1}" for idx in range(len(memory_ids)))
        status_param = len(memory_ids) + 1
        query = f"""
            SELECT DISTINCT rm.memory_id
            FROM request_memories rm
            JOIN requests r ON r.id = rm.request_id
            WHERE rm.memory_id IN ({placeholders})
              AND rm.relation = 'goal'
              AND r.status = ANY(${status_param})
        """
        async with scoped_acquire_on(
            self.pool, organization_id=organization_id, user_id=user_id
        ) as conn:
            rows = await conn.fetch(query, *memory_ids, statuses)
        return {
            row["memory_id"] if isinstance(row["memory_id"], UUID) else UUID(row["memory_id"])
            for row in rows
        }

    # ── Daemon Instance Registry ───────────────────────────────────────────

    async def register_instance(
        self,
        *,
        org_id: str,
        instance_id: str,
        hostname: str | None = None,
        pid: int | None = None,
        roles: list[str] | None = None,
        metadata: dict | None = None,
        status: str = "active",
    ) -> dict:
        """Register or refresh a daemon instance row."""
        now = datetime.now(timezone.utc)
        roles = roles or []
        metadata_json = json.dumps(metadata or {})
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """INSERT INTO daemon_instances
                   (instance_id, organization_id, hostname, pid, roles, status,
                    started_at, last_seen_at, metadata, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5::text[], $6, $7, $7, $8::jsonb, $7, $7)
                   ON CONFLICT (instance_id, organization_id) DO UPDATE
                   SET hostname = EXCLUDED.hostname,
                       pid = EXCLUDED.pid,
                       roles = EXCLUDED.roles,
                       status = EXCLUDED.status,
                       metadata = EXCLUDED.metadata,
                       last_seen_at = EXCLUDED.last_seen_at,
                       updated_at = EXCLUDED.updated_at
                   RETURNING *""",
                instance_id,
                UUID(org_id),
                hostname,
                pid,
                roles,
                status,
                now,
                metadata_json,
            )
        return dict(row)

    async def heartbeat_instance(
        self,
        *,
        org_id: str,
        instance_id: str,
        metadata: dict | None = None,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
        active_task_ids: list[str] | None = None,
    ) -> dict | None:
        """Update daemon last_seen and renew leases on claimed/running tasks.

        ``active_task_ids`` scopes the lease renewal to the tasks the instance
        still considers its own. This matters for recovery, not bookkeeping: an
        instance that finished a task but failed to write the result back has
        *detached* it, and continuing to renew its claim keeps the row looking
        busy forever. The stale-task reaper reads "recent heartbeat" as "work in
        progress", so an orphaned claim that keeps being renewed can never be
        recovered. Passing ``None`` preserves the legacy "renew everything I own"
        behaviour for callers that do not track active sessions.
        """
        now = datetime.now(timezone.utc)
        metadata_json = json.dumps(metadata or {})
        expires_at = now + timedelta(seconds=lease_seconds)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE daemon_instances
                       SET last_seen_at = $3,
                           status = 'active',
                           metadata = CASE
                               WHEN $4::jsonb = '{}'::jsonb THEN metadata
                               ELSE $4::jsonb
                           END,
                           updated_at = $3
                       WHERE organization_id = $1
                         AND instance_id = $2
                       RETURNING *""",
                    UUID(org_id),
                    instance_id,
                    now,
                    metadata_json,
                )
                if not row:
                    row = await conn.fetchrow(
                        """INSERT INTO daemon_instances
                           (instance_id, organization_id, roles, status,
                            started_at, last_seen_at, metadata, created_at, updated_at)
                           VALUES ($1, $2, '{}'::text[], 'active',
                                   $3, $3, $4::jsonb, $3, $3)
                           ON CONFLICT (instance_id, organization_id) DO UPDATE
                           SET status = 'active',
                               last_seen_at = EXCLUDED.last_seen_at,
                               metadata = CASE
                                   WHEN EXCLUDED.metadata = '{}'::jsonb
                                   THEN daemon_instances.metadata
                                   ELSE EXCLUDED.metadata
                               END,
                               updated_at = EXCLUDED.updated_at
                           RETURNING *""",
                        instance_id,
                        UUID(org_id),
                        now,
                        metadata_json,
                    )
                await conn.execute(
                    """UPDATE tasks
                       SET last_heartbeat_at = $3,
                           claim_expires_at = $4,
                           updated_at = $3
                       WHERE organization_id = $1
                          AND claimed_by = $2
                          AND status IN ('claimed', 'running')
                          AND ($5::uuid[] IS NULL OR id = ANY($5::uuid[]))""",
                    UUID(org_id),
                    instance_id,
                    now,
                    expires_at,
                    [UUID(t) for t in active_task_ids] if active_task_ids is not None else None,
                )
        return dict(row)

    async def mark_instance_stopped(self, *, org_id: str, instance_id: str) -> dict | None:
        """Mark daemon instance as stopped."""
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """UPDATE daemon_instances
                   SET status = 'stopped',
                       updated_at = $3
                   WHERE organization_id = $1
                     AND instance_id = $2
                   RETURNING *""",
                UUID(org_id),
                instance_id,
                now,
            )
        return dict(row) if row else None

    async def reconcile_stale_instances(
        self,
        *,
        org_id: str | None = None,
        stale_seconds: int = DEFAULT_INSTANCE_STALE_SECONDS,
    ) -> int:
        """Mark long-silent instances ``stopped``; returns how many changed.

        Only a graceful shutdown calls ``mark_instance_stopped``, so an instance
        killed by the container runtime (OOM, ``docker kill``, a reaped worker)
        keeps ``status='active'`` forever. That leaves a graveyard of "active"
        rows whose ``last_seen_at`` is days old, which makes the table actively
        misleading when diagnosing a dispatch problem — you have to know to
        cross-check timestamps to tell a live dispatcher from a dead one.

        Deliberately narrow: this only corrects the reported STATUS. Claim
        recovery does not depend on it — ``release_stale_tasks`` already judges
        liveness by ``last_seen_at`` age rather than trusting this column, so the
        reaper keeps working even if this reconcile never runs.
        """
        params: list[Any] = [max(60, int(stale_seconds))]
        org_clause = ""
        if org_id:
            params.append(UUID(org_id))
            org_clause = f"AND organization_id = ${len(params)}"
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            rows = await conn.fetch(
                f"""UPDATE daemon_instances
                    SET status = 'stopped',
                        updated_at = NOW()
                    WHERE status = 'active'
                      AND last_seen_at < NOW() - make_interval(secs := $1)
                      {org_clause}
                    RETURNING instance_id""",
                *params,
            )
        return len(rows)

    # ── Requests ──────────────────────────────────────────────────────────

    async def create_request(
        self,
        title: str,
        org_id: str,
        description: str | None = None,
        source: str = "user",
        priority: str = "medium",
        created_by: str | None = None,
        dependency_policy: str = "strict",
        memory_ids: list[dict] | None = None,
        target_repo: str | None = None,
        target_paths: list[str] | None = None,
        force_pending_approval: bool = False,
        goal_id: str | None = None,
        goal_milestone_index: int | None = None,
        backend_id: str | None = None,
        requested_model: str | None = None,
        decomposition_mode: str = DECOMPOSITION_MODE_PLANNER,
    ) -> dict:
        if source not in VALID_REQUEST_SOURCES:
            valid_sources = ", ".join(sorted(VALID_REQUEST_SOURCES))
            raise ValueError(
                f"Invalid source '{source}'. Must be one of: {valid_sources}"
            )
        if dependency_policy not in ("strict", "permissive"):
            raise ValueError(
                "Invalid dependency_policy "
                f"'{dependency_policy}'. Must be 'strict' or 'permissive'."
            )
        if decomposition_mode not in VALID_DECOMPOSITION_MODES:
            valid = ", ".join(sorted(VALID_DECOMPOSITION_MODES))
            raise ValueError(
                f"Invalid decomposition_mode '{decomposition_mode}'. "
                f"Must be one of: {valid}."
            )
        if goal_milestone_index is not None and goal_id is None:
            raise ValueError(
                "goal_milestone_index requires goal_id to be set."
            )
        if goal_milestone_index is not None and goal_milestone_index < 1:
            raise ValueError(
                "goal_milestone_index must be 1-based and >= 1."
            )
        approval = (
            APPROVAL_PENDING
            if force_pending_approval or _requires_approval(source, organization_id=org_id)
            else APPROVAL_AUTO
        )
        approved_at = datetime.now(timezone.utc) if approval == APPROVAL_AUTO else None
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row, _created = await self._create_request_in_conn(
                conn,
                title=title,
                org_id=org_id,
                description=description,
                source=source,
                priority=priority,
                created_by=created_by,
                dependency_policy=dependency_policy,
                memory_ids=memory_ids,
                target_repo=target_repo,
                target_paths=target_paths,
                approval=approval,
                approved_at=approved_at,
                goal_id=goal_id,
                goal_milestone_index=goal_milestone_index,
                backend_id=backend_id,
                requested_model=requested_model,
                decomposition_mode=decomposition_mode,
            )
        return row

    async def _create_request_in_conn(
        self,
        conn,
        *,
        title: str,
        org_id: str,
        description: str | None,
        source: str,
        priority: str,
        created_by: str | None,
        dependency_policy: str,
        memory_ids: list[dict] | None,
        target_repo: str | None,
        target_paths: list[str] | None,
        approval: str,
        approved_at: datetime | None,
        goal_id: str | None,
        goal_milestone_index: int | None,
        backend_id: str | None,
        requested_model: str | None,
        decomposition_mode: str,
    ) -> tuple[dict, bool]:
        """Insert one request on an existing connection.

        Split out of :meth:`create_request` so chat-authored task plans can
        create the request and every task inside a single transaction — either
        the whole plan lands or none of it does.

        Returns ``(request_row, created)``. ``created`` is False for a dedup or
        refusal outcome, which carries an existing request (or a ``skipped``
        marker) rather than a freshly inserted row.
        """
        # === STRUCTURED GOAL/MILESTONE VALIDATION ===
        # When the caller passes goal_id (the new structured path),
        # validate the goal and milestone here in one shot. This is the
        # canonical way to advance a goal — no title parsing, no
        # late-link, no inference. If the goal isn't active or the
        # named milestone isn't active (or hasn't reached start_after),
        # refuse with a clear reason.
        if goal_id:
            goal_row = await conn.fetchrow(
                """SELECT id, content, metadata
                   FROM memories
                   WHERE id = $1::uuid AND type = 'goal'
                     AND deleted_at IS NULL""",
                goal_id,
            )
            if goal_row is None:
                return {
                    "id": goal_id,
                    "title": title,
                    "status": "skipped",
                    "reason": "goal_not_found",
                    "detail": (
                        f"goal_id {goal_id} does not refer to an active "
                        "goal memory in this org."
                    ),
                }, False
            refusal = _validate_structured_goal_milestone(
                str(goal_row["id"]),
                _coerce_metadata(goal_row["metadata"]),
                goal_milestone_index,
            )
            if refusal:
                return refusal, False

            # Dedup against the structured fields. If another open
            # request already targets this exact (goal, milestone),
            # return it instead of inserting a duplicate. This is
            # robust against parallel cycles and across descriptions.
            dedup_row = await conn.fetchrow(
                """SELECT * FROM requests
                   WHERE organization_id = $1::uuid
                     AND goal_memory_id = $2::uuid
                     AND goal_milestone_index IS NOT DISTINCT FROM $3
                     AND status NOT IN ('completed', 'cancelled')
                   ORDER BY created_at DESC
                   LIMIT 1""",
                UUID(org_id),
                str(goal_row["id"]),
                goal_milestone_index,
            )
            if dedup_row:
                existing = dict(dedup_row)
                existing["_preexisting"] = True
                return existing, False

        # === LEGACY MEMORY_IDS CONTEXT-LINKING PATH ===
        # memory_ids is the general-purpose context attachment
        # mechanism (technical/experience/etc memories that the
        # request should be aware of). Goals SHOULD be passed via
        # the goal_id structured field instead, but for back-compat
        # we still accept relation='goal' here and run the same
        # checks the structured path runs. Anything that arrives
        # here that the structured path didn't already handle is
        # treated as pure context.
        if memory_ids:
            mem_ids = [UUID(m["id"]) for m in memory_ids]
            if mem_ids:
                # Goal-state guard for memory_ids legacy path. Only
                # checks the goal-level status (completed/abandoned)
                # and the all-milestones-done condition. Per-milestone
                # selection is the structured path's job; if the
                # caller wants to advance a specific milestone they
                # should use goal_id.
                goal_rows = await conn.fetch(
                    """SELECT id, content, metadata
                       FROM memories
                       WHERE id = ANY($1)
                         AND type = 'goal'
                         AND deleted_at IS NULL""",
                    mem_ids,
                )
                for goal_row in goal_rows:
                    # Skip the goal we already validated via goal_id.
                    if goal_id and str(goal_row["id"]) == str(goal_id):
                        continue
                    meta = _coerce_metadata(goal_row["metadata"])
                    if meta is None:
                        continue
                    gstatus = meta.get("status")
                    if gstatus in ("completed", "abandoned"):
                        return {
                            "id": str(goal_row["id"]),
                            "title": title,
                            "status": "skipped",
                            "reason": "goal_completed",
                            "detail": (
                                f"Goal {goal_row['id']} has "
                                f"metadata.status={gstatus!r} and must "
                                "not spawn new work."
                            ),
                        }, False

                # Dedup against any other open request linked to the
                # same memory(ies). 'failed' counts as active because
                # the user/operator may want to retry.
                existing = await conn.fetchrow(
                    """SELECT r.* FROM requests r
                       JOIN request_memories rm ON r.id = rm.request_id
                       WHERE r.organization_id = $1
                         AND rm.memory_id = ANY($2)
                         AND r.status NOT IN ('completed', 'cancelled')
                       ORDER BY r.created_at DESC LIMIT 1""",
                    UUID(org_id),
                    mem_ids,
                )
                if existing:
                    existing_row = dict(existing)
                    existing_row["_preexisting"] = True
                    return existing_row, False

        row = await conn.fetchrow(
            """INSERT INTO requests
               (title, description, source, priority, created_by,
                organization_id, dependency_policy,
                approval_status, approved_at,
                target_repo, target_paths,
                goal_memory_id, goal_milestone_index,
                backend_id, requested_model, decomposition_mode)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                       $12, $13, $14, $15, $16)
               RETURNING *""",
            title,
            description,
            source,
            priority,
            UUID(created_by) if created_by else None,
            UUID(org_id),
            dependency_policy,
            approval,
            approved_at,
            target_repo,
            target_paths,
            UUID(goal_id) if goal_id else None,
            goal_milestone_index,
            UUID(backend_id) if backend_id else None,
            requested_model,
            decomposition_mode,
        )

        # Link memories to the new request
        if memory_ids and row:
            for m in memory_ids:
                try:
                    await conn.execute(
                        """INSERT INTO request_memories (request_id, memory_id, relation)
                           VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                        row["id"],
                        UUID(m["id"]),
                        m.get("relation", "goal"),
                    )
                except Exception:
                    pass  # Best-effort — don't fail request creation on link errors

        # If the request advances a goal via the structured field, also
        # mirror that into request_memories(relation='goal') so the
        # existing UI panel and link-following queries continue to
        # surface the goal alongside the structured fields. Idempotent
        # via ON CONFLICT.
        if goal_id and row:
            try:
                await conn.execute(
                    """INSERT INTO request_memories
                            (request_id, memory_id, relation)
                       VALUES ($1, $2::uuid, 'goal')
                       ON CONFLICT DO NOTHING""",
                    row["id"],
                    goal_id,
                )
            except Exception:
                pass

        return dict(row), True

    async def get_request(
        self,
        request_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict | None:
        access_clause = ""
        params: list[Any] = [UUID(request_id), UUID(org_id)]
        if requester_user_id is not None:
            access_clause = " AND " + self._request_visibility_condition("$3", "$4")
            params.extend([UUID(requester_user_id), include_system])
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "SELECT r.* FROM requests r "
                "WHERE r.id = $1 AND r.organization_id = $2" + access_clause,
                *params,
            )
        return dict(row) if row else None

    async def get_task_summary(
        self,
        task_id: str,
        org_id: str,
    ) -> dict | None:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """SELECT id::text, request_id::text, title, agent_type,
                          model, reasoning_effort
                   FROM tasks
                   WHERE id = $1::uuid AND organization_id = $2::uuid""",
                task_id,
                org_id,
            )
        return dict(row) if row else None

    async def get_latest_task_event(
        self,
        task_id: str,
        event_types: list[str],
    ) -> dict | None:
        async with scoped_acquire_on(
            self.pool,
        ) as conn:
            row = await conn.fetchrow(
                """SELECT metadata
                   FROM task_events
                   WHERE task_id = $1::uuid
                     AND event_type = ANY($2::text[])
                   ORDER BY created_at DESC
                   LIMIT 1""",
                task_id,
                event_types,
            )
        return dict(row) if row else None

    async def mark_request_viewed(
        self,
        request_id: str,
        org_id: str,
        user_id: str,
    ) -> dict | None:
        """Record that a user opened a request detail page."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, user_id=user_id
        ) as conn:
            row = await conn.fetchrow(
                """INSERT INTO request_views (
                       request_id, user_id, organization_id, first_viewed_at, last_viewed_at
                   )
                   SELECT r.id, $3::uuid, r.organization_id, NOW(), NOW()
                   FROM requests r
                   WHERE r.id = $1::uuid AND r.organization_id = $2::uuid
                   ON CONFLICT (request_id, user_id)
                   DO UPDATE SET last_viewed_at = EXCLUDED.last_viewed_at
                   RETURNING *""",
                request_id,
                org_id,
                user_id,
            )
        return dict(row) if row else None

    async def list_requests(
        self,
        org_id: str,
        status: str | None = None,
        source: str | None = None,
        limit: int = 25,
        offset: int = 0,
        exclude_status: str | None = None,
        viewer_user_id: str | None = None,
        requester_user_id: str | None = None,
        include_system: bool = False,
        search: str | None = None,
    ) -> dict:
        base = "FROM requests r WHERE r.organization_id = $1"
        params: list[Any] = [UUID(org_id)]
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            base += " AND " + self._request_visibility_condition(
                f"${len(params) - 1}", f"${len(params)}"
            )
        # Free-text search over the two fields a person actually remembers.
        #
        # Substring (ILIKE) rather than full-text on purpose: request titles are
        # short and people search for fragments ("green car", "poem"), which a
        # tsquery would tokenize away. LIKE metacharacters in the user's input
        # are escaped so a literal "%" cannot silently match every row.
        if search and search.strip():
            escaped = (
                search.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params.append(f"%{escaped}%")
            base += (
                f" AND (r.title ILIKE ${len(params)} ESCAPE '\\'"
                f" OR r.description ILIKE ${len(params)} ESCAPE '\\')"
            )
        if status:
            statuses = [value.strip() for value in status.split(",") if value.strip()]
            if len(statuses) == 1:
                params.append(statuses[0])
                base += f" AND r.status = ${len(params)}"
            elif statuses:
                placeholders = ", ".join(
                    f"${len(params) + index + 1}" for index in range(len(statuses))
                )
                params.extend(statuses)
                base += f" AND r.status IN ({placeholders})"
        elif exclude_status:
            excluded = [s.strip() for s in exclude_status.split(",") if s.strip()]
            if excluded:
                placeholders = ", ".join(f"${len(params) + i + 1}" for i in range(len(excluded)))
                params.extend(excluded)
                base += f" AND r.status NOT IN ({placeholders})"
        if source:
            sources = [s.strip() for s in source.split(",") if s.strip()]
            if len(sources) == 1:
                params.append(sources[0])
                base += f" AND r.source = ${len(params)}"
            else:
                placeholders = ", ".join(f"${len(params) + i + 1}" for i in range(len(sources)))
                params.extend(sources)
                base += f" AND r.source IN ({placeholders})"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query_params = list(params)
        if viewer_user_id:
            query_params.append(UUID(viewer_user_id))
            view_join = (
                "LEFT JOIN request_views rv "
                f"ON rv.request_id = r.id AND rv.user_id = ${len(query_params)}"
            )
            last_viewed_expr = "rv.last_viewed_at"
        else:
            view_join = ""
            last_viewed_expr = "NULL::timestamptz"

        query = (
            "SELECT r.*, "
            "COALESCE(task_stats.task_count, 0)::int AS task_count, "
            "COALESCE(task_stats.tasks_completed, 0)::int AS tasks_completed, "
            "COALESCE(task_stats.tasks_running, 0)::int AS tasks_running, "
            "COALESCE(task_stats.tasks_failed, 0)::int AS tasks_failed, "
            "COALESCE(task_stats.models_used, ARRAY[]::text[]) AS models_used, "
            "COALESCE(output_stats.output_count, 0)::int AS output_count, "
            f"{last_viewed_expr} AS last_viewed_at, "
            "(r.status IN ('completed', 'failed') "
            " AND r.completed_at IS NOT NULL "
            f" AND ({last_viewed_expr} IS NULL OR {last_viewed_expr} < r.completed_at)"
            ") AS is_unviewed_completion "
            "FROM requests r "
            "LEFT JOIN LATERAL ("
            "  SELECT COUNT(*) AS task_count, "
            "         COUNT(*) FILTER (WHERE t.status = 'completed') AS tasks_completed, "
            "         COUNT(*) FILTER (WHERE t.status IN ('claimed', 'running')) AS tasks_running, "
            "         COUNT(*) FILTER (WHERE t.status = 'failed') AS tasks_failed, "
            "         COALESCE(ARRAY_AGG(DISTINCT t.model ORDER BY t.model) "
            "                  FILTER (WHERE t.model IS NOT NULL), ARRAY[]::text[]) AS models_used "
            "  FROM tasks t WHERE t.request_id = r.id"
            ") task_stats ON TRUE "
            "LEFT JOIN LATERAL ("
            "  SELECT COUNT(*) AS output_count "
            "  FROM task_outputs o WHERE o.request_id = r.id"
            ") output_stats ON TRUE "
            f"{view_join} "
            f"WHERE r.organization_id = $1"
        )
        # Re-apply dynamic filters after the joins so the list can use summary
        # fields in its ordering while the count query stays compact.
        where_suffix = base.removeprefix("FROM requests r WHERE r.organization_id = $1")
        query += where_suffix
        query += (
            " ORDER BY "
            "CASE "
            "  WHEN r.approval_status = 'pending_approval' "
            "       AND r.status NOT IN ('cancelled', 'rejection_processing') THEN 0 "
            "  WHEN COALESCE(task_stats.tasks_running, 0) > 0 THEN 1 "
            "  ELSE 2 "
            "END, "
            "CASE "
            "  WHEN (r.approval_status = 'pending_approval' "
            "        AND r.status NOT IN ('cancelled', 'rejection_processing')) "
            "       OR COALESCE(task_stats.tasks_running, 0) > 0 "
            "  THEN CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "                       WHEN 'medium' THEN 2 ELSE 3 END "
            "  ELSE 0 "
            "END, "
            "COALESCE(r.updated_at, r.created_at) DESC, r.created_at DESC "
            f"LIMIT ${len(query_params) + 1} OFFSET ${len(query_params) + 2}"
        )
        params_with_page = [*query_params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def update_request_decomposition_mode(
        self,
        request_id: str,
        decomposition_mode: str,
        org_id: str | None = None,
    ) -> dict | None:
        """Change who owns decomposition of a request.

        Switching to ``planner`` while tasks already exist is refused — the
        composer would author a second breakdown on top of them. ``author`` is
        always allowed: taking ownership away from the composer is safe.
        """
        if decomposition_mode not in VALID_DECOMPOSITION_MODES:
            valid = ", ".join(sorted(VALID_DECOMPOSITION_MODES))
            raise ValueError(
                f"Invalid decomposition_mode '{decomposition_mode}'. "
                f"Must be one of: {valid}."
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            org_clause = " AND organization_id = $3" if org_id else ""
            params: list[Any] = [UUID(request_id), decomposition_mode]
            if org_id:
                params.append(UUID(org_id))
            if decomposition_mode == DECOMPOSITION_MODE_PLANNER:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM tasks WHERE request_id = $1"
                    + (" AND organization_id = $2" if org_id else ""),
                    *([UUID(request_id), UUID(org_id)] if org_id else [UUID(request_id)]),
                )
                if existing:
                    raise ValueError(
                        f"Request already has {existing} task(s); the planner "
                        "would add a second breakdown"
                    )
            row = await conn.fetchrow(
                "UPDATE requests SET decomposition_mode = $2, updated_at = NOW()"
                " WHERE id = $1"
                + org_clause
                + " RETURNING *",
                *params,
            )
        return dict(row) if row else None

    async def _cancel_tasks_for_terminal_request(
        self, conn, *, request_id: str, status: str, now: datetime
    ) -> None:
        """Settle every task of a request that has just reached a terminal state.

        Queued tasks are simply cancelled. In-flight tasks (``claimed``/
        ``running``) are the interesting case: finalizing a request used to
        ignore them entirely, so a request could report "done" while a sibling
        task kept executing — burning model time and mutating state with no
        record that its work had been orphaned.

        For ``completed`` we settle them here, in the same transaction as the
        request transition, so the outcome is atomic and visible: the task
        becomes ``cancelled`` with a ``cancelled`` event, and the owning
        daemon session's later completion write is refused by its own
        ``status IN ('claimed','running')`` guard (a no-op, not a regression).

        ``cancel_requested_at`` is stamped at the same time on purpose. The
        row transition alone would not stop the provider call: the daemon
        learns about it from the ``task_cancel`` notification published by
        migration 144's trigger (and from its periodic durability sweep). The
        stamp is what actually interrupts the session; the status is what
        records the outcome.

        For ``cancelled`` the existing behaviour is preserved unchanged: only
        the signal is stamped, because the owning session (or the reaper, once
        the claim goes stale) is the only party allowed to finalize a cancel.
        """
        queued = await conn.fetch(
            """UPDATE tasks
               SET status = 'cancelled', completed_at = $2, updated_at = $2
               WHERE request_id = $1 AND status IN ('pending', 'planned')
               RETURNING id""",
            UUID(request_id),
            now,
        )
        if queued:
            await conn.executemany(
                """INSERT INTO task_events (task_id, event_type, detail, metadata)
                   VALUES ($1, 'cancelled', $2, $3)""",
                [
                    (
                        task["id"],
                        f"Task cancelled because parent request was {status}",
                        {"request_status": status},
                    )
                    for task in queued
                ],
            )

        if status == REQUEST_STATUS_CANCELLED:
            # Signal only — see the docstring. Do not transition underneath a
            # live claim.
            await conn.execute(
                """UPDATE tasks
                   SET cancel_requested_at = COALESCE(cancel_requested_at, $2),
                       updated_at = $2
                   WHERE request_id = $1
                     AND status IN ('claimed', 'running')
                     AND cancel_requested_at IS NULL""",
                UUID(request_id),
                now,
            )
            return

        if status != REQUEST_STATUS_COMPLETED:
            return

        in_flight = await conn.fetch(
            """UPDATE tasks
               SET status = 'cancelled',
                   completed_at = COALESCE(completed_at, $2),
                   updated_at = $2,
                   cancel_requested_at = COALESCE(cancel_requested_at, $2),
                   claimed_by = NULL,
                   claim_expires_at = NULL
               WHERE request_id = $1
                 AND status IN ('claimed', 'running')
               RETURNING id""",
            UUID(request_id),
            now,
        )
        if in_flight:
            await conn.executemany(
                """INSERT INTO task_events (task_id, event_type, detail, metadata)
                   VALUES ($1, 'cancelled', $2, $3)""",
                [
                    (
                        task["id"],
                        "Task cancelled because parent request was completed "
                        "while this task was still executing",
                        {
                            "request_status": status,
                            "was_in_flight": True,
                        },
                    )
                    for task in in_flight
                ],
            )

    async def update_request_status(
        self,
        request_id: str,
        status: str,
        org_id: str | None = None,
        *,
        expected_current_statuses: tuple[str, ...] | None = None,
    ) -> dict | None:
        """Move a request to ``status``, applying the terminal side effects.

        ``expected_current_statuses`` is an optimistic guard evaluated inside
        the UPDATE's own WHERE clause, which is what makes it race-free: the
        transition only lands when the request is still in one of the named
        states, so a concurrent finalize cannot be overwritten. Automated
        callers pass it (see ``_check_request_completion``) so a late task
        transition can never regress a request that already finished; manual
        callers leave it unset and keep last-write-wins behaviour.
        """
        if status not in VALID_REQUEST_STATUSES:
            valid = ", ".join(sorted(VALID_REQUEST_STATUSES))
            raise ValueError(f"Invalid status '{status}'. Must be one of: {valid}")
        guard: list[str] | None = (
            list(expected_current_statuses) if expected_current_statuses else None
        )
        now = datetime.now(timezone.utc)
        completed_at = (
            now
            if status
            in (REQUEST_STATUS_COMPLETED, REQUEST_STATUS_FAILED, REQUEST_STATUS_CANCELLED)
            else None
        )
        reviewed_at = (
            now if status in (REQUEST_STATUS_REVIEW, REQUEST_STATUS_NEEDS_REWORK) else None
        )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            async with conn.transaction():
                if org_id:
                    params: list[Any] = [
                        UUID(request_id),
                        status,
                        now,
                        completed_at,
                        reviewed_at,
                        UUID(org_id),
                    ]
                    guard_clause = ""
                    if guard is not None:
                        params.append(guard)
                        guard_clause = f" AND status = ANY(${len(params)}::text[])"
                    row = await conn.fetchrow(
                        """UPDATE requests
                           SET status = $2, updated_at = $3,
                               completed_at = COALESCE($4, completed_at),
                               reviewed_at = COALESCE($5, reviewed_at)
                           WHERE id = $1 AND organization_id = $6"""
                        + guard_clause
                        + " RETURNING *",
                        *params,
                    )
                else:
                    params = [UUID(request_id), status, now, completed_at, reviewed_at]
                    guard_clause = ""
                    if guard is not None:
                        params.append(guard)
                        guard_clause = f" AND status = ANY(${len(params)}::text[])"
                    row = await conn.fetchrow(
                        """UPDATE requests
                           SET status = $2, updated_at = $3,
                               completed_at = COALESCE($4, completed_at),
                               reviewed_at = COALESCE($5, reviewed_at)
                           WHERE id = $1"""
                        + guard_clause
                        + " RETURNING *",
                        *params,
                    )

                if row and status in (REQUEST_STATUS_COMPLETED, REQUEST_STATUS_CANCELLED):
                    await self._cancel_tasks_for_terminal_request(
                        conn, request_id=request_id, status=status, now=now
                    )

        # When a request reaches a terminal state, close any linked schedule run.
        # This runs in a separate connection from the request status update above,
        # so there is a small inconsistency window where the request is terminal
        # but the schedule_run remains "running". We intentionally keep this
        # best-effort/non-fatal to avoid rolling back the primary request update.
        # The schedule_run query targets only status='running', making retries safe
        # and preventing terminal schedule_runs from being overwritten.
        if row and status in (
            REQUEST_STATUS_COMPLETED,
            REQUEST_STATUS_FAILED,
            REQUEST_STATUS_CANCELLED,
        ):
            try:
                async with scoped_acquire_on(
                    self.pool, organization_id=org_id
                ) as conn:
                    if status == REQUEST_STATUS_COMPLETED:
                        await conn.execute(
                            """UPDATE schedule_runs
                               SET status = 'completed', completed_at = now()
                               WHERE request_id = $1::uuid AND status = 'running'""",
                            request_id,
                        )
                    else:
                        await conn.execute(
                            """UPDATE schedule_runs
                               SET status = 'failed', completed_at = now(),
                                   error = $2
                               WHERE request_id = $1::uuid AND status = 'running'""",
                            request_id,
                            f"Request {status}",
                        )
            except Exception as e:
                logger.warning(
                    "Failed to close schedule run for request %s: %s",
                    request_id,
                    e,
                )

        # Milestone-completion side effect: when a request that advances a
        # specific goal milestone reaches 'completed', mark that milestone
        # 'completed' on the goal memory in the same transaction-ish window.
        # This closes the loop so the planner's next cycle no longer sees
        # the milestone as plannable. Failures are non-fatal — the
        # primary status transition has already committed.
        if (
            row
            and status == REQUEST_STATUS_COMPLETED
            and row.get("goal_memory_id")
            and row.get("goal_milestone_index")
        ):
            try:
                await self._mark_milestone_completed(
                    str(row["goal_memory_id"]),
                    int(row["goal_milestone_index"]),
                    org_id=org_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to mark milestone %s of goal %s completed for "
                    "request %s: %s",
                    row.get("goal_milestone_index"),
                    row.get("goal_memory_id"),
                    request_id,
                    e,
                )

        return dict(row) if row else None

    async def _mark_milestone_completed(
        self,
        goal_memory_id: str,
        milestone_index: int,
        org_id: str | None = None,
    ) -> None:
        """Set metadata.milestones[i-1].status = 'completed' on a goal.

        Uses jsonb_set so unrelated milestone fields are preserved. If the
        milestone is already non-active (someone marked it abandoned, etc.)
        we leave it alone — only flip 'active' → 'completed'.

        Also bumps the goal's metadata.status to 'completed' if EVERY
        milestone is now completed/abandoned, since otherwise the next
        planning cycle would still consider the goal active.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT metadata FROM memories
                       WHERE id = $1::uuid AND type = 'goal'
                         AND deleted_at IS NULL
                       FOR UPDATE""",
                    goal_memory_id,
                )
                if not row:
                    return
                metadata = _coerce_metadata(row["metadata"]) or {}
                milestones = metadata.get("milestones")
                if not isinstance(milestones, list):
                    return
                if milestone_index < 1 or milestone_index > len(milestones):
                    return
                m = milestones[milestone_index - 1]
                if not isinstance(m, dict):
                    return

                if m.get("status") != "active":
                    # Don't overwrite abandoned/completed/etc.
                    return

                m["status"] = "completed"
                # Stamp completion date so analytics / UI can show when
                # the milestone wrapped up.
                m["completed_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                metadata["milestones"] = milestones

                # If every milestone is now non-active, the goal as a whole
                # is done — flip metadata.status so the planner stops
                # considering it.
                if all(
                    isinstance(x, dict) and x.get("status") in ("completed", "abandoned")
                    for x in milestones
                ):
                    metadata["status"] = "completed"

                await conn.execute(
                    """UPDATE memories
                       SET metadata = $2::jsonb,
                           updated_at = NOW(),
                           version = version + 1
                       WHERE id = $1::uuid""",
                    goal_memory_id,
                    metadata,
                )

    async def link_request_memory(
        self,
        request_id: str,
        memory_id: str,
        relation: str = "goal",
        org_id: str | None = None,
        block_duplicates: bool = True,
    ) -> dict | None:
        """Link a memory to a request.

        Returns one of:
            - the inserted link row dict on success
            - ``None`` if the request doesn't exist (or doesn't belong to org)
            - a dict with ``{"duplicate_of": <existing_request_id>, ...}`` when
              ``block_duplicates`` is True (default), ``relation == 'goal'``,
              and another active or recently-completed request is already
              linked to the same memory. The link is NOT inserted in this case.
            - ``None`` if the link already existed (insert was a no-op)

        The duplicate check is the structural backstop for the
        create-request-then-link-later pattern: callers (REST API,
        daemon's RequestAPI HTTP client, MCP tools called without
        ``goal_id``) all create requests without dedup running, then add
        memory links afterward. Without dedup here, those paths can produce
        duplicate goal-linked requests every cognitive cycle.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            async with conn.transaction():
                # Serialize concurrent linkers for the same memory so two
                # parallel callers can't both pass the dedup check before
                # either commits.
                if block_duplicates and relation == "goal":
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock($1, hashtext($2)::int)",
                        MEMORY_LINK_LOCK_NAMESPACE,
                        str(memory_id),
                    )

                # Verify the request exists and belongs to the org.
                req_row = await conn.fetchrow(
                    "SELECT id, title, source, organization_id FROM requests "
                    "WHERE id = $1::uuid"
                    + (" AND organization_id = $2::uuid" if org_id else ""),
                    *((request_id, UUID(org_id)) if org_id else (request_id,)),
                )
                if not req_row:
                    return None
                req_org_id = req_row["organization_id"]

                memory_exists = await conn.fetchval(
                    """SELECT 1 FROM memories
                       WHERE id = $1::uuid
                         AND organization_id = $2
                         AND deleted_at IS NULL""",
                    memory_id,
                    req_org_id,
                )
                if not memory_exists:
                    return None

                if block_duplicates and relation == "goal":
                    # NOTE: late-link goal-state validation has been removed.
                    # Goals are now expected to be passed via the structured
                    # goal_memory_id / goal_milestone_index fields at
                    # create_request time, where validation is atomic. The
                    # link_request_memory path remains supported for
                    # back-compat (and for non-goal context attachments)
                    # but no longer second-guesses the goal's lifecycle.
                    #
                    # The dedup checks below DO still run because two
                    # different requests linking the same goal at roughly
                    # the same time is still a duplicate-work signal worth
                    # catching, regardless of the goal's metadata state.

                    # 1) Any OTHER active goal-linked request for this memory?
                    active_dup = await conn.fetchrow(
                        """SELECT r.id, r.title, r.status
                           FROM requests r
                           JOIN request_memories rm ON rm.request_id = r.id
                           WHERE rm.memory_id = $1::uuid
                             AND r.organization_id = $2
                             AND r.id != $3::uuid
                             AND rm.relation = 'goal'
                             AND r.status NOT IN ('completed', 'cancelled')
                           ORDER BY r.created_at
                           LIMIT 1""",
                        memory_id,
                        req_org_id,
                        request_id,
                    )
                    if active_dup:
                        return {
                            "duplicate_of": str(active_dup["id"]),
                            "existing_title": active_dup["title"],
                            "existing_status": active_dup["status"],
                            "reason": "active_request_for_goal",
                        }

                    # 2) Any RECENTLY completed/cancelled request for this
                    # memory whose normalized title matches the linking
                    # request's title? Mirrors the create_request guard so
                    # the late-link path is just as protected.
                    normalized = _normalize_title_for_dedup(req_row["title"])
                    if normalized:
                        recent_dup = await conn.fetchrow(
                            """SELECT r.id, r.title, r.status, r.completed_at
                               FROM requests r
                               JOIN request_memories rm ON rm.request_id = r.id
                               WHERE rm.memory_id = $1::uuid
                                 AND r.organization_id = $2
                                 AND r.id != $3::uuid
                                 AND rm.relation = 'goal'
                                 AND r.status IN ('completed', 'cancelled')
                                 AND COALESCE(r.completed_at, r.updated_at)
                                     > NOW() - make_interval(hours => $4)
                                 AND regexp_replace(
                                         lower(r.title),
                                         '[^a-z0-9]+', ' ', 'g'
                                     ) = $5
                               ORDER BY COALESCE(r.completed_at, r.updated_at) DESC
                               LIMIT 1""",
                            memory_id,
                            req_org_id,
                            request_id,
                            RECENT_COMPLETION_WINDOW_HOURS,
                            normalized,
                        )
                        if recent_dup:
                            return {
                                "duplicate_of": str(recent_dup["id"]),
                                "existing_title": recent_dup["title"],
                                "existing_status": recent_dup["status"],
                                "reason": "duplicate_of_recent_completion",
                            }

                row = await conn.fetchrow(
                    """INSERT INTO request_memories (request_id, memory_id, relation)
                       VALUES ($1::uuid, $2::uuid, $3)
                       ON CONFLICT DO NOTHING
                       RETURNING *""",
                    request_id,
                    memory_id,
                    relation,
                )
        return dict(row) if row else None

    async def approve_request(
        self,
        request_id: str,
        org_id: str,
        approved_by: str,
        comment: str | None = None,
    ) -> dict | None:
        """Approve a pending_approval request so work can begin."""
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """UPDATE requests
                   SET approval_status = 'approved',
                       approved_by = $3::uuid,
                       approved_at = $4,
                       approval_comment = $5,
                       updated_at = $4
                   WHERE id = $1::uuid
                     AND organization_id = $2::uuid
                     AND approval_status = 'pending_approval'
                   RETURNING *""",
                request_id,
                org_id,
                approved_by,
                now,
                comment,
            )
        return dict(row) if row else None

    async def reject_request(
        self,
        request_id: str,
        org_id: str,
        rejected_by: str,
        comment: str,
    ) -> dict | None:
        """Reject a pending_approval request.

        Rejected requests enter rejection_processing for the daemon feedback loop.
        """
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """UPDATE requests
                   SET approval_status = 'rejected',
                       approved_by = $3::uuid,
                       approved_at = $4,
                       approval_comment = $5,
                       status = 'rejection_processing',
                       updated_at = $4
                   WHERE id = $1::uuid
                     AND organization_id = $2::uuid
                     AND approval_status = 'pending_approval'
                   RETURNING *""",
                request_id,
                org_id,
                rejected_by,
                now,
                comment,
            )
        return dict(row) if row else None

    async def list_pending_approvals(
        self, org_id: str, limit: int = 25, offset: int = 0
    ) -> dict:
        """List requests awaiting human approval."""
        base = """FROM requests
                   WHERE organization_id = $1
                     AND approval_status = 'pending_approval'"""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                UUID(org_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT * {base}
                   ORDER BY
                     CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'medium' THEN 2 ELSE 3 END,
                     created_at
                   LIMIT $2 OFFSET $3""",
                UUID(org_id),
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

    async def get_requests_in_review(
        self,
        org_id: str,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict:
        """List requests currently awaiting or undergoing review."""
        base = """FROM requests r
                   WHERE r.organization_id = $1
                     AND r.status IN ('review', 'needs_rework')"""
        params: list[Any] = [UUID(org_id)]
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            base += " AND " + self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT r.* {base}
                    ORDER BY
                      CASE r.status WHEN 'review' THEN 0 ELSE 1 END,
                      r.updated_at DESC
                    LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
                *params,
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

    async def get_request_with_tasks(
        self,
        request_id: str,
        org_id: str,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict | None:
        """Load a request with its full task tree, events, memory links, and reviews."""
        req = await self.get_request(
            request_id,
            org_id,
            requester_user_id=requester_user_id,
            include_system=include_system,
        )
        if not req:
            return None
        req["tasks"] = (await self.list_tasks(request_id, org_id=org_id))["items"]

        # asyncpg returns jsonb columns as strings unless a codec is registered
        # at the connection level. Parse the columns the UI cares about so
        # templates can index into them as dicts/lists.
        import json as _json

        def _parse_jsonb(row: dict, *cols: str) -> None:
            for col in cols:
                val = row.get(col)
                if isinstance(val, str):
                    try:
                        row[col] = _json.loads(val)
                    except (ValueError, TypeError):
                        # Leave the original string if it isn't valid JSON.
                        pass

        for task in req["tasks"]:
            _parse_jsonb(
                task,
                "sandbox_config",
                "output_contract",
                "result_structured",
                "validation_errors",
                # The detail page describes how a task ran, not just its
                # output: a fusion task's candidate list and arbiter come from
                # these, and an unparsed JSON string renders as one blob.
                "fusion_config",
                "execution_config",
            )

        # Batch-load events, memory links, and outputs for ALL tasks (avoids N+1)
        task_ids = [task["id"] for task in req["tasks"]]
        if task_ids:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                event_rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = ANY($1) ORDER BY created_at",
                    task_ids,
                )
                memory_rows = await conn.fetch(
                    """SELECT tm.*, m.content, m.type as memory_type, m.tags
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       WHERE tm.task_id = ANY($1)
                           AND m.organization_id = $2
                           AND m.deleted_at IS NULL
                       ORDER BY tm.created_at""",
                    task_ids,
                    UUID(org_id),
                )
                output_rows = await conn.fetch(
                    """SELECT * FROM task_outputs
                       WHERE task_id = ANY($1)
                         AND organization_id = $2
                       ORDER BY is_primary DESC, created_at DESC""",
                    task_ids,
                    UUID(org_id),
                )

            # Group by task_id
            events_by_task: dict[str, list[dict]] = {}
            for row in event_rows:
                tid = str(row["task_id"])
                event = dict(row)
                _parse_jsonb(event, "metadata")
                events_by_task.setdefault(tid, []).append(event)

            memories_by_task: dict[str, list[dict]] = {}
            for row in memory_rows:
                tid = str(row["task_id"])
                memories_by_task.setdefault(tid, []).append(dict(row))

            outputs_by_task: dict[str, list[dict]] = {}
            request_outputs: list[dict] = []
            for row in output_rows:
                tid = str(row["task_id"])
                output = self._row_to_output_dict(row)
                outputs_by_task.setdefault(tid, []).append(output)
                request_outputs.append(output)

            for task in req["tasks"]:
                tid = str(task["id"])
                task["events"] = events_by_task.get(tid, [])
                task["memories"] = memories_by_task.get(tid, [])
                task["outputs"] = outputs_by_task.get(tid, [])
            req["outputs"] = request_outputs
        else:
            for task in req["tasks"]:
                task["events"] = []
                task["memories"] = []
                task["outputs"] = []
            req["outputs"] = []

        # Load reviews for this request (batch, no N+1)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            review_rows = await conn.fetch(
                """SELECT * FROM reviews
                   WHERE request_id = $1 AND organization_id = $2
                   ORDER BY created_at DESC""",
                UUID(request_id),
                UUID(org_id),
            )
        req["reviews"] = [dict(r) for r in review_rows]

        # Load request-level memory links
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            mem_rows = await conn.fetch(
                """SELECT rm.memory_id, rm.relation, rm.created_at,
                          m.content, m.type AS memory_type, m.tags,
                          m.metadata
                   FROM request_memories rm
                   JOIN memories m ON rm.memory_id = m.id
                   WHERE rm.request_id = $1
                              AND m.organization_id = $2
                              AND m.deleted_at IS NULL
                   ORDER BY rm.created_at""",
                UUID(request_id),
                     UUID(org_id),
            )
        req["memories"] = [dict(r) for r in mem_rows]

        # Build task tree (nest sub-tasks under parents)
        task_map = {str(t["id"]): t for t in req["tasks"]}
        root_tasks = []
        for t in req["tasks"]:
            t["sub_tasks"] = []
        for t in req["tasks"]:
            parent_id = str(t["parent_task_id"]) if t.get("parent_task_id") else None
            if parent_id and parent_id in task_map:
                task_map[parent_id]["sub_tasks"].append(t)
            else:
                root_tasks.append(t)
        req["task_tree"] = root_tasks

        # Compute summary stats
        statuses = [t["status"] for t in req["tasks"]]
        req["stats"] = {
            "total": len(statuses),
            "pending": statuses.count("pending") + statuses.count("planned"),
            "running": statuses.count("claimed") + statuses.count("running"),
            "completed": statuses.count("completed"),
            "failed": statuses.count("failed"),
        }
        return req

    def _row_to_output_dict(self, row) -> dict[str, Any]:
        output = dict(row)
        metadata = output.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        output["metadata"] = metadata
        return output

    async def create_task_output(
        self,
        *,
        task_id: str,
        org_id: str,
        output: dict[str, Any],
        created_by: str | None = None,
    ) -> dict:
        """Create a user-facing output artifact for a task."""
        normalized = _normalize_task_output(output)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            task = await conn.fetchrow(
                """SELECT id, request_id, organization_id
                   FROM tasks
                   WHERE id = $1::uuid AND organization_id = $2::uuid""",
                task_id,
                org_id,
            )
            if not task:
                raise ValueError("Task not found")

            row = await conn.fetchrow(
                """INSERT INTO task_outputs (
                       task_id, request_id, organization_id, created_by,
                       output_type, provider, title, description, url,
                       external_id, mime_type, metadata, is_primary
                   ) VALUES (
                       $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                       $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13
                   )
                   RETURNING *""",
                task_id,
                str(task["request_id"]),
                org_id,
                created_by,
                normalized["output_type"],
                normalized["provider"],
                normalized["title"],
                normalized["description"],
                normalized["url"],
                normalized["external_id"],
                normalized["mime_type"],
                json.dumps(normalized["metadata"]),
                normalized["is_primary"],
            )
        created = self._row_to_output_dict(row)
        await self.add_task_event(
            task_id,
            "output_created",
            f"Output recorded: {created['title']}",
            metadata={
                "output_id": str(created["id"]),
                "output_type": created["output_type"],
                "url": created.get("url"),
            },
            org_id=org_id,
        )
        return created

    async def list_request_outputs(self, request_id: str, org_id: str) -> list[dict]:
        """List all outputs for a request, newest/primary first."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                """SELECT * FROM task_outputs
                   WHERE request_id = $1::uuid AND organization_id = $2::uuid
                   ORDER BY is_primary DESC, created_at DESC""",
                request_id,
                org_id,
            )
        return [self._row_to_output_dict(r) for r in rows]

    # ── Tasks ─────────────────────────────────────────────────────────────

    async def create_task(
        self,
        request_id: str,
        title: str,
        org_id: str,
        description: str | None = None,
        agent_type: str | None = None,
        agent_definition_id: str | None = None,
        parent_task_id: str | None = None,
        priority: str = "medium",
        sequence_order: int = 0,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_template_id: str | None = None,
        sandbox_config: dict | None = None,
        requesting_user_id: str | None = None,
        output_contract: dict | None = None,
        decision_config: dict | None = None,
        fusion_config: dict | None = None,
        backend_id: str | None = None,
        task_kind: str | None = None,
        execution_config: dict | None = None,
        remote_target_id: str | None = None,
        authored_by_llm_session_id: str | None = None,
    ) -> dict:
        """Create one task under an existing request.

        ``authored_by_llm_session_id`` is the trusted, header-derived LLM
        session that caused this insert. When it matches the request's
        ``origin_session_id`` and the caller owns the request, the request is
        flipped to ``decomposition_mode='author'`` in the same transaction so
        the background composer can never author a competing breakdown.
        Daemon planner sessions have no LLM session id and therefore can never
        flip ownership.
        """
        resolved_kind, resolved = _normalize_task_payload(
            agent_type=agent_type,
            output_contract=output_contract,
            decision_config=decision_config,
            fusion_config=fusion_config,
            task_kind=task_kind,
            execution_config=execution_config,
            sandbox_config=sandbox_config,
            sandbox_template_id=sandbox_template_id,
        )
        gate_error = await _script_entitlement_error(
            self.pool, org_id, resolved_kind
        )
        if gate_error:
            raise ValueError(gate_error)

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            parent_request = await conn.fetchrow(
                "SELECT status, created_by, origin_session_id FROM requests "
                "WHERE id = $1 AND organization_id = $2",
                UUID(request_id),
                UUID(org_id),
            )
            if parent_request is None:
                raise ValueError(f"Request {request_id} not found in this organization")
            if parent_request["status"] in ("cancelled", "completed"):
                raise ValueError(
                    f"Cannot create a task under a {parent_request['status']} "
                    "request — the request will not run this work"
                )
            row = await _insert_task_row(
                conn,
                request_id=request_id,
                org_id=org_id,
                title=title,
                description=description,
                agent_type=agent_type,
                agent_definition_id=agent_definition_id,
                parent_task_id=parent_task_id,
                priority=priority,
                sequence_order=sequence_order,
                model=model,
                reasoning_effort=reasoning_effort,
                sandbox_template_id=sandbox_template_id,
                sandbox_config=sandbox_config,
                requesting_user_id=requesting_user_id,
                output_contract=output_contract,
                decision_config=resolved["decision_config"],
                fusion_config=resolved["fusion_config"],
                backend_id=backend_id,
                task_kind=resolved_kind,
                execution_config=resolved["execution_config"],
                remote_target_id=remote_target_id,
            )
            await self._claim_decomposition_authorship(
                conn,
                request_id=request_id,
                org_id=org_id,
                parent_request=parent_request,
                llm_session_id=authored_by_llm_session_id,
            )
        task = dict(row)
        # Log creation event
        await self.add_task_event(
            str(task["id"]), "created", f"Task created: {title}", org_id=org_id
        )
        return task

    async def _claim_decomposition_authorship(
        self,
        conn,
        *,
        request_id: str,
        org_id: str,
        parent_request,
        llm_session_id: str | None,
    ) -> bool:
        """Flip a request to ``author`` when a chat session authored a task.

        Only a caller whose trusted LLM session is the request's originating
        session may claim authorship — that is exactly the situation where
        the composer and the chat agent would otherwise both produce a plan.
        Daemon planner sessions carry no LLM session id, so this is a no-op
        for them.
        """
        if not llm_session_id:
            return False
        origin_session_id = parent_request.get("origin_session_id")
        if not origin_session_id or str(origin_session_id) != str(llm_session_id):
            return False
        updated = await conn.execute(
            """UPDATE requests
               SET decomposition_mode = $3, updated_at = NOW()
               WHERE id = $1 AND organization_id = $2
                 AND decomposition_mode <> $3""",
            UUID(request_id),
            UUID(org_id),
            DECOMPOSITION_MODE_AUTHOR,
        )
        return updated.endswith("1")

    async def create_request_with_tasks(
        self,
        *,
        org_id: str,
        title: str,
        task_specs: list[dict],
        description: str | None = None,
        source: str = "user",
        priority: str = "medium",
        created_by: str | None = None,
        dependency_policy: str = "strict",
        memory_ids: list[dict] | None = None,
        target_repo: str | None = None,
        target_paths: list[str] | None = None,
        force_pending_approval: bool = False,
        goal_id: str | None = None,
        goal_milestone_index: int | None = None,
        backend_id: str | None = None,
        requested_model: str | None = None,
        authored_by_llm_session_id: str | None = None,
    ) -> dict:
        """Create a request and its full task plan atomically.

        Either the request and every task land, or nothing does. A request
        created this way is always ``decomposition_mode='author'`` so the
        background composer never adds a competing breakdown.

        Each entry in ``task_specs`` accepts the same keys as
        :meth:`create_task`, except that subtasks reference their parent by
        position with ``parent_task_index`` (0-based) instead of
        ``parent_task_id``.

        ``llm_session_id`` is passed by the MCP tool layer from trusted headers
        and is only used to stamp ``requests.origin_session_id``-backed lineage
        on the created tasks; authorship is explicit here regardless.
        """
        if not task_specs:
            raise ValueError("task_specs must contain at least one task")

        # Normalize and validate every spec BEFORE writing anything. A bad
        # spec must never leave a half-authored request behind.
        normalized: list[tuple[int, str, dict, dict]] = []
        for index, spec in enumerate(task_specs):
            if not isinstance(spec, dict):
                raise ValueError(f"task_specs[{index}] must be an object")
            parent_index = spec.get("parent_task_index")
            if parent_index is not None:
                if not isinstance(parent_index, int):
                    raise ValueError(
                        f"task_specs[{index}].parent_task_index must be an integer"
                    )
                if parent_index < 0 or parent_index >= len(task_specs):
                    raise ValueError(
                        f"task_specs[{index}].parent_task_index is out of range"
                    )
                if parent_index == index:
                    raise ValueError(
                        f"task_specs[{index}] cannot be its own parent"
                    )
                if parent_index > index:
                    raise ValueError(
                        f"task_specs[{index}] references a later task as parent; "
                        "parents must appear earlier in the plan"
                    )
            kind, resolved = _normalize_task_payload(
                agent_type=spec.get("agent_type"),
                output_contract=spec.get("output_contract"),
                decision_config=spec.get("decision_config"),
                fusion_config=spec.get("fusion_config"),
                task_kind=spec.get("task_kind"),
                execution_config=spec.get("execution_config"),
                sandbox_config=spec.get("sandbox_config"),
                sandbox_template_id=spec.get("sandbox_template_id"),
            )
            gate_error = await _script_entitlement_error(
                self.pool, org_id, kind
            )
            if gate_error:
                raise ValueError(f"task_specs[{index}]: {gate_error}")
            if not str(spec.get("title") or "").strip():
                raise ValueError(f"task_specs[{index}].title is required")
            normalized.append((index, kind, resolved, spec))

        created_tasks: list[dict] = []
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            req, created = await self._create_request_in_conn(
                conn,
                title=title,
                org_id=org_id,
                description=description,
                source=source,
                priority=priority,
                created_by=created_by,
                dependency_policy=dependency_policy,
                memory_ids=memory_ids,
                target_repo=target_repo,
                target_paths=target_paths,
                approval=(
                    APPROVAL_PENDING
                    if force_pending_approval
                    or _requires_approval(source, organization_id=org_id)
                    else APPROVAL_AUTO
                ),
                approved_at=(
                    None
                    if force_pending_approval
                    or _requires_approval(source, organization_id=org_id)
                    else datetime.now(timezone.utc)
                ),
                goal_id=goal_id,
                goal_milestone_index=goal_milestone_index,
                backend_id=backend_id,
                requested_model=requested_model,
                decomposition_mode=DECOMPOSITION_MODE_AUTHOR,
            )

            # A dedup hit or a refusal hands back an existing request (or a
            # skipped marker). Never append a fresh plan onto existing work — the
            # caller is told it already exists.
            if not created:
                existing = dict(req)
                existing.pop("_preexisting", None)
                return existing
            request_id = str(req["id"])

            created_ids: dict[int, str] = {}
            for index, kind, resolved, spec in normalized:
                parent_index = spec.get("parent_task_index")
                parent_task_id = (
                    created_ids.get(parent_index) if parent_index is not None else None
                )
                row = await _insert_task_row(
                    conn,
                    request_id=request_id,
                    org_id=org_id,
                    title=spec["title"],
                    description=spec.get("description"),
                    agent_type=spec.get("agent_type"),
                    agent_definition_id=spec.get("agent_definition_id"),
                    parent_task_id=parent_task_id,
                    priority=spec.get("priority") or "medium",
                    sequence_order=int(spec.get("sequence_order") or 0),
                    model=spec.get("model"),
                    reasoning_effort=spec.get("reasoning_effort"),
                    sandbox_template_id=spec.get("sandbox_template_id"),
                    sandbox_config=spec.get("sandbox_config"),
                    requesting_user_id=spec.get("requesting_user_id"),
                    output_contract=spec.get("output_contract"),
                    decision_config=resolved["decision_config"],
                    fusion_config=resolved["fusion_config"],
                    backend_id=spec.get("backend_id"),
                    task_kind=kind,
                    execution_config=resolved["execution_config"],
                    remote_target_id=spec.get("remote_target_id"),
                )
                task = dict(row)
                created_ids[index] = str(task["id"])
                created_tasks.append(task)

        # Emit creation events after commit so a rollback cannot leave events
        # for rows that never existed.
        for task in created_tasks:
            await self.add_task_event(
                str(task["id"]),
                "created",
                f"Task created: {task['title']}",
                org_id=org_id,
            )

        result = dict(req)
        result["task_ids"] = [str(t["id"]) for t in created_tasks]
        result["tasks_created"] = len(created_tasks)
        result["decomposition_mode"] = DECOMPOSITION_MODE_AUTHOR
        return result


    async def get_task(self, task_id: str, org_id: str | None = None) -> dict | None:
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM tasks WHERE id = $1 AND organization_id = $2",
                    UUID(task_id),
                    UUID(org_id),
                )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM tasks WHERE id = $1", UUID(task_id)
                )
        return dict(row) if row else None

    async def get_task_for_requester(
        self,
        task_id: str,
        org_id: str,
        *,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict | None:
        """Fetch a task only if its request is visible to the requester.

        Same visibility rule the request list tools use: a scoped session sees
        only requests it created (plus daemon/system requests when allowed).
        Tool handlers that accept a caller-supplied task id must go through this
        rather than ``get_task``, or a task can write events and outputs onto
        another task in the same organization.
        """
        params: list[Any] = [UUID(task_id), UUID(org_id)]
        where = "t.id = $1 AND t.organization_id = $2"
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            where += " AND " + self._request_visibility_condition("$3", "$4")
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            row = await conn.fetchrow(
                f"""SELECT t.* FROM tasks t
                    JOIN requests r ON r.id = t.request_id
                    WHERE {where}""",
                *params,
            )
        return dict(row) if row else None

    async def get_request_memory_links(self, request_id: str, org_id: str) -> list[dict]:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                """SELECT rm.memory_id, rm.relation, rm.created_at,
                          m.content, m.type AS memory_type, m.tags,
                          m.metadata->>'status' AS status
                   FROM request_memories rm
                   JOIN memories m ON rm.memory_id = m.id
                   WHERE rm.request_id = $1
                     AND m.organization_id = $2
                     AND m.deleted_at IS NULL
                   ORDER BY rm.created_at""",
                UUID(request_id),
                UUID(org_id),
            )
        return [dict(row) for row in rows]

    async def active_request_exists_by_title(self, title: str, org_id: str) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            return bool(
                await conn.fetchval(
                    """SELECT id FROM requests
                       WHERE title = $1
                         AND organization_id = $2::uuid
                         AND status NOT IN ('completed', 'failed', 'cancelled')
                       LIMIT 1""",
                    title,
                    org_id,
                )
            )

    async def active_scan_request_id(self, target_repo: str, org_id: str) -> str | None:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            request_id = await conn.fetchval(
                """SELECT id FROM requests
                   WHERE target_repo = $1
                     AND organization_id = $2
                     AND status NOT IN ('completed', 'failed', 'cancelled')
                   LIMIT 1""",
                target_repo,
                org_id,
            )
        return None if request_id is None else str(request_id)

    async def owns_request(
        self,
        request_id: str,
        *,
        org_id: str,
        user_id: str,
    ) -> bool:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, user_id=user_id
        ) as conn:
            return bool(
                await conn.fetchval(
                    """SELECT EXISTS(
                           SELECT 1 FROM requests
                           WHERE id = $1::uuid AND organization_id = $2::uuid
                             AND created_by = $3::uuid
                       )""",
                    request_id,
                    org_id,
                    user_id,
                )
            )

    async def notify_ready(self, *, org_id: str, payload: str) -> None:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role="daemon"
        ) as conn:
            await conn.execute(
                "SELECT pg_notify('request_ready', $1)",
                payload,
            )

    async def list_tasks(
        self,
        request_id: str,
        status: str | None = None,
        org_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        base = "FROM tasks WHERE request_id = $1"
        params: list[Any] = [UUID(request_id)]
        if org_id:
            params.append(UUID(org_id))
            base += f" AND organization_id = ${len(params)}"
        if status:
            params.append(status)
            base += f" AND status = ${len(params)}"

        count_query = f"SELECT COUNT(*) AS total {base}"
        query = (
            f"SELECT * {base} ORDER BY sequence_order, created_at "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        params_with_page = [*params, limit, offset]

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, *params_with_page)
        return {
            "items": [dict(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def list_pending_requests(
        self,
        org_id: str,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict:
        """Get pending requests, including those with no tasks yet."""
        base = """FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
                   WHERE r.organization_id = $1
                     AND r.status = 'pending'"""
        params: list[Any] = [UUID(org_id)]
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            base += " AND " + self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(DISTINCT r.id) AS total {base}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT r.*, count(t.id) as task_count
                   {base}
                   GROUP BY r.id
                   ORDER BY
                     CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     r.created_at
                         LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
                     *params,
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

    async def list_planning_targets(
        self,
        org_id: str,
        *,
        user_id: str | None = None,
        role: str = "member",
        limit: int = 50,
    ) -> list[dict]:
        """Return goal milestones that the planner MUST progress this cycle.

        This is the canonical "what's plannable" view. The cognitive
        planner does not choose between targets — it advances every entry
        this method returns. Filtering rules (all enforced in SQL):

          - Goal exists, not soft-deleted, organization scoped
          - Goal metadata.status is 'active' (or missing — treated as active)
          - The goal's first 'active' milestone (if it has milestones) has
            no ``start_after`` set OR ``start_after`` is in the past
          - The (goal_id, milestone_index) pair has no open request
            already targeting it via the structured fields. We use the
            structured fields (NOT request_memories) because that's the
            exact in-flight key.

        Open-ended goals (no milestones array) are returned with
        ``next_milestone_index=None`` and the goal-level dedup applies.

        Returns a list of dicts:
            {
              "goal_id": str,
              "goal_title": str (truncated),
              "next_milestone_index": int | None,
              "next_milestone_description": str | None,
              "next_milestone_start_after": str | None,
              "suggested_title": str,
                            "target_repo": str | None,
                            "target_paths": list[str],
              "total_milestones": int,
              "active_milestone_indexes": list[int],
              "completed_milestone_count": int,
            }

        ``user_id`` filters to a single user's goals (used by per-user
        cognitive fan-out). Otherwise returns goals for the whole org.

        Contract (2026-10-06, decided): member-role callers MUST pass
        ``user_id`` — the listing is scoped to that user's goals and the repo
        refuses to guess an owner (org-wide plannable targets are the
        daemon/planner path). The API router always threads the effective
        user for member callers; the MCP tool defaults to the calling agent's
        user.
        """
        if role == "member" and not user_id:
            raise ValueError(
                "list_planning_targets: member-role callers must pass user_id "
                "(scope to their own goals); org-wide targets are a "
                "daemon/planner path (role='daemon')"
            )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id, role=role
        ) as conn:
            params: list = [UUID(org_id)]
            user_clause = ""
            if user_id:
                params.append(UUID(user_id))
                user_clause = f" AND m.user_id = ${len(params)}::uuid"

            params.append(limit)
            limit_param = f"${len(params)}"

            rows = await conn.fetch(
                f"""SELECT m.id, m.content, m.metadata, m.user_id
                    FROM memories m
                    WHERE m.organization_id = $1::uuid
                      AND m.type = 'goal'
                      AND m.deleted_at IS NULL
                      AND COALESCE(m.metadata->>'status', 'active') = 'active'
                      {user_clause}
                    ORDER BY m.updated_at DESC
                    LIMIT {limit_param}""",
                *params,
            )

            now_utc = datetime.now(timezone.utc)
            targets: list[dict] = []
            for row in rows:
                metadata = _coerce_metadata(row["metadata"])
                summary = _milestones_summary(metadata)
                short_title = (row["content"] or "")[:60].splitlines()[0].strip()

                # Find the first active milestone whose start_after gate
                # (if any) has passed. start_after in the future skips
                # the milestone for this cycle.
                next_idx: int | None = None
                next_desc: str | None = None
                next_start_after: str | None = None
                if metadata:
                    milestones = metadata.get("milestones") or []
                    if isinstance(milestones, list):
                        for i, ms in enumerate(milestones, start=1):
                            if not isinstance(ms, dict):
                                continue
                            if ms.get("status") != "active":
                                continue
                            sa_raw = ms.get("start_after")
                            if sa_raw:
                                sa_dt = _parse_iso_datetime(sa_raw)
                                if sa_dt is not None and sa_dt > now_utc:
                                    # Future-dated — skip this milestone
                                    # this cycle. Continue scanning in case
                                    # a later milestone is unblocked, but
                                    # that's unusual; first active is the
                                    # canonical pick.
                                    continue
                            next_idx = i
                            next_desc = (ms.get("description") or "").strip() or None
                            next_start_after = sa_raw if isinstance(sa_raw, str) else None
                            break

                # Goals with milestones but no plannable one this cycle
                # (all completed, all abandoned, or all future-dated) are
                # NOT returned. They'll appear next cycle if any milestone
                # becomes plannable.
                if summary["total"] > 0 and next_idx is None:
                    continue

                # Dedup against the structured (goal_memory_id,
                # goal_milestone_index) fields. If an open request already
                # targets this exact pair, skip — the planner shouldn't
                # double-up. Also skip if any open request targets the
                # same goal with a NULL milestone index (which represents
                # "advancing the whole goal" — blocks per-milestone work
                # until it completes; also covers legacy rows backfilled
                # by migration 068 that only have goal_memory_id set).
                in_flight = await conn.fetchval(
                    """SELECT 1 FROM requests
                       WHERE organization_id = $1::uuid
                         AND goal_memory_id = $2::uuid
                         AND (
                             goal_milestone_index IS NOT DISTINCT FROM $3
                             OR goal_milestone_index IS NULL
                         )
                         AND status NOT IN ('completed', 'cancelled')
                       LIMIT 1""",
                    UUID(org_id),
                    str(row["id"]),
                    next_idx,
                )
                if in_flight:
                    continue

                # Suggested title is informational only — the planner is
                # free to write its own. The structured fields are what
                # actually identify the work.
                if next_idx is not None and next_desc:
                    suggested_title = (
                        f"{short_title or 'Goal'} M{next_idx}: {next_desc[:80]}"
                    ).strip()
                else:
                    suggested_title = short_title or "Goal"

                target_repo = None
                target_paths: list[str] = []
                if metadata:
                    raw_repo = (
                        metadata.get("target_repo")
                        or metadata.get("repo")
                        or metadata.get("repository")
                    )
                    if isinstance(raw_repo, str) and raw_repo.strip():
                        target_repo = raw_repo.strip()
                    raw_paths = metadata.get("target_paths") or metadata.get("paths") or []
                    if isinstance(raw_paths, str) and raw_paths.strip():
                        target_paths = [raw_paths.strip()]
                    elif isinstance(raw_paths, list):
                        target_paths = [str(p).strip() for p in raw_paths if str(p).strip()]

                targets.append({
                    "goal_id": str(row["id"]),
                    "goal_title": short_title,
                    "next_milestone_index": next_idx,
                    "next_milestone_description": next_desc,
                    "next_milestone_start_after": next_start_after,
                    "suggested_title": suggested_title,
                    "target_repo": target_repo,
                    "target_paths": target_paths,
                    "total_milestones": summary["total"],
                    "active_milestone_indexes": summary["active_indexes"],
                    "completed_milestone_count": summary["completed_count"],
                })

        return targets

    async def list_active_work(
        self,
        org_id: str,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict:
        """Get all non-terminal requests with task status summaries.

        Returns requests that are pending/in_progress/planned/needs_rework/review
        AND requests in 'failed' state (because failed work is still active from
        the planner's perspective — it needs to be fixed/retried, not duplicated).
        Only 'completed' and 'cancelled' are excluded as truly terminal.
        """
        base = """FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
                   WHERE r.organization_id = $1
                     AND r.status NOT IN ('completed', 'cancelled')"""
        params: list[Any] = [UUID(org_id)]
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            base += " AND " + self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(DISTINCT r.id) AS total {base}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT r.id, r.title, r.description, r.status, r.priority,
                          r.source, r.created_at,
                          count(t.id) FILTER (WHERE t.status = 'pending') AS tasks_pending,
                          count(t.id) FILTER (WHERE t.status = 'planned') AS tasks_planned,
                          count(t.id) FILTER (
                              WHERE t.status IN ('claimed', 'running')
                          ) AS tasks_running,
                          count(t.id) FILTER (WHERE t.status = 'completed') AS tasks_completed,
                          count(t.id) FILTER (WHERE t.status = 'failed') AS tasks_failed,
                          count(t.id) AS tasks_total
                   {base}
                   GROUP BY r.id
                   ORDER BY
                     CASE r.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     r.created_at
                         LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
                     *params,
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

    async def list_delegated_work_for_session(
        self,
        session_id: str,
        org_id: str,
        *,
        max_requests: int = 10,
        max_tasks_per_request: int = 12,
    ) -> dict:
        """Requests a chat session delegated, with their task rows.

        This is the read model behind both the chat's delegated-work panel and
        the "what is my sub-agent doing" digest injected into the next turn.
        Lineage comes from either path the system actually writes:
        ``requests.origin_session_id`` (set by create_request from trusted
        headers) or ``llm_session_requests`` (the explicit edge). Both are
        resolved so a session never loses sight of work it caused.

        Returns ``{"requests": [...], "active_task_count": n,
        "revision": "<token>"}``. The revision changes whenever any status or
        result in the tree changes, so a poller can cheaply detect "nothing
        new" without diffing the payload.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            request_rows = await conn.fetch(
                """SELECT DISTINCT r.id::text AS id, r.title, r.status,
                          r.priority, r.decomposition_mode,
                          r.review_count, r.max_reviews,
                          -- Where in the transcript this request was created.
                          -- The progress card must live there for its whole life,
                          -- not at the end of the thread: a card that moves to the
                          -- bottom reads as new activity and detaches the box from
                          -- the turn that caused it.
                          r.origin_message_id::text AS origin_message_id,
                          r.created_at, r.updated_at
                   FROM requests r
                   WHERE r.organization_id = $1
                     AND (
                         r.origin_session_id = $2
                         OR r.id IN (
                             SELECT lsr.request_id
                             FROM llm_session_requests lsr
                             JOIN llm_sessions s ON s.id = lsr.session_id
                             WHERE lsr.session_id = $2
                               AND lsr.relation = 'created'
                               AND s.organization_id = $1
                         )
                     )
                   ORDER BY r.created_at DESC
                   LIMIT $3""",
                UUID(org_id),
                UUID(session_id),
                max_requests,
            )
            if not request_rows:
                return {
                    "requests": [],
                    "active_task_count": 0,
                    "revision": "empty",
                    "report_revision": "none",
                }

            request_ids = [row["id"] for row in request_rows]
            # Report cards are written *after* a request settles (the report
            # service debounces for ~8s), so they are not reflected in
            # request/task rows. Folding them into the revision is what lets a
            # poller notice "a new card for this session exists" and refresh the
            # transcript — without it the thread kept showing the superseded
            # "in review" card until a manual reload.
            report_rows = await conn.fetch(
                """SELECT raw->>'request_id' AS request_id,
                          COALESCE(raw->>'report_kind', '') AS report_kind,
                          created_at
                   FROM llm_session_events
                   WHERE session_id = $1
                     AND event_type = 'subagent_report'
                   ORDER BY created_at DESC
                   LIMIT 50""",
                UUID(session_id),
            )
            task_rows = await conn.fetch(
                """SELECT id::text AS id, request_id::text AS request_id,
                          title, status, agent_type, sequence_order,
                          result_summary, error, cancel_requested_at,
                          completed_at, updated_at,
                          -- Prefer the actual output. A remote executor reports a
                          -- terse result_summary ("Completed on remote X in 0.3s")
                          -- that would otherwise mask the host output.
                          -- Generous enough that a normal task result survives intact.
                          -- A truncated excerpt is worse than a long one: the model
                          -- reports the work as having no output when the text it needs
                          -- was cut. Callers that need the whole thing use
                          -- get_request_details, which returns full result text.
                          left(COALESCE(NULLIF(result, ''), result_summary, ''), 4000) AS result_excerpt
                   FROM (
                       SELECT t.*,
                              row_number() OVER (
                                  PARTITION BY t.request_id
                                  ORDER BY t.sequence_order, t.created_at
                              ) AS rn
                       FROM tasks t
                       WHERE t.request_id = ANY($1::uuid[])
                         AND t.title <> 'Post-completion review'
                   ) t
                   WHERE t.rn <= $2
                   ORDER BY t.request_id, t.sequence_order, t.created_at""",
                [UUID(rid) for rid in request_ids],
                max_tasks_per_request,
            )

        tasks_by_request: dict[str, list[dict]] = {rid: [] for rid in request_ids}
        for row in task_rows:
            tasks_by_request.setdefault(row["request_id"], []).append(dict(row))

        active_statuses = {"claimed", "running"}
        items: list[dict] = []
        active_task_count = 0
        revision_parts: list[str] = []
        for row in request_rows:
            req = dict(row)
            tasks = tasks_by_request.get(req["id"], [])
            counts = {
                "pending": 0,
                "planned": 0,
                "claimed": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "needs_review": 0,
            }
            for task in tasks:
                status = str(task.get("status") or "")
                if status in counts:
                    counts[status] += 1
                if status in active_statuses:
                    active_task_count += 1
            req["tasks"] = tasks
            req["task_counts"] = counts
            req["task_total"] = len(tasks)
            items.append(req)
            revision_parts.append(
                f"{req['id']}:{req['status']}:{req.get('updated_at')}"
            )
            for task in tasks:
                revision_parts.append(
                    f"{task['id']}:{task.get('status')}:{task.get('updated_at')}"
                )

        # Report cards get their OWN revision, separate from the work revision.
        #
        # A card is written *after* the request/task rows settle, so it has to be
        # detectable by a poller. But it is also a transcript row, unlike the
        # in-place progress patch that request/task churn feeds: bundling the two
        # together forced every poller to choose between missing new cards and
        # rebuilding the whole thread every few seconds (which reset scroll and
        # made the page jump while work ran). Two tokens let a client rebuild only
        # when a card actually appeared.
        report_parts = [
            f"report:{report['request_id']}:{report['report_kind']}:"
            f"{report['created_at']}"
            for report in report_rows
        ]

        revision = sha256("\x1f".join(revision_parts).encode()).hexdigest()[:16]
        report_revision = (
            sha256("\x1f".join(report_parts).encode()).hexdigest()[:16]
            if report_parts
            else "none"
        )
        return {
            "requests": items,
            "active_task_count": active_task_count,
            "revision": revision,
            "report_revision": report_revision,
        }

    async def list_recently_completed(
        self,
        org_id: str,
        hours: int = 2,
        limit: int = 25,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> list[dict]:
        """Get requests completed within the last N hours.

        Used by the cognitive loop to avoid re-creating work that was
        just finished. Without this, the window between a request completing
        and the goal memory being updated leaves a gap where duplicates
        can be created.
        """
        params: list[Any] = [UUID(org_id), hours]
        visibility = ""
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            visibility = " AND " + self._request_visibility_condition("$3", "$4")
        params.append(limit)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                """SELECT r.id, r.title, r.source, r.status, r.completed_at
                   FROM requests r
                   WHERE r.organization_id = $1
                     AND r.status IN ('completed', 'review')
                     AND r.completed_at > NOW() - make_interval(hours => $2)
                 """ + visibility + """
                   ORDER BY r.completed_at DESC
               LIMIT $""" + str(len(params)),
            *params,
            )
        return [dict(r) for r in rows]

    async def list_pending_tasks(
        self,
        org_id: str,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict:
        """Get all tasks ready to be claimed.

        Respects sequence_order as a stage gate: tasks at the same level are
        dispatchable together, and a later level waits until every task in
        each earlier level is terminal with an acceptable outcome.

        This correctly handles retries — if a task at sequence 0 fails but
        a retry task at the same sequence 0 completes, subsequent tasks
        are unblocked.

        The request's dependency_policy controls what happens when a
        predecessor fails or is cancelled:
          - 'strict' (default): at least one task at each earlier level must
            be 'completed' — failed/cancelled predecessors block unless a
            retry completed.
          - 'permissive': completed, failed, and cancelled all count as
            acceptable terminal states.
        """
        base = f"""{DISPATCHABLE_TASKS_FROM}
                   WHERE {DISPATCHABLE_TASK_PREDICATE}"""
        params: list[Any] = [UUID(org_id)]
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            base += " AND " + self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                                f"""SELECT t.*, r.title as request_title,
                                                    EXISTS (
                                                            SELECT 1 FROM task_outputs o
                                                            WHERE o.task_id = t.id
                                                                AND o.organization_id = t.organization_id
                                                    ) AS has_durable_output
                   {base}
                   ORDER BY
                     CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     t.sequence_order, t.created_at
                         LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
                     *params,
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

    async def list_queued_tasks(
        self,
        org_id: str,
        limit: int = 25,
        offset: int = 0,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict:
        """List all queued tasks, including work blocked from dispatch.

        Queued is the operational backlog: tasks in either ``pending`` or
        ``planned`` state. Unlike :meth:`list_pending_tasks`, this does not
        exclude work waiting on approval or an earlier sequence stage.
        """
        base = """FROM tasks t
                   JOIN requests r ON r.id = t.request_id
                   WHERE t.organization_id = $1
                     AND t.status IN ('pending', 'planned')"""
        params: list[Any] = [UUID(org_id)]
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            base += " AND " + self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                *params,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"""SELECT t.*, r.title AS request_title,
                          r.status AS request_status,
                          r.approval_status AS request_approval_status
                   {base}
                   ORDER BY
                     CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                     WHEN 'medium' THEN 2 ELSE 3 END,
                     t.created_at
                   LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
                *params,
                limit,
                offset,
            )
        return {
            "items": [dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def claim_task(
        self,
        task_id: str,
        instance_id: str,
        org_id: str | None = None,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> dict | None:
        """Atomically claim a pending task. Returns None if already claimed.

        Refuses to claim a task whose parent request has already reached a
        terminal state (cancelled, completed, failed). The dispatch query
        filters these out, but cancellation can land between dispatch
        selection and claim — we belt-and-suspender here so a stuck
        cancellation race doesn't run an LLM session for nothing.
        """
        now = datetime.now(timezone.utc)
        claim_expires_at = now + timedelta(seconds=lease_seconds)
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                       claimed_at = $3, last_heartbeat_at = $3,
                       claim_expires_at = $4, claim_version = claim_version + 1,
                       updated_at = $3
                       WHERE id = $1 AND status IN ('pending', 'planned')
                       AND organization_id = $5
                       AND EXISTS (
                           SELECT 1 FROM requests r
                           WHERE r.id = tasks.request_id
                             AND r.status NOT IN ('cancelled', 'completed', 'failed')
                       )
                       RETURNING *""",
                    UUID(task_id),
                    instance_id,
                    now,
                    claim_expires_at,
                    UUID(org_id),
                )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'claimed', claimed_by = $2,
                       claimed_at = $3, last_heartbeat_at = $3,
                       claim_expires_at = $4, claim_version = claim_version + 1,
                       updated_at = $3
                       WHERE id = $1 AND status IN ('pending', 'planned')
                       AND EXISTS (
                           SELECT 1 FROM requests r
                           WHERE r.id = tasks.request_id
                             AND r.status NOT IN ('cancelled', 'completed', 'failed')
                       )
                       RETURNING *""",
                    UUID(task_id),
                    instance_id,
                    now,
                    claim_expires_at,
                )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id,
                "claimed",
                f"Claimed by {instance_id}",
                metadata={"instance_id": instance_id},
                org_id=str(task["organization_id"]),
            )
            # Update parent request to in_progress if still pending/planned
            await self._ensure_request_in_progress(
                str(task["request_id"]), org_id=str(task["organization_id"])
            )
            return task
        return None

    async def claim_remote_task(
        self,
        *,
        remote_id: str,
        org_id: str,
        limit: int = 1,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> dict | None:
        """Atomically claim the next dispatchable task pinned to a remote.

        This is the remote half of the claim protocol. It uses the same
        single-statement compare-and-swap discipline as :meth:`claim_task`
        (so two remote agents racing for the same task cannot both win) and
        the same stage gates via REMOTE_DISPATCHABLE_TASK_PREDICATE.

        The claim is recorded under ``remote:{remote_id}`` so that
        ``heartbeat_instance`` renews its lease and ``release_stale_tasks``
        reaps it exactly like a local daemon claim.
        """
        now = datetime.now(timezone.utc)
        claim_expires_at = now + timedelta(seconds=lease_seconds)
        instance_id = f"remote:{remote_id}"
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                f"""WITH candidate AS (
                        SELECT t.id
                        {DISPATCHABLE_TASKS_FROM}
                        WHERE {REMOTE_DISPATCHABLE_TASK_PREDICATE}
                        ORDER BY
                          CASE t.priority WHEN 'urgent' THEN 0
                                          WHEN 'high' THEN 1
                                          WHEN 'medium' THEN 2
                                          ELSE 3 END,
                          t.sequence_order,
                          t.created_at
                        LIMIT $3
                        FOR UPDATE OF t SKIP LOCKED
                    )
                    UPDATE tasks t
                    SET status = 'claimed',
                        claimed_by = $4,
                        claimed_at = $5,
                        last_heartbeat_at = $5,
                        claim_expires_at = $6,
                        claim_version = claim_version + 1,
                        updated_at = $5
                    FROM candidate c
                    WHERE t.id = c.id
                    RETURNING t.*, (
                        SELECT r.title FROM requests r WHERE r.id = t.request_id
                    ) AS request_title""",
                UUID(org_id),
                UUID(remote_id),
                limit,
                instance_id,
                now,
                claim_expires_at,
            )
        if not row:
            return None
        task = dict(row)
        await self.add_task_event(
            str(task["id"]),
            "claimed",
            f"Claimed by remote agent {instance_id}",
            metadata={"instance_id": instance_id, "remote_id": str(remote_id)},
            org_id=org_id,
        )
        await self._ensure_request_in_progress(
            str(task["request_id"]), org_id=org_id
        )
        return task

    async def count_active_remote_tasks(self, *, remote_id: str, org_id: str) -> int:
        """Count tasks currently claimed by a remote's agent instance."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            value = await conn.fetchval(
                """SELECT COUNT(*) FROM tasks
                   WHERE organization_id = $1
                     AND remote_target_id = $2
                     AND status IN ('claimed', 'running')""",
                UUID(org_id),
                UUID(remote_id),
            )
        return int(value or 0)

    async def release_orphaned_remote_claims(
        self,
        *,
        remote_id: str,
        org_id: str,
        active_task_ids: list[str],
        grace_seconds: int = DEFAULT_REMOTE_ORPHAN_GRACE_SECONDS,
    ) -> list[str]:
        """Requeue claims a remote no longer reports working on.

        A lease-expiry wait is the wrong recovery time for this case. When the
        agent itself says "I am running nothing", the work it used to hold is
        known-dead immediately: waiting out a 30-minute lease strands the whole
        request behind a task nobody is executing.

        ``grace_seconds`` is the only protection against a wrong report — a
        client whose heartbeat happens to land in the gap between claiming a
        task and adding it to its active set. Nothing is released until the
        claim is at least that old, and a released task is only requeued
        (never failed), so a mistaken release costs one restart, not the work.

        Returns the ids that were released.
        """
        active = [UUID(str(t)) for t in active_task_ids if t]
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            rows = await conn.fetch(
                """UPDATE tasks
                   SET status = 'pending',
                       claimed_by = NULL,
                       claimed_at = NULL,
                       last_heartbeat_at = NULL,
                       claim_expires_at = NULL,
                       claim_version = claim_version + 1,
                       updated_at = NOW()
                   WHERE organization_id = $1
                     AND remote_target_id = $2
                     AND status IN ('claimed', 'running')
                     AND claimed_at IS NOT NULL
                     AND claimed_at < NOW() - ($3::int * INTERVAL '1 second')
                     AND NOT (id = ANY($4::uuid[]))
                   RETURNING id, sequence_order, title""",
                UUID(org_id),
                UUID(remote_id),
                int(grace_seconds),
                active,
            )
        released = [str(row["id"]) for row in rows]
        # Record each release so the activity page explains the restart rather
        # than showing a task that silently went back in the queue.
        for row in rows:
            await self.add_task_event(
                str(row["id"]),
                "claim_orphaned",
                (
                    "Requeued: the remote that claimed this task no longer "
                    "reports it as active (its agent restarted or never "
                    f"received the payload). Recovery waited {grace_seconds}s "
                    "for a wrong-report grace window."
                ),
                metadata={
                    "remote_id": str(remote_id),
                    "grace_seconds": int(grace_seconds),
                },
                org_id=org_id,
            )
        return released

    async def clear_remote_targeting(self, remote_id: str, org_id: str) -> int:
        """Unpin not-yet-claimed tasks from a remote (winding one down).

        Only ``pending``/``planned`` rows are touched: claimed or running work
        is left to the reaper so we never rip a task out from under an agent
        that is actively reporting progress.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                """UPDATE tasks
                   SET remote_target_id = NULL, updated_at = NOW()
                   WHERE organization_id = $1
                     AND remote_target_id = $2
                     AND status IN ('pending', 'planned')
                   RETURNING id""",
                UUID(org_id),
                UUID(remote_id),
            )
        return len(rows)

    async def get_daemon_instance(self, instance_id: str, *, org_id: str) -> dict | None:
        """Fetch a daemon_instances row (used for remote claim attribution)."""
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """SELECT * FROM daemon_instances
                   WHERE organization_id = $1 AND instance_id = $2""",
                UUID(org_id),
                instance_id,
            )
        return dict(row) if row else None

    async def update_task_model(
        self, task_id: str, model: str, org_id: str | None = None
    ) -> dict | None:
        """Write the resolved model back to the task record."""
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                "UPDATE tasks SET model = $1, updated_at = $2 WHERE id = $3 RETURNING *",
                model,
                now,
                UUID(task_id),
            )
        return dict(row) if row else None

    async def update_task_reasoning_effort(
        self,
        task_id: str,
        reasoning_effort: str | None,
        org_id: str | None = None,
    ) -> dict | None:
        """Write the resolved reasoning effort back to the task record."""
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """UPDATE tasks SET reasoning_effort = $1, updated_at = $2
                   WHERE id = $3 RETURNING *""",
                reasoning_effort,
                now,
                UUID(task_id),
            )
        return dict(row) if row else None

    # Statuses where the task is in flight or already finalized successfully.
    # Anything else (pending, planned, failed, cancelled, needs_review, etc.)
    # is safe to edit because the daemon won't be actively executing it.
    _NON_EDITABLE_TASK_STATUSES = ('claimed', 'running', 'completed')

    async def update_pending_task(
        self,
        task_id: str,
        org_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agent_type: str | None = None,
        sandbox_template_id: str | None = None,
        clear_sandbox_template: bool = False,
        backend_id: str | None = None,
        clear_backend: bool = False,
    ) -> dict | None:
        """Edit a task in place. Returns the updated task or None.

        Succeeds for any task that is NOT currently being executed and has
        not already completed successfully. That means: pending, planned,
        failed, cancelled, needs_review, etc. are all editable. Tasks in
        ``claimed``, ``running``, or ``completed`` cannot be modified.
        Pass ``clear_sandbox_template=True`` to explicitly null out the
        sandbox template (distinguishes "no change" from "remove"). Likewise
        ``clear_backend=True`` clears a pinned engine backend.
        """
        sets: list[str] = []
        params: list = []

        if title is not None:
            params.append(title)
            sets.append(f"title = ${len(params)}")
        if description is not None:
            params.append(description)
            sets.append(f"description = ${len(params)}")
        if model is not None:
            params.append(model)
            sets.append(f"model = ${len(params)}")
        if reasoning_effort is not None:
            params.append(reasoning_effort or None)
            sets.append(f"reasoning_effort = ${len(params)}")
        if agent_type is not None:
            params.append(agent_type)
            sets.append(f"agent_type = ${len(params)}")
        if clear_sandbox_template:
            sets.append("sandbox_template_id = NULL")
        elif sandbox_template_id is not None:
            params.append(UUID(sandbox_template_id))
            sets.append(f"sandbox_template_id = ${len(params)}")
        if clear_backend:
            sets.append("backend_id = NULL")
        elif backend_id is not None:
            params.append(UUID(backend_id))
            sets.append(f"backend_id = ${len(params)}")

        if not sets:
            return await self.get_task(task_id, org_id=org_id)

        sets.append(f"updated_at = ${len(params) + 1}")
        params.append(datetime.now(timezone.utc))

        params.append(UUID(task_id))
        params.append(UUID(org_id))

        non_editable = ', '.join(f"'{s}'" for s in self._NON_EDITABLE_TASK_STATUSES)
        sql = (
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ${len(params) - 1} "
            f"  AND organization_id = ${len(params)} "
            f"  AND status NOT IN ({non_editable}) "
            "RETURNING *"
        )
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(sql, *params)
        if row:
            edited_fields = sorted(s.split(" = ")[0] for s in sets if "updated_at" not in s)
            await self.add_task_event(
                task_id,
                "edited",
                f"Task edited: {', '.join(edited_fields)}",
                org_id=org_id,
            )
        return dict(row) if row else None

    async def start_task(
        self,
        task_id: str,
        org_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict | None:
        """Mark a claimed task as running."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           AND organization_id = $3
                           AND claimed_by = $4
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           AND organization_id = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                    )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           AND claimed_by = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'running', updated_at = $2
                           WHERE id = $1 AND status = 'claimed'
                           RETURNING *""",
                        UUID(task_id),
                        now,
                    )
        if row:
            org = dict(row).get("organization_id")
            await self.add_task_event(
                task_id,
                "running",
                "Agent started execution",
                org_id=str(org) if org else None,
            )
            return dict(row)
        return None

    async def complete_task(
        self,
        task_id: str,
        result: str,
        org_id: str | None = None,
        instance_id: str | None = None,
        result_structured: dict | None = None,
        result_summary: str | None = None,
        validation_status: str = "not_applicable",
        validation_errors: list | None = None,
        outputs: list[dict[str, Any]] | None = None,
    ) -> dict | None:
        """Mark task as completed with result.

        Only tasks in 'claimed' or 'running' state can be completed
        (workflow-audit/phase-4: status transition guard).
        """
        if validation_status not in _VALID_VALIDATION_STATUSES:
            valid = ", ".join(sorted(_VALID_VALIDATION_STATUSES))
            raise ValueError(
                f"Invalid validation_status '{validation_status}'. Must be one of: {valid}"
            )
        output_candidates: list[dict[str, Any]] = []
        if outputs:
            output_candidates.extend(_normalize_task_output(item) for item in outputs)
        output_candidates.extend(_extract_outputs_from_structured_result(result_structured))
        output_candidates.extend(_extract_outputs_from_text(result))
        output_candidates = _dedupe_task_outputs(output_candidates)
        now = datetime.now(timezone.utc)
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $8
                           AND claimed_by = $9
                           RETURNING *""",
                        UUID(task_id),
                        result,
                        json.dumps(result_structured) if result_structured is not None else None,
                        result_summary,
                        validation_status,
                        json.dumps(validation_errors) if validation_errors is not None else None,
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $8
                           RETURNING *""",
                        UUID(task_id),
                        result,
                        json.dumps(result_structured) if result_structured is not None else None,
                        result_summary,
                        validation_status,
                        json.dumps(validation_errors) if validation_errors is not None else None,
                        now,
                        UUID(org_id),
                    )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND claimed_by = $8
                           RETURNING *""",
                        UUID(task_id),
                        result,
                        json.dumps(result_structured) if result_structured is not None else None,
                        result_summary,
                        validation_status,
                        json.dumps(validation_errors) if validation_errors is not None else None,
                        now,
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'completed', result = $2,
                           result_structured = $3,
                           result_summary = $4,
                           validation_status = $5,
                           validation_errors = $6,
                           error = NULL,
                           completed_at = $7, updated_at = $7
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           RETURNING *""",
                        UUID(task_id),
                        result,
                        json.dumps(result_structured) if result_structured is not None else None,
                        result_summary,
                        validation_status,
                        json.dumps(validation_errors) if validation_errors is not None else None,
                        now,
                    )
        if row:
            task = dict(row)
            created_outputs: list[dict] = []
            for output in output_candidates:
                created_outputs.append(
                    await self.create_task_output(
                        task_id=task_id,
                        org_id=str(task["organization_id"]),
                        output=output,
                        created_by=(
                            str(task["requesting_user_id"])
                            if task.get("requesting_user_id")
                            else None
                        ),
                    )
                )
            task["outputs"] = created_outputs
            await self.add_task_event(
                task_id,
                "completed",
                f"Completed ({len(result)} chars output)",
                org_id=str(task["organization_id"]),
            )
            # Check if all tasks in request are done
            await self._check_request_completion(
                str(task["request_id"]), org_id=str(task["organization_id"])
            )
            return task
        return None

    async def fail_task(
        self,
        task_id: str,
        error: str,
        org_id: str | None = None,
        instance_id: str | None = None,
        result: str | None = None,
    ) -> dict | None:
        """Mark task as failed.

        Only tasks in 'claimed' or 'running' state can be failed
        (workflow-audit/phase-4: status transition guard).
        """
        now = datetime.now(timezone.utc)
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           result = COALESCE($6, result),
                           result_summary = CASE
                               WHEN $6::text IS NULL THEN result_summary
                               ELSE left($6::text, 500)
                           END,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $4
                           AND claimed_by = $5
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        UUID(org_id),
                        instance_id,
                        result,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           result = COALESCE($5, result),
                           result_summary = CASE
                               WHEN $5::text IS NULL THEN result_summary
                               ELSE left($5::text, 500)
                           END,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $4
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        UUID(org_id),
                        result,
                    )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           result = COALESCE($5, result),
                           result_summary = CASE
                               WHEN $5::text IS NULL THEN result_summary
                               ELSE left($5::text, 500)
                           END,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND claimed_by = $4
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        instance_id,
                        result,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'failed', error = $2,
                           result = COALESCE($4, result),
                           result_summary = CASE
                               WHEN $4::text IS NULL THEN result_summary
                               ELSE left($4::text, 500)
                           END,
                           completed_at = $3, updated_at = $3
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           RETURNING *""",
                        UUID(task_id),
                        error,
                        now,
                        result,
                    )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id,
                "failed",
                f"Failed: {error[:200]}",
                {"rejected_output_chars": len(result or "")},
                org_id=str(task["organization_id"]),
            )
            await self._check_request_completion(
                str(task["request_id"]), org_id=str(task["organization_id"])
            )
            return task
        return None

    async def mark_task_needs_review(
        self,
        task_id: str,
        error: str,
        org_id: str | None = None,
        instance_id: str | None = None,
        result: str | None = None,
    ) -> dict | None:
        """Stop automatic execution while preserving the result for human review."""
        now = datetime.now(timezone.utc)
        query = """UPDATE tasks SET status = 'needs_review', error = $2,
                   result = COALESCE($4, result),
                   result_summary = CASE
                       WHEN $4::text IS NULL THEN result_summary
                       ELSE left($4::text, 500)
                   END,
                   completed_at = $3, updated_at = $3
                   WHERE id = $1 AND status IN ('claimed', 'running')"""
        params: list[Any] = [UUID(task_id), error, now, result]
        if org_id:
            query += f" AND organization_id = ${len(params) + 1}"
            params.append(UUID(org_id))
        if instance_id:
            query += f" AND claimed_by = ${len(params) + 1}"
            params.append(instance_id)
        query += " RETURNING *"
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(query, *params)
        if not row:
            return None
        task = dict(row)
        await self.add_task_event(
            task_id,
            "needs_review",
            f"Manual review required: {error[:200]}",
            {"rejected_output_chars": len(result or "")},
            org_id=str(task["organization_id"]),
        )
        return task

    async def release_task(
        self,
        task_id: str,
        org_id: str | None = None,
        instance_id: str | None = None,
    ) -> dict | None:
        """Release a claimed/running task back to pending (for retry/stale recovery)."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $3
                           AND claimed_by = $4
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND organization_id = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        UUID(org_id),
                    )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                if instance_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           AND claimed_by = $3
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        instance_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                           claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                           updated_at = $2
                           WHERE id = $1 AND status IN ('claimed', 'running')
                           RETURNING *""",
                        UUID(task_id),
                        now,
                    )
        if row:
            await self.add_task_event(
                task_id,
                "released",
                "Task released back to pending",
                org_id=str(org_id) if org_id else None,
            )
            return dict(row)
        return None

    async def retry_task(self, task_id: str, org_id: str | None = None) -> dict | None:
        """Reset a failed or manually reviewed task back to pending for retry."""
        now = datetime.now(timezone.utc)
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                       completed_at = NULL, result = NULL,
                       error = NULL, updated_at = $2
                       WHERE id = $1 AND status IN ('failed', 'needs_review')
                       AND organization_id = $3 RETURNING *""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                       completed_at = NULL, result = NULL,
                       error = NULL, updated_at = $2
                       WHERE id = $1 AND status IN ('failed', 'needs_review') RETURNING *""",
                    UUID(task_id),
                    now,
                )
        if row:
            task = dict(row)
            await self.add_task_event(
                task_id,
                "retried",
                "Task queued for retry",
                org_id=str(task["organization_id"]),
            )
            # If parent request was marked failed, set it back to in_progress
            await self._ensure_request_in_progress(
                str(task["request_id"]), org_id=str(task["organization_id"])
            )
            return task
        return None

    async def cancel_task(
        self,
        task_id: str,
        org_id: str | None = None,
        requested_by: str | None = None,
    ) -> dict | None:
        """Cancel a task, interrupting it if it is already executing.

        Non-started tasks (``pending``/``planned``/``failed``/``needs_review``)
        transition to ``cancelled`` immediately and return the row.

        An executing task (``claimed``/``running``) cannot be transitioned here
        — the owning daemon session still holds the claim, and writing
        ``cancelled`` from underneath it would race that session's own
        completion write. Instead this stamps ``cancel_requested_at``, which
        fires the ``task_cancel`` notification, and returns the row with the
        request marker set. The daemon interrupts the session and calls
        :meth:`finalize_requested_cancel`; the reaper does the same if the
        owning instance has gone away. Callers can distinguish the two
        outcomes with ``row["status"]`` vs ``row["cancel_requested_at"]``.

        Returns ``None`` when the task is already terminal.
        """
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            # Two statements with different parameter sets on purpose: the
            # direct transition never references requested_by, and passing an
            # unused parameter leaves asyncpg unable to infer its type.
            if org_id:
                row = await conn.fetchrow(
                    """UPDATE tasks
                       SET status = 'cancelled',
                           completed_at = COALESCE(completed_at, $2),
                           updated_at = $2
                       WHERE id = $1
                         AND status NOT IN ('claimed', 'running', 'completed', 'cancelled')
                         AND organization_id = $3
                       RETURNING *""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
            else:
                row = await conn.fetchrow(
                    """UPDATE tasks
                       SET status = 'cancelled',
                           completed_at = COALESCE(completed_at, $2),
                           updated_at = $2
                       WHERE id = $1
                         AND status NOT IN ('claimed', 'running', 'completed', 'cancelled')
                       RETURNING *""",
                    UUID(task_id),
                    now,
                )
            if row is None:
                # Either already terminal, or executing. Ask for an interrupt
                # in the latter case; a terminal row stays a no-op because the
                # WHERE clause below requires a non-terminal status.
                requested_by_uuid = UUID(requested_by) if requested_by else None
                if org_id:
                    row = await conn.fetchrow(
                        """UPDATE tasks
                           SET cancel_requested_at = COALESCE(cancel_requested_at, $2),
                               cancel_requested_by = COALESCE(
                                   cancel_requested_by, $3::uuid
                               ),
                               updated_at = $2
                           WHERE id = $1
                             AND status IN ('claimed', 'running')
                             AND cancel_requested_at IS NULL
                             AND organization_id = $4
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        requested_by_uuid,
                        UUID(org_id),
                    )
                else:
                    row = await conn.fetchrow(
                        """UPDATE tasks
                           SET cancel_requested_at = COALESCE(cancel_requested_at, $2),
                               cancel_requested_by = COALESCE(
                                   cancel_requested_by, $3::uuid
                               ),
                               updated_at = $2
                           WHERE id = $1
                             AND status IN ('claimed', 'running')
                             AND cancel_requested_at IS NULL
                           RETURNING *""",
                        UUID(task_id),
                        now,
                        requested_by_uuid,
                    )
                if row is None:
                    return None
                task = dict(row)
                await self.add_task_event(
                    task_id,
                    "cancel_requested",
                    "Cancellation requested; interrupting the running session",
                    {
                        "requested_by": requested_by,
                        "status": task.get("status"),
                    },
                    org_id=str(task["organization_id"]),
                )
                return task
        task = dict(row)
        await self.add_task_event(
            task_id,
            "cancelled",
            "Task cancelled by user",
            org_id=str(task["organization_id"]),
        )
        return task

    async def finalize_requested_cancel(
        self,
        task_id: str,
        org_id: str | None = None,
        *,
        reason: str = "Cancelled by user",
    ) -> dict | None:
        """Move a claimed/running task to ``cancelled`` after an interrupt.

        Called by the daemon session that owned the claim (after it stopped the
        provider session) and by the stale-task reaper when the owning instance
        died mid-cancel. Only acts when a cancel was actually requested, so a
        normal completion/failure race can never be mislabeled as cancelled.
        """
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            org_clause = " AND organization_id = $3" if org_id else ""
            params: list[Any] = [UUID(task_id), now]
            if org_id:
                params.append(UUID(org_id))
            row = await conn.fetchrow(
                f"""UPDATE tasks
                    SET status = 'cancelled',
                        completed_at = COALESCE(completed_at, $2),
                        updated_at = $2,
                        claimed_by = NULL,
                        claim_expires_at = NULL
                    WHERE id = $1
                      AND status IN ('claimed', 'running')
                      AND cancel_requested_at IS NOT NULL"""
                + org_clause
                + " RETURNING *",
                *params,
            )
        if not row:
            return None
        task = dict(row)
        await self.add_task_event(
            task_id,
            "cancelled",
            reason,
            {"interrupted": True},
            org_id=str(task["organization_id"]),
        )
        await self._check_request_completion(
            str(task["request_id"]), org_id=str(task["organization_id"])
        )
        return task

    async def list_requested_cancels(
        self,
        org_id: str,
        *,
        task_ids: list[str] | None = None,
    ) -> list[dict]:
        """Return claimed/running tasks carrying an outstanding cancel request.

        Used by the daemon heartbeat sweep as the durability backstop for a
        dropped ``task_cancel`` notification.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if task_ids:
                rows = await conn.fetch(
                    """SELECT id::text AS id, request_id::text AS request_id,
                              claimed_by
                       FROM tasks
                       WHERE organization_id = $1
                         AND id = ANY($2::uuid[])
                         AND status IN ('claimed', 'running')
                         AND cancel_requested_at IS NOT NULL""",
                    UUID(org_id),
                    [UUID(t) for t in task_ids],
                )
            else:
                rows = await conn.fetch(
                    """SELECT id::text AS id, request_id::text AS request_id,
                              claimed_by
                       FROM tasks
                       WHERE organization_id = $1
                         AND status IN ('claimed', 'running')
                         AND cancel_requested_at IS NOT NULL""",
                    UUID(org_id),
                )
        return [dict(r) for r in rows]

    async def retry_task_with_feedback(
        self, task_id: str, feedback: str, org_id: str | None = None
    ) -> dict | None:
        """Retry a failed task and persist corrective feedback on the parent request.

        Refuses once the request has spent its review budget. Nothing else
        enforces ``max_reviews``: the review loop would otherwise requeue the
        same task forever, forever resetting the request to ``in_progress`` and
        never terminating. The request is escalated to ``review`` so a person
        makes the call instead of the loop spinning.

        Returns ``None`` when the retry is refused, which the review path
        already treats as "could not requeue".
        """
        # Reserve review budget *before* touching the task. Checking after
        # ``retry_task`` would mutate the task and only then discover the
        # budget is gone, leaving a pending task the queue will never dispatch
        # — the deadlock this guard exists to prevent.
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                reserved = await conn.fetchrow(
                    """UPDATE requests
                       SET review_count = review_count + 1, updated_at = $2
                       WHERE id = (SELECT request_id FROM tasks WHERE id = $1)
                         AND organization_id = $3
                         AND review_count < max_reviews
                       RETURNING id AS request_id""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
            else:
                reserved = await conn.fetchrow(
                    """UPDATE requests
                       SET review_count = review_count + 1, updated_at = $2
                       WHERE id = (SELECT request_id FROM tasks WHERE id = $1)
                         AND review_count < max_reviews
                       RETURNING id AS request_id""",
                    UUID(task_id),
                    now,
                )
            if not reserved:
                await self._escalate_review_budget_exhausted(
                    task_id, org_id=org_id
                )
                return None

        task = await self.retry_task(task_id, org_id=org_id)
        if not task:
            # The task could not be retried after budget was reserved; give the
            # budget back so a later legitimately-failed task can use it.
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                await conn.execute(
                    """UPDATE requests SET review_count = GREATEST(review_count - 1, 0)
                       WHERE id = $1""",
                    reserved["request_id"],
                )
            return None

        request_id = str(task["request_id"])
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                await conn.execute(
                    """UPDATE requests
                       SET status = $2,
                           review_feedback = $3,
                           updated_at = $4
                       WHERE id = $1 AND organization_id = $5""",
                    UUID(request_id),
                    REQUEST_STATUS_IN_PROGRESS,
                    feedback,
                    now,
                    UUID(org_id),
                )
            else:
                await conn.execute(
                    """UPDATE requests
                       SET status = $2,
                           review_feedback = $3,
                           updated_at = $4
                       WHERE id = $1""",
                    UUID(request_id),
                    REQUEST_STATUS_IN_PROGRESS,
                    feedback,
                    now,
                )

        await self.add_task_event(
            task_id,
            "review_feedback",
            "Retry queued with review feedback",
            metadata={"feedback": feedback},
            org_id=org_id,
        )
        refreshed = await self.get_task(task_id, org_id=org_id)
        return refreshed

    async def _escalate_review_budget_exhausted(
        self, task_id: str, *, org_id: str | None = None
    ) -> None:
        """Stop a rework loop and hand the request to a person.

        Called when the review budget is gone. The alternative — requeueing
        again — leaves the request ``in_progress`` with a pending task the queue
        will never dispatch, i.e. permanently stuck with no error to show for
        it. ``review`` is a terminal-for-automation state a human can act on.
        """
        now = datetime.now(timezone.utc)
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            row = await conn.fetchrow(
                """SELECT r.id, r.review_count, r.max_reviews
                   FROM requests r JOIN tasks t ON t.request_id = r.id
                   WHERE t.id = $1""",
                UUID(task_id),
            )
            if not row:
                return
            if org_id:
                await conn.execute(
                    """UPDATE requests SET status = 'review', updated_at = $2
                       WHERE id = $1 AND organization_id = $3""",
                    row["id"],
                    now,
                    UUID(org_id),
                )
            else:
                await conn.execute(
                    "UPDATE requests SET status = 'review', updated_at = $2 WHERE id = $1",
                    row["id"],
                    now,
                )
        logger.warning(
            "Rework refused for request %s: review budget exhausted (%s of %s). "
            "Escalated to human review instead of looping.",
            str(row["id"])[:8],
            row["review_count"],
            row["max_reviews"],
        )
        await self.add_task_event(
            task_id,
            "rework_refused",
            "Rework refused: the request exhausted its review budget. "
            "Escalated to human review instead of looping.",
            org_id=org_id,
        )

    async def requeue_completed_task_for_rework(
        self, task_id: str, feedback: str, org_id: str | None = None
    ) -> dict | None:
        """Reopen a completed task that a request review explicitly rejected.

        Refuses once the request has spent its review budget. This is the path
        the post-completion review agent drives, and without a budget check it
        is an unbounded loop: review rejects → task reopened → agent re-runs →
        review rejects again. One request reached 18 cycles against a
        ``max_reviews`` of 3 this way.

        On refusal the request escalates to ``review`` for a person and nothing
        is reopened, so the loop stops in a visible state instead of spinning.
        """
        now = datetime.now(timezone.utc)
        # Reserve the review budget BEFORE reopening the task, so a refused
        # rework cannot leave a reopened task behind for the queue to run.
        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            if org_id:
                reserved = await conn.fetchrow(
                    """UPDATE requests
                       SET review_count = review_count + 1, updated_at = $2
                       WHERE id = (SELECT request_id FROM tasks WHERE id = $1)
                         AND organization_id = $3
                         AND review_count < max_reviews
                       RETURNING id AS request_id""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
            else:
                reserved = await conn.fetchrow(
                    """UPDATE requests
                       SET review_count = review_count + 1, updated_at = $2
                       WHERE id = (SELECT request_id FROM tasks WHERE id = $1)
                         AND review_count < max_reviews
                       RETURNING id AS request_id""",
                    UUID(task_id),
                    now,
                )
        if not reserved:
            await self._escalate_review_budget_exhausted(task_id, org_id=org_id)
            return None

        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                       completed_at = NULL, result = NULL, result_structured = NULL,
                       result_summary = NULL, validation_status = 'not_applicable',
                       validation_errors = NULL, error = NULL, updated_at = $2
                       WHERE id = $1 AND status = 'completed'
                       AND organization_id = $3 RETURNING *""",
                    UUID(task_id),
                    now,
                    UUID(org_id),
                )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                row = await conn.fetchrow(
                    """UPDATE tasks SET status = 'pending', claimed_by = NULL,
                       claimed_at = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL,
                       completed_at = NULL, result = NULL, result_structured = NULL,
                       result_summary = NULL, validation_status = 'not_applicable',
                       validation_errors = NULL, error = NULL, updated_at = $2
                       WHERE id = $1 AND status = 'completed' RETURNING *""",
                    UUID(task_id),
                    now,
                )
        if not row:
            # Nothing was reopened, so release the reservation.
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                await conn.execute(
                    "UPDATE requests SET review_count = GREATEST(review_count - 1, 0) WHERE id = $1",
                    reserved["request_id"],
                )
            return None

        task = dict(row)
        request_id = str(task["request_id"])
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                await conn.execute(
                    """UPDATE requests SET status = $2,
                           review_feedback = $3,
                           updated_at = $4
                       WHERE id = $1 AND organization_id = $5""",
                    UUID(request_id),
                    REQUEST_STATUS_IN_PROGRESS,
                    feedback,
                    now,
                    UUID(org_id),
                )
            else:
                await conn.execute(
                    """UPDATE requests SET status = $2,
                           review_feedback = $3,
                           updated_at = $4
                       WHERE id = $1""",
                    UUID(request_id),
                    REQUEST_STATUS_IN_PROGRESS,
                    feedback,
                    now,
                )
        await self.add_task_event(
            task_id,
            "rework_queued",
            "Task reopened after request review",
            metadata={"feedback": feedback},
            org_id=org_id,
        )
        return await self.get_task(task_id, org_id=org_id)

    async def has_orphaned_tasks(self, *, org_id: str | None = None) -> bool:
        """True when a non-terminal task belongs to a terminal request.

        Such a task can never be dispatched (the queue excludes terminal
        parents), so it should be cancelled rather than left in the queued
        backlog. ``completed`` and ``cancelled`` are hard terminal; ``failed``
        is retryable and is intentionally left alone.

        ``claimed``/``running`` rows are included: the transition-time
        cancellation in :meth:`update_request_status` covers new cases, but a
        row left behind by a crash, a rolled-back transaction, or a pre-fix
        deployment is exactly the shape this sweep exists for, and an
        in-flight task under a terminal request is the worst one to miss —
        it is still consuming model time.
        """
        where = """
            t.status IN ('pending', 'planned', 'claimed', 'running')
              AND r.status IN ('cancelled', 'completed')
        """
        params: list[Any] = []
        if org_id:
            where += " AND t.organization_id = $1"
            params.append(UUID(org_id))
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            return bool(
                await conn.fetchval(
                    f"""SELECT EXISTS (
                            SELECT 1 FROM tasks t
                            JOIN requests r ON r.id = t.request_id
                            WHERE {where}
                        )""",
                    *params,
                )
            )

    async def cancel_orphaned_tasks(self, *, org_id: str | None = None) -> int:
        """Cancel non-terminal tasks whose parent request is terminal.

        Cancellation at request-transition time is the primary path; this is
        the self-healing sweep for rows that predate that path or were created
        before the parent reached a terminal state. Returns the number of tasks
        cancelled.

        In-flight rows are cancelled here because the sweep runs under the
        stale-task reaper's umbrella: nothing else will ever move them, the
        parent is already terminal, and the owning session's own completion
        write is refused by its status guard. ``cancel_requested_at`` is
        stamped alongside so the daemon still interrupts any live provider
        call for that task.
        """
        now = datetime.now(timezone.utc)
        where = """
            t.status IN ('pending', 'planned', 'claimed', 'running')
              AND r.status IN ('cancelled', 'completed')
        """
        params: list[Any] = [now]
        if org_id:
            where += " AND t.organization_id = $2"
            params.append(UUID(org_id))
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    f"""UPDATE tasks t
                        SET status = 'cancelled',
                            completed_at = $1,
                            updated_at = $1,
                            cancel_requested_at = COALESCE(
                                t.cancel_requested_at, $1
                            ),
                            claimed_by = NULL,
                            claim_expires_at = NULL
                        FROM requests r
                        WHERE r.id = t.request_id
                          AND {where}
                        RETURNING t.id, r.status AS request_status""",
                    *params,
                )
                if rows:
                    await conn.executemany(
                        """INSERT INTO task_events
                               (task_id, event_type, detail, metadata)
                           VALUES ($1, 'cancelled', $2, $3)""",
                        [
                            (
                                row["id"],
                                "Task cancelled because parent request was "
                                f"{row['request_status']}",
                                {"request_status": row["request_status"]},
                            )
                            for row in rows
                        ],
                    )
        return len(rows)

    async def release_stale_tasks(
        self,
        stale_minutes: int = 30,
        org_id: str | None = None,
        instance_stale_seconds: int = DEFAULT_INSTANCE_STALE_SECONDS,
    ) -> int:
        """Release tasks from stale instances, expired leases, or inactive claims.

        A claimed/running task is released only when one of these holds:
        - its claim lease has expired;
        - the owning daemon instance is dead (not active or not seen for
          instance_stale_seconds);
        - the task is ACTIVITY-STALE: no heartbeat from the owning daemon's
          heartbeat loop, no task-attributed tool-audit rows, and no task
          events within stale_seconds. Claim age alone never releases a task
          that is still producing work; a hung task (zero activity) is still
          reaped at the threshold.
        """
        stale_seconds = max(60, int(stale_minutes * 60))
        # A cancel request whose owning session died (or whose notification was
        # lost) must still complete. Finalize those before the release pass so
        # the reaper below never resurrects them as pending work.
        if org_id:
            await self.finalize_stale_cancels(
                org_id=org_id,
                instance_stale_seconds=instance_stale_seconds,
            )
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                rows = await conn.fetch(
                    """WITH stale AS (
                           SELECT t.id, t.claimed_by
                           FROM tasks t
                           WHERE t.status IN ('claimed', 'running')
                             AND t.organization_id = $2
                             AND (
                                 t.claim_expires_at < NOW()
                                 OR (
                                     t.claimed_by IS NOT NULL
                                     AND EXISTS (
                                         SELECT 1 FROM daemon_instances di
                                         WHERE di.organization_id = t.organization_id
                                           AND di.instance_id = t.claimed_by
                                            AND (
                                                di.status <> 'active'
                                                OR di.last_seen_at < NOW()
                                                    - make_interval(secs := $3)
                                            )
                                     )
                                 )
                                 OR (
                                     t.claimed_at IS NOT NULL
AND (
    COALESCE(t.last_heartbeat_at, t.claimed_at)
        < NOW() - make_interval(secs := $1)
    AND NOT EXISTS (
        SELECT 1 FROM tool_call_audit_log a
        WHERE a.task_id = t.id
          AND a.created_at >= NOW() - make_interval(secs := $1)
    )
    AND NOT EXISTS (
        SELECT 1 FROM task_events te
        WHERE te.task_id = t.id
          AND te.created_at >= NOW() - make_interval(secs := $1)
    )
)
                                 )
                             )
                           FOR UPDATE
                       ),
                       released AS (
                           UPDATE tasks t
                           SET status = 'pending',
                               claimed_by = NULL,
                               claimed_at = NULL,
                               claim_expires_at = NULL,
                               last_heartbeat_at = NULL,
                               updated_at = NOW()
                           FROM stale s
                           WHERE t.id = s.id
                           RETURNING t.id, s.claimed_by AS previous_claimed_by
                       )
                       SELECT id, previous_claimed_by FROM released""",
                    stale_seconds,
                    UUID(org_id),
                    instance_stale_seconds,
                )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                rows = await conn.fetch(
                    """WITH stale AS (
                           SELECT t.id, t.claimed_by
                           FROM tasks t
                           WHERE t.status IN ('claimed', 'running')
                             AND (
                                 t.claim_expires_at < NOW()
                                 OR (
                                     t.claimed_by IS NOT NULL
                                     AND EXISTS (
                                         SELECT 1 FROM daemon_instances di
                                         WHERE di.organization_id = t.organization_id
                                           AND di.instance_id = t.claimed_by
                                            AND (
                                                di.status <> 'active'
                                                OR di.last_seen_at < NOW()
                                                    - make_interval(secs := $2)
                                            )
                                     )
                                 )
                                 OR (
                                     t.claimed_at IS NOT NULL
AND (
    COALESCE(t.last_heartbeat_at, t.claimed_at)
        < NOW() - make_interval(secs := $1)
    AND NOT EXISTS (
        SELECT 1 FROM tool_call_audit_log a
        WHERE a.task_id = t.id
          AND a.created_at >= NOW() - make_interval(secs := $1)
    )
    AND NOT EXISTS (
        SELECT 1 FROM task_events te
        WHERE te.task_id = t.id
          AND te.created_at >= NOW() - make_interval(secs := $1)
    )
)
                                 )
                             )
                           FOR UPDATE
                       ),
                       released AS (
                           UPDATE tasks t
                           SET status = 'pending',
                               claimed_by = NULL,
                               claimed_at = NULL,
                               claim_expires_at = NULL,
                               last_heartbeat_at = NULL,
                               updated_at = NOW()
                           FROM stale s
                           WHERE t.id = s.id
                           RETURNING t.id, s.claimed_by AS previous_claimed_by
                       )
                       SELECT id, previous_claimed_by FROM released""",
                    stale_seconds,
                    instance_stale_seconds,
                )
        for row in rows:
            claimed_by = row["previous_claimed_by"] or "unknown"
            await self.add_task_event(
                str(row["id"]),
                "reaper",
                f"Claim expired (was claimed by {claimed_by}), task requeued to pending",
                org_id=org_id,
            )
        return len(rows)

    async def finalize_stale_cancels(
        self,
        *,
        org_id: str,
        instance_stale_seconds: int = DEFAULT_INSTANCE_STALE_SECONDS,
    ) -> int:
        """Finalize cancel requests whose owning session will never act on them.

        The daemon holding a claim normally interrupts the session itself. This
        covers the remaining cases: the owning instance died, the claim lease
        expired, or the ``task_cancel`` notification was dropped. Only
        claimed/running tasks whose owner is provably gone (or whose lease
        expired) are finalized, so a healthy in-flight cancel is left to the
        session that can actually stop the provider call.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                """UPDATE tasks t
                   SET status = 'cancelled',
                       completed_at = NOW(),
                       updated_at = NOW(),
                       claimed_by = NULL,
                       claim_expires_at = NULL
                   WHERE t.organization_id = $1
                     AND t.status IN ('claimed', 'running')
                     AND t.cancel_requested_at IS NOT NULL
                     AND (
                         (t.claim_expires_at IS NOT NULL
                          AND t.claim_expires_at < NOW())
                         OR (
                             t.claimed_by IS NOT NULL
                             AND EXISTS (
                                 SELECT 1 FROM daemon_instances di
                                 WHERE di.organization_id = t.organization_id
                                   AND di.instance_id = t.claimed_by
                                   AND (
                                       di.status <> 'active'
                                       OR di.last_seen_at
                                           < NOW() - make_interval(secs := $2)
                                   )
                             )
                         )
                     )
                   RETURNING t.id, t.request_id""",
                UUID(org_id),
                instance_stale_seconds,
            )
        for row in rows:
            await self.add_task_event(
                str(row["id"]),
                "cancelled",
                "Cancellation finalized after the owning claim expired",
                {"interrupted": False, "finalized_by": "reaper"},
                org_id=org_id,
            )
            await self._check_request_completion(
                str(row["request_id"]), org_id=org_id
            )
        return len(rows)

    async def stale_task_reaper_has_work(
        self,
        stale_minutes: int = 30,
        org_id: str | None = None,
        instance_stale_seconds: int = DEFAULT_INSTANCE_STALE_SECONDS,
    ) -> bool:
        """True when at least one claimed/running task is stale enough to release.

        Uses the same activity-based predicate as release_stale_tasks() so the
        preflight gate and the reaper always agree: lease expired, dead owning
        daemon instance, or no activity (heartbeat / tool-audit rows / task
        events) within the threshold.
        """
        stale_seconds = max(60, int(stale_minutes * 60))
        if org_id:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    WHERE t.status IN ('claimed', 'running')
                      AND t.organization_id = $2
                      AND (
                          t.claim_expires_at < NOW()
                          OR (
                              t.claimed_by IS NOT NULL
                              AND EXISTS (
                                  SELECT 1 FROM daemon_instances di
                                  WHERE di.organization_id = t.organization_id
                                    AND di.instance_id = t.claimed_by
                                    AND (
                                        di.status <> 'active'
                                        OR di.last_seen_at < NOW() - make_interval(secs := $3)
                                    )
                              )
                          )
                          OR (
                              t.claimed_at IS NOT NULL
AND (
    COALESCE(t.last_heartbeat_at, t.claimed_at)
        < NOW() - make_interval(secs := $1)
    AND NOT EXISTS (
        SELECT 1 FROM tool_call_audit_log a
        WHERE a.task_id = t.id
          AND a.created_at >= NOW() - make_interval(secs := $1)
    )
    AND NOT EXISTS (
        SELECT 1 FROM task_events te
        WHERE te.task_id = t.id
          AND te.created_at >= NOW() - make_interval(secs := $1)
    )
)
                          )
                      )
                    """,
                    stale_seconds,
                    UUID(org_id),
                    instance_stale_seconds,
                )
        else:
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks t
                    WHERE t.status IN ('claimed', 'running')
                      AND (
                          t.claim_expires_at < NOW()
                          OR (
                              t.claimed_by IS NOT NULL
                              AND EXISTS (
                                  SELECT 1 FROM daemon_instances di
                                  WHERE di.organization_id = t.organization_id
                                    AND di.instance_id = t.claimed_by
                                    AND (
                                        di.status <> 'active'
                                        OR di.last_seen_at < NOW() - make_interval(secs := $2)
                                    )
                              )
                          )
                          OR (
                              t.claimed_at IS NOT NULL
AND (
    COALESCE(t.last_heartbeat_at, t.claimed_at)
        < NOW() - make_interval(secs := $1)
    AND NOT EXISTS (
        SELECT 1 FROM tool_call_audit_log a
        WHERE a.task_id = t.id
          AND a.created_at >= NOW() - make_interval(secs := $1)
    )
    AND NOT EXISTS (
        SELECT 1 FROM task_events te
        WHERE te.task_id = t.id
          AND te.created_at >= NOW() - make_interval(secs := $1)
    )
)
                          )
                      )
                    """,
                    stale_seconds,
                    instance_stale_seconds,
                )
        return int(count or 0) > 0

    # ── Task Events ───────────────────────────────────────────────────────

    async def add_task_event(
        self,
        task_id: str,
        event_type: str,
        detail: str | None = None,
        metadata: dict | None = None,
        org_id: str | None = None,
    ) -> dict:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                task_exists = await conn.fetchval(
                    "SELECT 1 FROM tasks WHERE id = $1 AND organization_id = $2",
                    UUID(task_id),
                    UUID(org_id),
                )
                if not task_exists:
                    raise ValueError("Task not found")
            row = await conn.fetchrow(
                """INSERT INTO task_events (task_id, event_type, detail, metadata)
                   VALUES ($1, $2, $3, $4) RETURNING *""",
                UUID(task_id),
                event_type,
                detail,
                metadata or {},
            )
        return dict(row)

    async def list_task_events(
        self,
        task_id: str,
        limit: int = 25,
        offset: int = 0,
        org_id: str | None = None,
    ) -> dict:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                count_row = await conn.fetchrow(
                    """SELECT COUNT(*) AS total FROM task_events te
                       JOIN tasks t ON te.task_id = t.id
                       WHERE te.task_id = $1 AND t.organization_id = $2""",
                    UUID(task_id),
                    UUID(org_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT te.* FROM task_events te
                       JOIN tasks t ON te.task_id = t.id
                       WHERE te.task_id = $1 AND t.organization_id = $2
                       ORDER BY te.created_at LIMIT $3 OFFSET $4""",
                    UUID(task_id),
                    UUID(org_id),
                    limit,
                    offset,
                )
            else:
                count_row = await conn.fetchrow(
                    "SELECT COUNT(*) AS total FROM task_events WHERE task_id = $1",
                    UUID(task_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    "SELECT * FROM task_events WHERE task_id = $1 "
                    "ORDER BY created_at LIMIT $2 OFFSET $3",
                    UUID(task_id),
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

    async def get_live_task_output(
        self,
        request_id: str,
        org_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return in-flight model output for the request's running tasks.

        Keyed by task id so the page can look output up while rendering each
        task. Only tasks that are actually in flight are returned: a settled
        task's durable text lives in ``tasks.result`` and the daemon deletes
        its scratch row, so anything left behind is stale and is filtered out
        here rather than trusted.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                """SELECT o.task_id, o.content, o.updated_at
                   FROM task_live_output o
                   JOIN tasks t ON t.id = o.task_id
                   WHERE t.request_id = $1
                     AND o.organization_id = $2
                     AND t.status IN ('claimed', 'running')""",
                UUID(request_id),
                UUID(org_id),
            )
        return {
            str(row["task_id"]): {
                "content": row["content"] or "",
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    # ── Task ↔ Memory Links ──────────────────────────────────────────────

    async def link_memory(
        self,
        task_id: str,
        memory_id: str,
        relation: str = "created",
        org_id: str | None = None,
    ) -> None:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                task_exists = await conn.fetchval(
                    "SELECT 1 FROM tasks WHERE id = $1 AND organization_id = $2",
                    UUID(task_id),
                    UUID(org_id),
                )
                if not task_exists:
                    raise ValueError("Task not found")
                memory_exists = await conn.fetchval(
                    """SELECT 1 FROM memories
                       WHERE id = $1 AND organization_id = $2 AND deleted_at IS NULL""",
                    UUID(memory_id),
                    UUID(org_id),
                )
                if not memory_exists:
                    raise ValueError("Memory not found")
                await conn.execute(
                    """INSERT INTO task_memories (task_id, memory_id, relation)
                       VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                    UUID(task_id),
                    UUID(memory_id),
                    relation,
                )
            else:
                # task_memories has no per-tenant stamps by schema
                # (015_request_tracking.sql). Keep the statement for callers
                # running under the daemon role with explicit org context in
                # scope; the unscoped branch cannot persist cross-tenant
                # links because the caller's org is verified upstream.
                await conn.execute(
                    """INSERT INTO task_memories (task_id, memory_id, relation)
                       VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                    UUID(task_id),
                    UUID(memory_id),
                    relation,
                )
        await self.add_task_event(
            task_id,
            f"memory_{relation}",
            f"Memory {relation}: {memory_id[:8]}...",
            metadata={"memory_id": memory_id, "relation": relation},
            org_id=org_id,
        )

    async def list_task_memories(
        self,
        task_id: str,
        org_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                count_row = await conn.fetchrow(
                    """SELECT COUNT(*) AS total
                       FROM task_memories tm
                       JOIN tasks t ON tm.task_id = t.id
                       JOIN memories m ON tm.memory_id = m.id
                       WHERE tm.task_id = $1 AND t.organization_id = $2
                         AND m.organization_id = $2
                         AND m.deleted_at IS NULL""",
                    UUID(task_id),
                    UUID(org_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT tm.*, m.content, m.type as memory_type, m.tags
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       JOIN tasks t ON tm.task_id = t.id
                       WHERE tm.task_id = $1 AND t.organization_id = $2
                                  AND m.organization_id = $2
                                  AND m.deleted_at IS NULL
                       ORDER BY tm.created_at LIMIT $3 OFFSET $4""",
                    UUID(task_id),
                    UUID(org_id),
                    limit,
                    offset,
                )
            else:
                count_row = await conn.fetchrow(
                    """SELECT COUNT(*) AS total
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       WHERE tm.task_id = $1
                         AND m.deleted_at IS NULL""",
                    UUID(task_id),
                )
                total_count = count_row["total"] if count_row else 0
                rows = await conn.fetch(
                    """SELECT tm.*, m.content, m.type as memory_type, m.tags
                       FROM task_memories tm
                       JOIN memories m ON tm.memory_id = m.id
                       WHERE tm.task_id = $1
                                  AND m.deleted_at IS NULL
                       ORDER BY tm.created_at LIMIT $2 OFFSET $3""",
                    UUID(task_id),
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

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _ensure_request_in_progress(
        self, request_id: str, org_id: str | None = None
    ) -> None:
        """Move request to in_progress if it's not already active.

        Handles pending/planned states AND failed (for retry recovery).
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            if org_id:
                await conn.execute(
                    """UPDATE requests SET status = 'in_progress', updated_at = NOW()
                       WHERE id = $1 AND status IN ('pending', 'planned', 'failed', 'needs_rework')
                         AND organization_id = $2""",
                    UUID(request_id),
                    UUID(org_id),
                )
            else:
                await conn.execute(
                    """UPDATE requests SET status = 'in_progress', updated_at = NOW()
                       WHERE id = $1 AND status IN ('pending', 'planned', 'failed', 'needs_rework')""",
                    UUID(request_id),
                )

    async def _check_request_completion(
        self, request_id: str, org_id: str | None = None
    ) -> None:
        """If all work tasks are done, move request to review (or failed).

        Excludes request-review meta-tasks from the completion check. We
        identify them by EITHER agent_type='request-review' OR the canonical
        title 'Post-completion review' — the daemon's review-agent fallback
        path may create review tasks under a different agent_type (e.g.
        'code') when the dedicated review agent isn't accessible. Without
        the title-based exclusion those fallback reviews would count as
        work tasks and re-trigger another review task on completion,
        producing an infinite loop.

        This is only ever allowed to *advance* a request. It is called from
        every task transition (complete/fail/cancel), so without a guard a
        late task write could drag an already-decided request backwards —
        e.g. a task finishing after a request was completed would move it
        back to 'review' and strand it. The guard is applied inside
        ``update_request_status``'s WHERE clause, not read-then-write, so a
        concurrent finalize cannot be overwritten between the two.
        """
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            row = await conn.fetchrow(
                """SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled')) as done
                   FROM tasks
                   WHERE request_id = $1
                     AND agent_type IS DISTINCT FROM 'request-review'
                     AND title IS DISTINCT FROM 'Post-completion review'""",
                UUID(request_id),
            )
        if row and row["total"] > 0 and row["total"] == row["done"]:
            # Check if any failed
            async with scoped_acquire_on(
                self.pool, organization_id=org_id
            ) as conn:
                failed = await conn.fetchval(
                    """SELECT COUNT(*) FROM tasks
                       WHERE request_id = $1 AND status = 'failed'
                         AND agent_type IS DISTINCT FROM 'request-review'
                         AND title IS DISTINCT FROM 'Post-completion review'""",
                    UUID(request_id),
                )
                organization_id = await conn.fetchval(
                    "SELECT organization_id FROM requests WHERE id = $1",
                    UUID(request_id),
                )
            status = REQUEST_STATUS_FAILED if failed > 0 else (
                REQUEST_STATUS_REVIEW if _requires_post_completion_review(
                    organization_id=str(organization_id) if organization_id else None,
                )
                else REQUEST_STATUS_COMPLETED
            )
            advanced = await self.update_request_status(
                request_id,
                status,
                org_id=org_id,
                expected_current_statuses=_COMPLETION_ADVANCEABLE_STATUSES,
            )
            if advanced is None:
                # The request had already left the advanceable set (it was
                # completed/cancelled/failed/review, or another writer won the
                # race). Not an error — the deliberate outcome is "do not
                # regress a decision that was already made" — but it is worth
                # seeing, because a task settling after its request finished is
                # otherwise invisible.
                logger.info(
                    "Task completion for request %s did not advance it to %s: "
                    "the request is already past the states task completion may "
                    "move it out of",
                    request_id,
                    status,
                )

    async def reconcile_request_statuses(self, org_id: str | None = None) -> int:
        """Fix request statuses that got out of sync with their tasks.

        Handles three cases:
        1. Request is 'in_progress' but all tasks are terminal → complete/fail it
        2. Request is 'pending' but has running/completed tasks → mark in_progress
        3. Request is stuck in 'review' with a completed review task whose
           recorded decision never landed → apply that decision

        Returns the number of requests fixed.
        """
        fixed = 0
        org_filter = "AND r.organization_id = $1" if org_id else ""
        params: list = [UUID(org_id)] if org_id else []

        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            # Case 1: in_progress requests where all tasks are done
            rows = await conn.fetch(
                     f"""SELECT r.id, r.organization_id FROM requests r
                         WHERE r.status = 'in_progress' {org_filter}
                   AND NOT EXISTS (
                       SELECT 1 FROM tasks t
                       WHERE t.request_id = r.id
                       AND t.status NOT IN ('completed', 'failed', 'cancelled')
                   )
                   AND EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id)""",
                *params,
            )
            for row in rows:
                await self._check_request_completion(
                    str(row["id"]), org_id=str(row["organization_id"])
                )
                fixed += 1

                 # Case 2: pending/planned requests with active/completed tasks.
                 # Needs-rework waits for an explicit task claim, which already
                 # moves the parent request back to in_progress.
            rows = await conn.fetch(
                f"""SELECT DISTINCT r.id, r.organization_id FROM requests r
                   JOIN tasks t ON t.request_id = r.id
                     WHERE r.status IN ('pending', 'planned') {org_filter}
                   AND t.status IN ('claimed', 'running', 'completed')""",
                *params,
            )
            for row in rows:
                await self._ensure_request_in_progress(
                    str(row["id"]), org_id=str(row["organization_id"])
                )
                fixed += 1

            # Case 3: a request stuck in 'review' that has nothing left to
            # dispatch and no review task that will ever run again.
            #
            # Nothing else repairs this shape. `_create_request_review_task`
            # deliberately refuses to add a second review task when a review
            # task already exists (that guard is what prevents an infinite
            # review loop when the review agent's output cannot be parsed), so
            # a request whose review task completed — but whose decision never
            # reached the request row, e.g. a later task transition regressed
            # it out of 'completed' — has no path forward at all.
            #
            # The action is to *finish the decision that was already recorded*
            # rather than to re-review: re-opening a review task would re-run
            # an LLM review for a verdict that already exists, and would loop
            # for any review whose decision text is unparseable. A review task
            # that recorded no usable decision is therefore left for a human,
            # with a one-shot event so the stall is visible instead of silent.
            stuck_rows = await conn.fetch(
                f"""SELECT r.id, r.organization_id, r.review_count, r.max_reviews
                      FROM requests r
                     WHERE r.status = 'review' {org_filter}
                       AND NOT EXISTS (
                           SELECT 1 FROM tasks t
                            WHERE t.request_id = r.id
                              AND t.status IN ('pending', 'planned', 'claimed', 'running')
                       )
                       AND EXISTS (
                           SELECT 1 FROM tasks t
                            WHERE t.request_id = r.id
                              AND (t.agent_type = 'request-review'
                                   OR t.title = 'Post-completion review')
                       )""",
                *params,
            )
            for row in stuck_rows:
                request_org_id = str(row["organization_id"])
                review_task = await conn.fetchrow(
                    """SELECT id, status, result, error
                         FROM tasks
                        WHERE request_id = $1
                          AND (agent_type = 'request-review'
                               OR title = 'Post-completion review')
                        ORDER BY COALESCE(completed_at, updated_at) DESC
                        LIMIT 1""",
                    row["id"],
                )
                if review_task is None:
                    continue
                decision = _recorded_review_decision(review_task["result"])
                if decision == "APPROVED":
                    updated = await self.update_request_status(
                        str(row["id"]),
                        REQUEST_STATUS_COMPLETED,
                        org_id=request_org_id,
                        expected_current_statuses=(REQUEST_STATUS_REVIEW,),
                    )
                    if updated:
                        await self.add_task_event(
                            str(review_task["id"]),
                            "request_review_approved",
                            "Reconciled: request was stuck in review; applied "
                            "the APPROVED decision already recorded by its "
                            "review task.",
                            {"reconciled": True, "recommendation": "APPROVED"},
                            org_id=request_org_id,
                        )
                        fixed += 1
                elif decision == "NEEDS_REWORK":
                    # Requeue the tasks the verdict named *before* moving the
                    # request. Flipping the status alone would reproduce the
                    # very stall this case exists to clear: a request reading
                    # "needs rework" whose tasks are all still `completed` has
                    # nothing dispatchable, so it would sit there forever under
                    # a new label.
                    #
                    # Ordering matches the live path in
                    # daemon/review/lifecycle.py: requeue named tasks, and only
                    # fall back to a bare status flip when nothing could be
                    # reopened (which is also what that path does).
                    reopen_ids = _recorded_review_task_ids(review_task["result"])
                    rework_feedback = _recorded_review_feedback(review_task["result"])
                    # An exhausted review budget is a fixed point, not a state to
                    # re-apply. `_escalate_review_budget_exhausted` deliberately
                    # parks a request in `review` precisely *because* requeueing
                    # again would loop forever. Re-applying the recorded
                    # NEEDS_REWORK here would undo that decision on every sweep:
                    # observed live, a request moved review → needs_rework →
                    # review → needs_rework every two minutes with zero tasks
                    # ever reopened, and its conversation was never told.
                    budget_spent = (
                        int(row["review_count"] or 0) >= int(row["max_reviews"] or 0)
                        and int(row["max_reviews"] or 0) > 0
                    )
                    if budget_spent:
                        existing = await conn.fetchval(
                            """SELECT 1 FROM task_events
                                WHERE task_id = $1
                                  AND event_type = 'request_review_budget_exhausted'""",
                            review_task["id"],
                        )
                        if not existing:
                            await self.add_task_event(
                                str(review_task["id"]),
                                "request_review_budget_exhausted",
                                "Request is parked in review with its rework "
                                "budget spent. Leaving it here for a person "
                                "instead of re-queuing work that would loop.",
                                {
                                    "reconciled": True,
                                    "reason": "review_budget_exhausted",
                                    "review_count": int(row["review_count"] or 0),
                                    "max_reviews": int(row["max_reviews"] or 0),
                                },
                                org_id=request_org_id,
                            )
                            fixed += 1
                        continue
                    requeued: list[str] = []
                    for candidate_id in reopen_ids:
                        # Guard against a verdict that names the review task
                        # itself — reopening it would re-run the review whose
                        # decision we are applying.
                        if str(candidate_id) == str(review_task["id"]):
                            continue
                        reopened = await self.requeue_completed_task_for_rework(
                            str(candidate_id),
                            rework_feedback,
                            org_id=request_org_id,
                        )
                        if reopened:
                            requeued.append(str(candidate_id))

                    # requeue_completed_task_for_rework already moves the parent
                    # to in_progress; a bare flip is only correct when no task
                    # was reopened.
                    updated = (
                        None
                        if requeued
                        else await self.update_request_status(
                            str(row["id"]),
                            REQUEST_STATUS_NEEDS_REWORK,
                            org_id=request_org_id,
                            expected_current_statuses=(REQUEST_STATUS_REVIEW,),
                        )
                    )
                    if requeued or updated:
                        await self.add_task_event(
                            str(review_task["id"]),
                            "request_review_needs_rework",
                            (
                                "Reconciled: request was stuck in review; "
                                "applied the NEEDS_REWORK decision already "
                                "recorded by its review task"
                                + (
                                    f" and reopened {len(requeued)} task(s)."
                                    if requeued
                                    else " (no task could be reopened)."
                                )
                            ),
                            {
                                "reconciled": True,
                                "recommendation": "NEEDS_REWORK",
                                "requeued_task_ids": requeued,
                            },
                            org_id=request_org_id,
                        )
                        fixed += 1
                else:
                    # No usable decision on record: a human has to decide.
                    # Emit the marker once so the stall is observable rather
                    # than looking like work that is still in progress.
                    existing = await conn.fetchval(
                        """SELECT 1 FROM task_events
                            WHERE task_id = $1
                              AND event_type = 'request_review_manual_required'""",
                        review_task["id"],
                    )
                    if not existing:
                        await self.add_task_event(
                            str(review_task["id"]),
                            "request_review_manual_required",
                            "Request is in review with no dispatchable work and "
                            "no recorded review decision; a human decision is "
                            "required.",
                            {"reconciled": True, "reason": "no_recorded_decision"},
                            org_id=request_org_id,
                        )
                        fixed += 1

        return fixed

    # ── Dashboard queries ─────────────────────────────────────────────────

    async def get_active_summary(
        self,
        org_id: str,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> dict:
        """Quick dashboard stats."""
        params: list[Any] = [UUID(org_id)]
        visibility = ""
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            visibility = " AND " + self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            req_stats = await conn.fetchrow(
                f"""SELECT
                     COUNT(*) as total,
                     COUNT(*) FILTER (
                         WHERE status IN ('in_progress', 'review', 'needs_rework')
                     ) as active,
                     COUNT(*) FILTER (WHERE status = 'pending') as pending,
                     COUNT(*) FILTER (WHERE status = 'completed') as completed
                         FROM requests r WHERE r.organization_id = $1{visibility}""",
                     *params,
            )
            task_stats = await conn.fetchrow(
                     f"""SELECT
                     COUNT(*) as total,
                            COUNT(*) FILTER (WHERE t.status IN ('claimed', 'running')) as running,
                            COUNT(*) FILTER (WHERE t.status IN ('pending', 'planned')) as queued,
                            COUNT(*) FILTER (WHERE t.status = 'completed') as completed
                         FROM tasks t
                         JOIN requests r ON r.id = t.request_id
                         WHERE t.organization_id = $1{visibility}""",
                     *params,
            )
        return {
            "requests": dict(req_stats) if req_stats else {},
            "tasks": dict(task_stats) if task_stats else {},
        }

    async def get_recent_events(
        self,
        org_id: str,
        limit: int = 50,
        requester_user_id: str | None = None,
        include_system: bool = False,
    ) -> list[dict]:
        """Get recent events across all tasks for the activity feed."""
        params: list[Any] = [UUID(org_id)]
        visibility = ""
        if requester_user_id is not None:
            params.extend([UUID(requester_user_id), include_system])
            visibility = " AND " + self._request_visibility_condition("$2", "$3")
        params.append(limit)
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            rows = await conn.fetch(
                f"""SELECT te.*, t.title as task_title, t.agent_type,
                          r.title as request_title, r.id as request_id
                   FROM task_events te
                   JOIN tasks t ON te.task_id = t.id
                   JOIN requests r ON t.request_id = r.id
                   WHERE t.organization_id = $1{visibility}
                   ORDER BY te.created_at DESC LIMIT ${len(params)}""",
                *params,
            )
        return [dict(r) for r in rows]

    async def count_pending_approvals(
        self,
        org_id: str,
        requester_user_id: str,
        include_system: bool = False,
    ) -> int:
        """Count visible requests waiting at the pre-work approval gate."""
        visibility = self._request_visibility_condition("$2", "$3")
        async with scoped_acquire_on(
            self.pool, organization_id=org_id
        ) as conn:
            return int(
                await conn.fetchval(
                    f"""SELECT COUNT(*) FROM requests r
                        WHERE r.organization_id = $1
                          AND {visibility}
                          AND r.approval_status = 'pending_approval'
                          AND r.status NOT IN ('cancelled', 'rejection_processing')""",
                    UUID(org_id),
                    UUID(requester_user_id),
                    include_system,
                )
                or 0
            )
