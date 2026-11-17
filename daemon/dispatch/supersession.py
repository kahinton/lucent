"""Provenance for task attempts replaced by a corrective continuation.

A task attempt that misses required tool work gets one scoped continuation
(see ``_build_missing_tool_continuation_prompt``). When that continuation
produces a result, the first attempt is *superseded*: readers treat
``tasks.result`` as the terminal answer, so it must contain only the final
attempt's text. The superseded text is still valuable evidence (it is often
where the model recorded a false "tool unavailable" claim), so it is preserved
verbatim in a dedicated task event and referenced from a compact header above
the final text.

Every helper here is pure or takes its I/O as callables so the invariants can
be tested without a live daemon or database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

# Task-event type: task_events.event_type is VARCHAR(32) and
# "superseded_output" is 17 chars. The replaced pass's full text lives in this
# event's metadata as {"attempt": int, "reason": {...}, "chars": int, "text": str}.
SUPERSEDED_EVENT_TYPE = "superseded_output"

# Events written under the earlier name stay readable, so a retried
# finalization recognizes an already-recorded attempt instead of duplicating it.
LEGACY_SUPERSEDED_EVENT_TYPES = ("result_superseded",)

# Marker used to make header application idempotent.
_HEADER_RE = re.compile(r"^\[superseded attempt \d+ of \d+\]")
_HEADER_PREFIX = "[superseded attempt "

# Task-event type for the *replacement* itself (structured-output repair). The
# displaced text is preserved under SUPERSEDED_EVENT_TYPE above, so a reader of
# the trail sees preservation first and the replacement transition after it.
STRUCTURED_REPAIR_EVENT_TYPE = "structured_output_repaired"

# Leading line added to `tasks.result` when a repair session replaced the
# model's answer with extracted JSON. Its job is to stop a reader of only
# `result` from concluding the model produced that JSON itself.
_REPAIR_NOTICE_PREFIX = "[structured output repaired]"
_REPAIR_NOTICE_RE = re.compile(r"^\[structured output repaired\]")

# Where the superseded attempt's full text is retrievable. The durable
# task-event store is the current answer; keep this as the single value to
# change if that location ever moves.
SUPERSEDED_TEXT_LOCATION = f"task_event:{SUPERSEDED_EVENT_TYPE}"

REASON_MISSING_REQUIRED_TOOLS = "missing_required_tools"
REASON_MISSING_OPERATIONAL_TOOL_WORK = "missing_operational_tool_work"
REASON_STRUCTURED_OUTPUT_REPAIR = "structured_output_repair"


@dataclass(frozen=True)
class SupersededAttempt:
    """A finished attempt whose text was replaced by a continuation."""

    text: str
    attempt: int = 1
    total_attempts: int = 2
    reason: str = REASON_MISSING_REQUIRED_TOOLS
    missing_tools: tuple[str, ...] = ()
    event_id: str | None = None

    def with_event(self, event_id: str | None) -> "SupersededAttempt":
        return replace(self, event_id=event_id)


def build_superseded_header(superseded: SupersededAttempt) -> str:
    """Return a compact machine- and human-readable provenance header.

    Deliberately one line and free of phrasing the completion validator reads
    as a failure signal (``failed to``, ``unable to``, ``missing required
    tooling``) — the header is prepended to a *successful* final attempt.
    """
    if superseded.missing_tools:
        reason = REASON_MISSING_REQUIRED_TOOLS
        missing = ",".join(superseded.missing_tools)
    else:
        reason = superseded.reason
        missing = ""
    if superseded.event_id:
        source = f"{SUPERSEDED_TEXT_LOCATION}:{superseded.event_id}"
    else:
        source = f"{SUPERSEDED_TEXT_LOCATION}:attempt={superseded.attempt}"

    fields = [
        f"attempt={superseded.attempt}/{superseded.total_attempts}",
        f"reason={reason}",
    ]
    if missing:
        fields.append(f"missing_tools={missing}")
    fields.append(f"source={source}")
    return (
        f"{_HEADER_PREFIX}{superseded.attempt} of {superseded.total_attempts}] "
        + "; ".join(fields)
        + ". Only the final attempt's text follows."
    )


def apply_superseded_header(
    final_text: str | None,
    superseded: SupersededAttempt | None,
) -> str | None:
    """Return the text to persist as ``tasks.result``.

    With no superseded attempt the input is returned byte-identically — no
    header, no reflow, and ``None`` stays ``None``. An empty prior attempt
    superseded nothing, so it also yields no header and no leading separator.
    Applying the same header twice is a no-op.
    """
    if superseded is None or not (superseded.text or "").strip():
        return final_text
    text = final_text or ""
    if _HEADER_RE.match(text):
        return text
    return f"{build_superseded_header(superseded)}\n\n{text}"


def build_repair_notice(displaced: SupersededAttempt | None) -> str:
    """Return the provenance line for an answer produced by output repair.

    Deliberately states that the text was *not* the model's answer, so someone
    reading only ``tasks.result`` cannot mistake the repair JSON for it.
    """
    source = ""
    if displaced is not None and (displaced.text or "").strip():
        if displaced.event_id:
            source = (
                " The displaced answer is preserved verbatim at "
                f"{SUPERSEDED_TEXT_LOCATION}:{displaced.event_id}."
            )
        else:
            source = (
                " The displaced answer is preserved verbatim at "
                f"{SUPERSEDED_TEXT_LOCATION}:attempt={displaced.attempt}."
            )
    return (
        f"{_REPAIR_NOTICE_PREFIX} The model's own answer did not match this "
        "task's JSON Schema, so the text below was produced by a repair "
        "session extracting structured data, not by the model as its answer."
        + source
    )


def apply_repair_notice(
    text: str | None,
    displaced: SupersededAttempt | None,
    *,
    replaced: bool = False,
) -> str | None:
    """Prepend the repair provenance notice after any superseded header.

    ``replaced`` is explicit (rather than inferred from ``displaced``) because
    a repair can replace an empty answer, where there is nothing to preserve
    but the replacement must still be visible. When ``replaced`` is false the
    input is returned byte-identically. Applying twice is a no-op.
    """
    if not replaced:
        return text
    notice = build_repair_notice(displaced)
    current = text or ""
    if _REPAIR_NOTICE_RE.match(current):
        return current
    if _HEADER_RE.match(current):
        header, separator, remainder = current.partition("\n\n")
        if separator:
            if not remainder:
                return f"{header}{separator}{notice}"
            if _REPAIR_NOTICE_RE.match(remainder):
                return current
            return f"{header}{separator}{notice}\n\n{remainder}"
    return f"{notice}\n\n{current}" if current else notice


def build_repair_replacement_summary(displaced: SupersededAttempt | None) -> str:
    """One-line terminal state for ``tasks.result_summary`` after a repair.

    Names what actually happened instead of leaving a summary derived from the
    repair JSON looking like the model's own answer.
    """
    summary = (
        "Structured output was repaired: the model's answer did not match the "
        "task's JSON Schema, so a repair session produced the structured payload."
    )
    if displaced is not None and (displaced.text or "").strip():
        if displaced.event_id:
            summary += (
                " The pre-repair answer is preserved at "
                f"{SUPERSEDED_TEXT_LOCATION}:{displaced.event_id}."
            )
        else:
            summary += (
                " The pre-repair answer is preserved at "
                f"{SUPERSEDED_TEXT_LOCATION}:attempt={displaced.attempt}."
            )
    else:
        summary += " No pre-repair text was present."
    return summary


def superseded_event_metadata(superseded: SupersededAttempt) -> dict[str, Any]:
    """Full, lossless payload for the durable superseded-attempt event."""
    return {
        "attempt": superseded.attempt,
        "reason": {
            "code": superseded.reason,
            "missing_tools": list(superseded.missing_tools),
        },
        "chars": len(superseded.text),
        "text": superseded.text,
    }


def superseded_event_detail(superseded: SupersededAttempt) -> str:
    """Short human-readable event detail; the full text lives in metadata."""
    return (
        f"Attempt {superseded.attempt} of {superseded.total_attempts} was "
        f"superseded ({superseded.reason}"
        + (f": {', '.join(superseded.missing_tools)}" if superseded.missing_tools else "")
        + f"); its full text is preserved in this {SUPERSEDED_EVENT_TYPE} "
        "event's metadata."
    )


def build_terminal_summary(
    superseded: SupersededAttempt,
    *,
    succeeded: bool,
    satisfied_tools: list[str] | tuple[str, ...] = (),
) -> str:
    """One-line terminal state for ``tasks.result_summary``.

    Example: "Final attempt 2 of 2 succeeded; send_handoff called. Attempt 1
    missed required tool and is superseded."
    """
    final_attempt = superseded.attempt + 1
    head = (
        f"Final attempt {final_attempt} of {superseded.total_attempts} "
        + ("succeeded" if succeeded else "did not satisfy required tool work")
    )
    if succeeded and satisfied_tools:
        head += f"; {', '.join(sorted(satisfied_tools))} called"
    noun = "tool" if len(superseded.missing_tools) == 1 else "tools"
    tail = f"Attempt {superseded.attempt} missed required {noun}"
    if superseded.missing_tools:
        tail += f" ({', '.join(superseded.missing_tools)})"
    tail += " and is superseded."
    return f"{head}. {tail}"


def _as_metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def find_superseded_event(
    events: list[dict] | tuple[dict, ...] | None,
    attempt: int,
    *,
    text: str | None = None,
) -> dict | None:
    """Return the already-recorded event for ``attempt``, if one exists.

    ``text`` narrows the match to an event that preserved that exact text. A
    re-dispatched task keeps events from its earlier lifecycle under the same
    attempt number; matching on text keeps a *new* displaced answer from being
    silently dropped in favour of the stale record.
    """
    accepted_types = (SUPERSEDED_EVENT_TYPE, *LEGACY_SUPERSEDED_EVENT_TYPES)
    for event in events or []:
        if str(event.get("event_type") or "") not in accepted_types:
            continue
        metadata = _as_metadata_dict(event.get("metadata"))
        try:
            if int(metadata.get("attempt")) != int(attempt):
                continue
        except (TypeError, ValueError):
            continue
        if text is not None and metadata.get("text") != text:
            continue
        return event
    return None


async def record_superseded_attempt(
    superseded: SupersededAttempt,
    *,
    list_events: Callable[[], Awaitable[list[dict]]],
    add_event: Callable[[str, str, dict[str, Any]], Awaitable[dict | None]],
    match_text: bool = False,
) -> dict | None:
    """Persist the superseded attempt exactly once and return its event row.

    ``list_events``/``add_event`` are injected so callers can use the daemon's
    HTTP client (or fakes in tests). Re-running finalization for the same
    attempt returns the existing event instead of emitting a duplicate. With
    ``match_text`` the existing event must also preserve the same text, so a
    stale event from an earlier lifecycle cannot stand in for new text.
    """
    existing = find_superseded_event(
        await list_events(),
        superseded.attempt,
        text=superseded.text if match_text else None,
    )
    if existing is not None:
        return existing
    return await add_event(
        SUPERSEDED_EVENT_TYPE,
        superseded_event_detail(superseded),
        superseded_event_metadata(superseded),
    )
