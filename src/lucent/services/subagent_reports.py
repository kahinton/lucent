"""Push background task and request results into the conversation that caused them.

A chat session that delegates work becomes a supervisor: the person expects to be
told when a task finishes and when the request as a whole reaches a decision
point, without having to ask or reload. Three things make that real:

* **Signal** — migration 145 publishes content-free ``lucent_subagent_report``
  notifications. This service consumes them on its own LISTEN connection.
* **Card** — a durable, visible ``subagent_report`` event is appended to the
  session transcript, so the report survives reload and reads as part of the
  conversation rather than as transient UI chrome.
* **Turn** — Lucent answers. The reply is a real assistant message in the
  thread, generated with no tools (an unattended turn must not have side
  effects) and without provider resume (the reply would otherwise be missing
  from a resumed provider history).

Everything is best-effort by design: a report that cannot be produced must
never break chat, and a failed reply must never lose the card.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
import json
import os
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from lucent.constants import (
    INTERRUPTIBLE_TASK_STATUSES,
    SUBAGENT_REPORT_LOCK_NAMESPACE,
    TERMINAL_TASK_STATUSES,
)
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.pool import scoped_acquire
from lucent.db.requests import RequestRepository
from lucent.logging import get_logger
from lucent.services.delegated_work import (
    request_status_label,
    summarize_report_event,
    task_status_label,
)

logger = get_logger(__name__)

REPORT_CHANNEL = "lucent_subagent_report"
REPORT_EVENT_TYPE = "subagent_report"
REPORT_REPLY_KIND = "subagent_report_reply"

# Coalesce a burst of transitions (a task finishing usually moves its request
# too) into a single report rather than a card per row.
DEBOUNCE_SECONDS = float(os.environ.get("LUCENT_SUBAGENT_REPORT_DEBOUNCE_SECONDS", "8"))
# One generated reply per session per window. Every distinct outcome still gets
# a durable card, and anything suppressed by this window is re-armed through
# ``_defer_reply`` rather than dropped — so this is purely a cost control and
# never a correctness rule. A request's terminal outcome bypasses it entirely.
# Kept well above the ~40-55s review→completed gap so the two do not compete for
# the single slot, and because a collaboration reply arriving a minute or two
# late is far better than a wrong or missing one.
REPLY_MIN_INTERVAL_SECONDS = float(
    os.environ.get("LUCENT_SUBAGENT_REPORT_REPLY_INTERVAL_SECONDS", "180")
)
# Do not start a turn on a conversation nobody has touched in a long time. The
# card still lands; the person sees it next time they open the thread.
REPLY_MAX_AGE_HOURS = float(os.environ.get("LUCENT_SUBAGENT_REPORT_MAX_AGE_HOURS", "24"))
# A user turn in flight wins. Wait this long for it to finish, then card-only.
REPLY_TURN_WAIT_SECONDS = float(
    os.environ.get("LUCENT_SUBAGENT_REPORT_TURN_WAIT_SECONDS", "20")
)
REPLY_HISTORY_LIMIT = 20
# Fallback only. The live value comes from the organization's
# ``chat.timeout_seconds`` so a report turn is bounded the same way a normal
# chat turn is — an unattended turn must not outlive the interactive setting.
REPLY_TIMEOUT_SECONDS = int(os.environ.get("LUCENT_SUBAGENT_REPORT_TIMEOUT_SECONDS", "300"))
REPLY_MAX_CHARS = 1200
_MAX_REPORT_TEXT = 1600
# How many times a reply may be re-deferred before it is abandoned. Unbounded
# retries on a permanently-broken session (no model, revoked access) would spin
# forever; a handful of attempts covers transient causes (a busy conversation,
# a momentary engine failure) without turning a durable card into a task queue.
REPLY_MAX_DEFERRALS = int(os.environ.get("LUCENT_SUBAGENT_REPORT_MAX_DEFERRALS", "6"))

REPORT_SYSTEM_PREAMBLE = """\
You are Lucent, continuing a conversation in which you delegated work to the daemon.

A background sub-agent just reported. Write the short message you would naturally
say to the person now — as their collaborator, not as a status system.

Rules:
- Lead with what the work PRODUCED, quoting the concrete result below. The person
  asked for an outcome, not a status.
- Use the reported result, not speculation. If it failed, say so directly and name
  the concrete next step.
- Do not list internal IDs, task statuses as statuses, or tool names.
- Do not call tools. This turn is a report, not a work session.
- Keep it under 150 words. No greeting, no sign-off.
- NEVER describe the result as missing, thin, empty, or unavailable when the
  REPORTED RESULT below contains work output. Quote the finding.
- The REPORTED RESULT is the authoritative content of THIS report. Earlier turns
  may have said a report was empty; that referred to an earlier report and must
  not be repeated.
- "The work finished" and "the request was approved" are different milestones and
  both are reported. When the request is complete, say plainly that it is finished
  and approved — do not leave the person wondering whether a further approval step
  is still owed to them.

--- REPORTED RESULT ---
"""


def _is_terminal_request_status(status: str | None) -> bool:
    """Whether a request status is a final outcome worth a reply.

    ``review`` is deliberately excluded: it is Lucent's own post-completion
    quality gate, which runs automatically and resolves within seconds. It is an
    internal step, not a decision the person makes, so answering it produces a
    reply that describes work as unfinished moments before it completes.
    """
    return str(status or "") in {"completed", "failed", "cancelled"}


def _as_uuid(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _clip(text: str | None, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


class SubagentReportService:
    """Consume delegated-work notifications and report them into their session."""

    def __init__(self) -> None:
        self._connection: asyncpg.Connection | None = None
        self._task: asyncio.Task | None = None
        self._database_url: str | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_reply_at: dict[str, float] = {}
        # Reports whose card was written but whose reply was suppressed by the
        # reply interval. Without a retry these are answered never.
        self._reply_pending: dict[str, dict[str, Any]] = {}
        # Re-deferral counters per session, so a permanently unreachable session
        # cannot retry forever.
        self._reply_deferrals: dict[str, int] = {}
        self._debounce_task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

    # -- lifecycle -----------------------------------------------------

    async def start(self, database_url: str | None = None) -> None:
        """Attach the LISTEN connection and start the consumer loop."""
        if self._task and not self._task.done():
            return
        self._database_url = database_url or os.environ.get("DATABASE_URL")
        if not self._database_url:
            logger.info("Subagent reports disabled: no database URL")
            return
        self._shutdown.clear()
        self._task = asyncio.create_task(self._run(), name="subagent-reports")

    async def stop(self) -> None:
        """Detach the listener and let the consumer finish."""
        self._shutdown.set()
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        connection, self._connection = self._connection, None
        if connection and not connection.is_closed():
            try:
                await connection.remove_listener(REPORT_CHANNEL, self._on_notification)
                await connection.close()
            except Exception:
                logger.debug("Failed to close report listener", exc_info=True)

    # -- listener ------------------------------------------------------

    async def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._connect()
                # The LISTEN connection delivers callbacks; idle here until
                # shutdown so the task stays alive without polling.
                await self._shutdown.wait()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Subagent report listener failed; retrying", exc_info=True)
                await asyncio.sleep(15)

    async def _connect(self) -> None:
        connection = await asyncpg.connect(self._database_url)
        await connection.add_listener(REPORT_CHANNEL, self._on_notification)
        self._connection = connection
        logger.info("Subagent report listener connected on %s", REPORT_CHANNEL)

    @staticmethod
    def _pending_key(signal: dict[str, Any]) -> str:
        """Coalescing key for one pending outcome.

        Keyed on *outcome*, not session: a task finishing usually moves its
        request too, and those are two distinct reports a person wants to see.
        Keying on the session alone silently dropped one of them.
        """
        return (
            f"{signal['session_id']}:{signal['request_id']}:"
            f"{signal.get('task_id') or ''}:{signal.get('kind', '')}"
        )

    def _on_notification(self, _conn, _pid, _channel, payload: str) -> None:
        """Queue a report signal (sync callback — must not block)."""
        try:
            event = json.loads(payload)
            session_id = str(event["session_id"])
            request_id = str(event["request_id"])
        except (TypeError, ValueError, KeyError):
            logger.warning("Ignoring malformed subagent report payload")
            return
        org_id = str(event.get("organization_id") or "")
        if not org_id:
            return
        task_id = event.get("task_id")
        kind = str(event.get("kind") or "")
        signal = {
            "session_id": session_id,
            "request_id": request_id,
            "task_id": str(task_id) if task_id else None,
            "kind": kind,
            "organization_id": org_id,
            "user_id": str(event["user_id"]) if event.get("user_id") else None,
        }
        self._pending[self._pending_key(signal)] = signal
        if self._debounce_task is None or self._debounce_task.done():
            self._debounce_task = asyncio.create_task(
                self._flush_after_debounce(), name="subagent-reports-flush"
            )

    async def _flush_after_debounce(self) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        pending, self._pending = self._pending, {}

        # A finished task almost always moves its request in the same instant.
        # Reporting both would stack two cards (and two replies) that say the
        # same thing, so one burst becomes one report: the request-level outcome
        # is the headline and the task detail rides along in the body.
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for signal in pending.values():
            by_session[signal["session_id"]].append(signal)

        for session_id, signals in by_session.items():
            try:
                await self._handle_batch(signals)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Subagent report handling failed for session %s; re-queuing",
                    session_id,
                    exc_info=True,
                )
                # Put the burst back so it is retried instead of lost. A failure
                # here is usually transient (a database blip, a lock timeout), and
                # the card this would have produced is the only record the person
                # gets — dropping it silently is the worst possible outcome.
                for signal in signals:
                    key = self._pending_key(signal)
                    self._pending.setdefault(key, signal)

        # Handling a burst can take a while (it generates a reply turn), and new
        # signals that arrive meanwhile land in the fresh _pending dict without
        # being able to schedule their own flush — this task is still running, so
        # _on_notification's "is the flush done?" check says no. Without this
        # re-arm, anything that arrives mid-flush sits unflushed indefinitely.
        if self._pending and not self._shutdown.is_set():
            self._debounce_task = asyncio.create_task(
                self._flush_after_debounce(), name="subagent-reports-flush"
            )

    # -- deferred replies ----------------------------------------------

    def _defer_reply(
        self,
        session_id: str,
        *,
        signal: dict[str, Any],
        card: dict[str, Any],
        context: dict[str, Any],
        event_row: dict[str, Any],
        reason: str,
    ) -> bool:
        """Park a reply that could not be generated now, and re-arm it.

        Every path that cannot answer immediately must come through here. The
        card is already durable, so nothing would ever retry it: a drop here is
        permanent silence about work the person is waiting on. Returning False
        means the retry budget is spent and the silence is accepted (the card
        remains the record).
        """
        attempts = self._reply_deferrals.get(session_id, 0)
        if attempts >= REPLY_MAX_DEFERRALS:
            logger.warning(
                "Subagent report for session %s: giving up on the reply after "
                "%s deferrals (last reason: %s); the card remains in the transcript",
                session_id,
                attempts,
                reason,
            )
            return False
        self._reply_deferrals[session_id] = attempts + 1
        self._reply_pending[session_id] = {
            "signal": signal,
            "card": card,
            "context": context,
            "event_row": event_row,
        }
        self._schedule_deferred_reply_retry(session_id)
        logger.info(
            "Subagent report for session %s: reply deferred (%s, attempt %s/%s)",
            session_id,
            reason,
            attempts + 1,
            REPLY_MAX_DEFERRALS,
        )
        return True

    def _schedule_deferred_reply_retry(self, session_id: str) -> None:
        """Answer a suppressed report once the blocking condition has cleared.

        ``_reply_too_soon`` used to drop the reply outright, and nothing ever
        retried: the card was already written, so no further notification would
        arrive for it. A request's completion could therefore go unacknowledged
        forever while an interim notice got the single reply. This re-arms the
        answer after the window.
        """
        delay = max(1.0, REPLY_MIN_INTERVAL_SECONDS + 1.0)

        async def _retry() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            entry = self._reply_pending.get(session_id)
            if not entry or self._shutdown.is_set():
                return
            if self._awaiting_user_turn(session_id):
                # Still busy — re-arm through the shared helper so the re-defer
                # is counted and cannot loop forever.
                self._defer_reply(
                    session_id,
                    signal=entry["signal"],
                    card=entry["card"],
                    context=entry["context"],
                    event_row=entry["event_row"],
                    reason="user turn still active",
                )
                return
            self._reply_pending.pop(session_id, None)
            try:
                await self._maybe_generate_reply(
                    entry["signal"],
                    entry["card"],
                    entry["context"],
                    entry["event_row"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Deferred subagent reply failed for session %s",
                    session_id,
                    exc_info=True,
                )

        asyncio.create_task(_retry(), name="subagent-report-deferred-reply")

    @staticmethod
    def _signal_identity(signal: dict[str, Any]) -> str:
        """A stable identity for one contributing notification.

        Every notification is already unique: migration 145's trigger publishes
        exactly one signal per (session, request, task, status transition), and
        the debounce key in ``_on_notification`` keeps at most one of each per
        burst. Replaying the same transition (a trigger re-fire after a
        reconnect) yields the same identity, which is what makes de-duplication
        still work.
        """
        return f"{signal.get('kind', '')}:{signal.get('task_id') or ''}"

    @staticmethod
    def _state_fingerprint(signal: dict[str, Any], context: dict[str, Any]) -> str:
        """Observable state as-of this burst, for signals with no task id.

        Request-level notifications carry no per-event id, so ``kind`` alone
        cannot distinguish a second ``request.review`` (after a rework cycle)
        from a replay of the first. The re-read state can: a genuine replay
        lands on the same rows and produces the same fingerprint, while a later
        transition that changed the request's contents produces a different one.
        """
        counts = context.get("counts") or {}
        request = context.get("request") or {}
        parts = [
            str(request.get("status") or ""),
            str(counts.get("total") or 0),
            str(counts.get("completed") or 0),
            str(counts.get("failed") or 0),
            str(counts.get("running") or 0),
            str(counts.get("queued") or 0),
        ]
        return ":".join(parts)

    @classmethod
    def _burst_identity(
        cls,
        signals: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> str:
        """Identity of a whole burst — the *set of events*, not of kinds.

        This used to hash only the sorted set of kinds, so two genuinely
        distinct bursts that happened to share a kind set (e.g. two separate
        ``task.completed`` bursts on the same request) produced the same key and
        the second one was silently dropped. Including each signal's own
        identity means a burst can only collide with a replay of itself.
        """
        parts = [cls._signal_identity(s) for s in signals]
        if context is not None:
            for signal in signals:
                if not signal.get("task_id"):
                    parts.append(
                        f"{signal.get('kind', '')}#"
                        f"{cls._state_fingerprint(signal, context)}"
                    )
        return "|".join(sorted(parts))

    # -- reporting -----------------------------------------------------

    @staticmethod
    def _rank(signal: dict[str, Any]) -> int:
        """Higher wins when collapsing a burst into one report.

        Ordered by how final the outcome is, NOT by how urgent it feels.
        ``cancelled`` must outrank ``review``: a burst carrying both means the
        request was reviewed and then cancelled (or vice versa) in one window,
        and the surviving card must describe where it actually ended up. Ranking
        ``review`` above ``cancelled`` produced a card that said "checking the
        finished work" for a cancelled request — a contradiction the reader
        cannot resolve.
        """
        rank = {
            "request.completed": 100,
            "request.failed": 97,
            "request.cancelled": 95,
            "request.needs_rework": 90,
            "request.review": 85,
            "task.needs_review": 60,
            "task.failed": 57,
            "task.cancelled": 55,
            "task.completed": 50,
        }
        return rank.get(signal.get("kind", ""), 0)

    async def _handle_batch(self, signals: list[dict[str, Any]]) -> None:
        if not signals:
            return
        signals = sorted(signals, key=self._rank, reverse=True)
        primary = signals[0]
        request_ids = {s["request_id"] for s in signals}
        if len(request_ids) > 1:
            # Distinct requests in one burst: report them separately so neither
            # is swallowed. Bounded because a burst is one debounce window.
            for signal in signals:
                await self._handle_one(signal)
            return
        async with self._session_locks[primary["session_id"]]:
            context = await self._load_context(primary)
            if not context:
                return
            card = self._build_card(primary, context)
            extras = [
                self._event_phrase(s)
                for s in signals[1:]
                if self._adds_information(primary, s)
            ]
            if extras:
                card["body"] = " ".join(
                    part
                    for part in [card["body"], "Also: " + "; ".join(extras) + "."]
                    if part
                )
            # The dedupe key covers the whole burst, so a replayed notification
            # cannot produce a duplicate card — while a *different* burst that
            # happens to carry the same kinds still gets its own card.
            card["dedupe_key"] = (
                f"{primary['request_id']}:"
                f"{self._burst_identity(signals, context)}"
            )
            event_row = await self._write_card(primary, card, context)
            if event_row is None:
                # The card is a duplicate, but a reply owed for an earlier
                # suppressed burst may never have been generated. Suppressing a
                # duplicate card must not also swallow that reply.
                await self._reply_for_suppressed_burst(primary, card, context)
                return
            await self._maybe_generate_reply(primary, card, context, event_row)

    @staticmethod
    def _adds_information(primary: dict[str, Any], other: dict[str, Any]) -> bool:
        """Whether a secondary event tells the person anything new.

        A request reaching a terminal state subsumes every task event in that
        same burst — "the request is complete" already implies its tasks
        finished, and listing them again reads as noise.
        """
        primary_kind = str(primary.get("kind", ""))
        other_kind = str(other.get("kind", ""))
        if not other_kind or other_kind == primary_kind:
            return False
        if primary_kind.startswith("request."):
            return False
        # A task-level headline does benefit from mentioning that the request as
        # a whole also moved, as long as that is a different fact.
        return other_kind.startswith("request.")

    @staticmethod
    def _event_phrase(signal: dict[str, Any]) -> str:
        kind = signal.get("kind", "")
        status = kind.split(".", 1)[1] if "." in kind else kind
        if kind.startswith("request."):
            return f"request {request_status_label(status)}"
        return f"task {task_status_label(status)}"

    async def _handle_one(self, signal: dict[str, Any]) -> None:
        async with self._session_locks[signal["session_id"]]:
            context = await self._load_context(signal)
            if not context:
                return
            card = self._build_card(signal, context)
            card["dedupe_key"] = (
                f"{signal['request_id']}:"
                f"{self._burst_identity([signal], context)}"
            )
            event_row = await self._write_card(signal, card, context)
            if event_row is None:
                await self._reply_for_suppressed_burst(signal, card, context)
                return
            await self._maybe_generate_reply(signal, card, context, event_row)

    async def _reply_for_suppressed_burst(
        self,
        signal: dict[str, Any],
        card: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Answer a burst whose card was de-duplicated, when no reply exists yet.

        Suppression is about the *card*, not the answer. Two paths used to
        return early on a suppressed card and lose the reply entirely: a burst
        replayed while its first reply turn was still rate-limited or blocked by
        an in-flight user turn (``_reply_too_soon`` / ``_awaiting_user_turn``),
        and any burst that matched an existing key. The person then saw the work
        finish with no acknowledgement, because the only card for it had already
        been written.

        The reply is generated only when this session's most recent report
        event has no reply attached, so a genuine duplicate (card *and* reply
        already delivered) stays silent.
        """
        session_id = context["session"]["id"]
        org_id = signal["organization_id"]
        try:
            repo = LLMSessionRepository(await _get_pool())
            events = await repo.list_events(
                session_id,
                org_id,
                user_id=context["session"].get("user_id"),
                limit=50,
                newest_first=True,
            )
        except Exception:
            logger.debug("Could not check report reply state", exc_info=True)
            return
        for row in events:
            if row.get("event_type") != REPORT_EVENT_TYPE:
                continue
            # Only the newest report matters: if it already has a reply, the
            # conversation is up to date and another turn would be noise.
            if row.get("message_id"):
                return
            # Answer from the card that is actually in the transcript, not from
            # the suppressed duplicate's freshly built one — the reply must
            # describe what the person can see.
            stored = row.get("raw") if isinstance(row.get("raw"), dict) else None
            await self._maybe_generate_reply(
                signal, stored or card, context, dict(row)
            )
            return

    async def _load_context(self, signal: dict[str, Any]) -> dict[str, Any] | None:
        """Re-read the real state. The notification payload is never trusted.

        Returns ``None`` only when the session genuinely no longer exists, or the
        conversation opted out of reports — both permanent, intentional no-report
        cases. Transient database failures RAISE instead: returning ``None`` for
        them used to drop the report entirely, so a momentary hiccup lost a card
        that nothing would ever regenerate.
        """
        org_id = signal["organization_id"]
        repo = RequestRepository(await _get_pool())
        try:
            async with scoped_acquire(organization_id=org_id) as conn:
                session_row = await conn.fetchrow(
                    """SELECT id::text AS id, kind, status, user_id::text AS user_id,
                              model, agent_definition_id::text AS agent_definition_id,
                              metadata, last_message_at
                       FROM llm_sessions
                       WHERE id = $1::uuid AND organization_id = $2::uuid""",
                    _as_uuid(signal["session_id"]),
                    _as_uuid(org_id),
                )
                if not session_row:
                    logger.info(
                        "Subagent report for session %s: session no longer exists",
                        signal["session_id"],
                    )
                    return None
                request_row = await conn.fetchrow(
                    """SELECT id::text AS id, title, status, created_by::text AS created_by
                       FROM requests
                       WHERE id = $1::uuid AND organization_id = $2::uuid""",
                    _as_uuid(signal["request_id"]),
                    _as_uuid(org_id),
                )
                task_row = None
                if signal.get("task_id"):
                    task_row = await conn.fetchrow(
                        """SELECT id::text AS id, title, status, agent_type,
                                  error, result_summary,
                                  -- Prefer the actual output. A remote executor
                                  -- reports a terse result_summary ("Completed on
                                  -- remote X in 0.3s") that would otherwise mask
                                  -- the host output the person asked for.
                                  left(COALESCE(NULLIF(result, ''), result_summary, ''), 900) AS result_excerpt,
                                  cancel_requested_at
                           FROM tasks
                           WHERE id = $1::uuid AND organization_id = $2::uuid""",
                        _as_uuid(signal["task_id"]),
                        _as_uuid(org_id),
                    )
                counts = await conn.fetchrow(
                    """SELECT count(*) FILTER (WHERE status IN ('claimed','running')) AS running,
                              count(*) FILTER (WHERE status = 'completed') AS completed,
                              count(*) FILTER (WHERE status = 'failed') AS failed,
                              count(*) FILTER (WHERE status IN ('pending','planned')) AS queued,
                              count(*) AS total
                       FROM tasks
                       WHERE request_id = $1::uuid
                         AND title <> 'Post-completion review'""",
                    _as_uuid(signal["request_id"]),
                )
                # A request-level report is the headline, but the person wants to
                # know what the work actually produced. Without this the reply
                # turn has nothing concrete to say and degrades into a status
                # sentence. Bounded so a large request cannot flood the prompt.
                result_rows = await conn.fetch(
                    """SELECT title, error,
                              left(COALESCE(NULLIF(result, ''), result_summary, ''), 600) AS result_excerpt
                       FROM tasks
                       WHERE request_id = $1::uuid
                         AND title <> 'Post-completion review'
                         AND status IN ('completed', 'failed', 'needs_review')
                       ORDER BY sequence_order, created_at
                       LIMIT 5""",
                    _as_uuid(signal["request_id"]),
                )
        except Exception:
            # Transient failure: let it propagate so the flush's per-session
            # guard retries the burst. Swallowing it here turned a momentary
            # database hiccup into a permanently lost report.
            logger.warning(
                "Failed to load report context for session %s; will retry",
                signal["session_id"],
                exc_info=True,
            )
            raise

        metadata = session_row["metadata"] if isinstance(session_row["metadata"], dict) else {}
        if metadata.get("subagent_reports_disabled"):
            logger.info(
                "Subagent report for session %s: reports disabled for this conversation",
                signal["session_id"],
            )
            return None
        return {
            "session": dict(session_row),
            "request": dict(request_row) if request_row else None,
            "task": dict(task_row) if task_row else None,
            "counts": dict(counts) if counts else {},
            "task_results": [dict(r) for r in result_rows],
            "repo": repo,
        }

    @staticmethod
    def _dedupe_key(signal: dict[str, Any]) -> str:
        """Legacy single-signal key. ``_build_card`` uses the burst identity."""
        return f"{signal['kind']}:{signal['request_id']}:{signal.get('task_id') or ''}"

    def _build_card(
        self, signal: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        request = context.get("request") or {}
        task = context.get("task") or {}
        counts = context.get("counts") or {}
        kind = signal["kind"]

        # The card describes the request's state as RE-READ, not as the signal
        # claimed. A burst can carry several transitions, and the signal that won
        # the ranking is not always the one that decided the final status — a
        # card whose headline says "review" while its own request block says
        # "cancelled" is unresolvable for the reader.
        authoritative_status = (
            str(request.get("status") or "") if kind.startswith("request.") else ""
        )
        headline_kind = (
            f"request.{authoritative_status}"
            if authoritative_status and not kind.startswith("task.")
            else kind
        )
        if headline_kind != kind:
            logger.info(
                "Report card headline follows the re-read status (%s) rather "
                "than the signal (%s) for request %s",
                headline_kind,
                kind,
                signal.get("request_id"),
            )

        headline = summarize_report_event(
            kind=headline_kind, request=request, task=task
        )
        details: list[str] = []
        if kind.startswith("task."):
            status = kind.split(".", 1)[1]
            if status in TERMINAL_TASK_STATUSES or status == "needs_review":
                excerpt = _clip(
                    task.get("result_excerpt") or task.get("error"), _MAX_REPORT_TEXT
                )
                if excerpt:
                    details.append(excerpt)
            if task.get("cancel_requested_at") and status in INTERRUPTIBLE_TASK_STATUSES:
                details.append("Stopping as requested.")
        else:
            # A request-level report is the headline, but the person still wants
            # to know what the work actually produced. Otherwise the reply turn
            # has nothing concrete to say and degrades into a status sentence.
            # NOTE: the result excerpts are appended BEFORE the status sentence
            # so a 1200-char reply prompt cannot truncate the result away and
            # leave only "the work is ready" — which produced replies that
            # correctly complained the report was empty.
            for result in context.get("task_results") or []:
                title = _clip(result.get("title"), 90)
                excerpt = _clip(
                    result.get("result_excerpt") or result.get("error"), 400
                )
                if excerpt:
                    details.append(f'"{title}": {excerpt}')
            if headline_kind == "request.review":
                details.append(
                    "This is Lucent's automatic post-completion check; it is not "
                    "a human decision and it resolves on its own."
                )
            elif headline_kind == "request.needs_rework":
                details.append("Review sent the work back for another pass.")
            elif headline_kind == "request.completed":
                details.append("All tasks finished and the result was approved.")
            elif headline_kind == "request.failed":
                details.append("The work stopped on a failed task.")
            elif headline_kind == "request.cancelled":
                details.append("The request was cancelled.")

        total = int(counts.get("total") or 0)
        if total:
            done = int(counts.get("completed") or 0)
            running = int(counts.get("running") or 0)
            summary = f"{done}/{total} tasks done"
            if running:
                summary += f", {running} running"
            details.append(summary + ".")

        status = headline_kind.split(".", 1)[1] if "." in headline_kind else ""
        return {
            "kind": headline_kind,
            "severity": _severity_for(status),
            "headline": headline,
            "body": " ".join(part for part in details if part).strip(),
            "request": {
                "id": request.get("id"),
                "title": request.get("title"),
                "status": request.get("status"),
                "status_label": request_status_label(request.get("status")),
            },
            "task": (
                {
                    "id": task.get("id"),
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "status_label": task_status_label(task.get("status")),
                }
                if task
                else None
            ),
            "dedupe_key": self._dedupe_key(signal),
            "url": f"/activity/{request.get('id')}" if request.get("id") else None,
        }

    async def _write_card(
        self,
        signal: dict[str, Any],
        card: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create or refresh the one visible card for this request.

        Cards are a projection of a request's CURRENT state, not an audit trail:
        the same row is updated as the request advances. Appending a card per
        status change is what left the thread showing "READY FOR REVIEW" for work
        that had already completed, because nothing ever retracted the earlier
        card.

        ``None`` means exactly one thing: this exact card content is already
        stored (a replay of the same outcome). A failure to WRITE raises, because
        the caller treats ``None`` as "nothing to do" and would otherwise discard
        a report that was never stored.
        """
        session = context["session"]
        org_id = signal["organization_id"]
        repo = LLMSessionRepository(await _get_pool())
        raw = {**card, "request_id": signal["request_id"], "report_kind": signal["kind"]}
        if signal.get("task_id"):
            raw["task_id"] = signal["task_id"]
        try:
            row, changed = await repo.upsert_report_card(
                session["id"],
                request_id=signal["request_id"],
                org_id=org_id,
                user_id=session.get("user_id"),
                detail=card["headline"],
                raw=raw,
            )
        except Exception:
            logger.warning(
                "Failed to persist subagent report card for session %s; retrying",
                session["id"],
                exc_info=True,
            )
            raise
        if not changed:
            logger.debug(
                "Subagent report for session %s: card for request %s already "
                "reflects this outcome",
                session["id"],
                signal["request_id"],
            )
            return None
        if row is None:
            return None
        if card.get("headline"):
            logger.info(
                "Subagent report card updated for session %s request %s: %s",
                session["id"],
                signal["request_id"],
                card["headline"],
            )
        return row

    async def _maybe_generate_reply(
        self,
        signal: dict[str, Any],
        card: dict[str, Any],
        context: dict[str, Any],
        event_row: dict[str, Any],
    ) -> None:
        """Answer in the thread when doing so is safe and useful."""
        session = context["session"]
        session_id = session["id"]
        org_id = signal["organization_id"]

        if str(session.get("kind") or "") != "chat":
            return
        if str(session.get("status") or "") in {"archived", "deleted"}:
            return
        if self._awaiting_user_turn(session_id):
            if not await self._wait_for_user_turn(session_id):
                # A busy conversation used to end the reply for good. Defer
                # instead: the person is actively here, which is exactly when
                # an acknowledgement matters most.
                self._defer_reply(
                    session_id,
                    signal=signal,
                    card=card,
                    context=context,
                    event_row=event_row,
                    reason="user turn still active",
                )
                return
        # Which outcomes earn a direct message. A request's meaningful
        # milestones are "the work finished" (review) and "it was approved"
        # (completed) — the person asked to be told about BOTH, so neither is a
        # rate-limit casualty. Intermediate bookkeeping (needs_rework on a step,
        # individual task rows) still rides the interval so a long request cannot
        # produce a message per row. The card is the streaming progress view; the
        # message is the substantive report.
        kind = str(signal.get("kind", ""))
        status = kind.split(".", 1)[-1]
        milestone_outcome = kind.startswith("request.") and status in {
            "review",
            "completed",
            "failed",
            "cancelled",
        }
        if self._reply_too_soon(session_id) and not milestone_outcome:
            self._defer_reply(
                session_id,
                signal=signal,
                card=card,
                context=context,
                event_row=event_row,
                reason=f"reply interval for {kind}",
            )
            return
        if _stale_beyond_age_hours(session.get("last_message_at")):
            logger.info(
                "Subagent report for session %s: conversation idle beyond %sh, "
                "card only (the person sees it when they return)",
                session_id,
                REPLY_MAX_AGE_HOURS,
            )
            return

        model = session.get("model")
        if not model:
            logger.info(
                "Subagent report for session %s: no model on the session, card only",
                session_id,
            )
            return
        user_id = session.get("user_id")
        if not user_id:
            logger.info(
                "Subagent report for session %s: no session owner, card only",
                session_id,
            )
            return

        from lucent.api.routers.chat import _can_user_access_model
        from lucent.llm import get_engine_for_model
        from lucent.settings import chat_timeout_seconds

        pool = await _get_pool()
        user_context = {"id": user_id, "organization_id": org_id}
        try:
            if not await _can_user_access_model(user_context, pool, model):
                logger.info(
                    "Subagent report for session %s: model %s no longer accessible",
                    session_id,
                    model,
                )
                return
        except Exception:
            # The access check itself failed — that is not evidence the model is
            # unavailable, so defer rather than silently dropping the reply.
            logger.warning(
                "Subagent report for session %s: model access check failed; "
                "deferring the reply",
                session_id,
                exc_info=True,
            )
            self._defer_reply(
                session_id,
                signal=signal,
                card=card,
                context=context,
                event_row=event_row,
                reason="model access check failed",
            )
            return

        repo = LLMSessionRepository(pool)
        try:
            # list_messages returns the oldest N, so take a generous window and
            # keep the tail — the report turn needs recent conversational
            # context, not the start of the thread.
            history = (
                await repo.list_messages(
                    session_id,
                    org_id,
                    user_id=user_id,
                    roles={"user", "assistant"},
                    limit=200,
                )
            )[-REPLY_HISTORY_LIMIT:]
        except Exception:
            # Without history the reply loses conversational continuity but can
            # still report the result, which is the point. Log it so a persistent
            # history failure is visible rather than merely degraded.
            logger.warning(
                "Subagent report for session %s: could not load history; "
                "replying without it",
                session_id,
                exc_info=True,
            )
            history = []

        try:
            reply_timeout = chat_timeout_seconds(organization_id=org_id)
        except Exception:
            logger.debug("Could not resolve chat timeout; using default", exc_info=True)
            reply_timeout = REPLY_TIMEOUT_SECONDS

        system_prompt = await self._build_system_message(session, org_id, card)
        engine = get_engine_for_model(model)
        await _acquire_session_lock(session_id, org_id)
        try:
            result = await engine.run_session(
                model=model,
                system_message=system_prompt,
                # The reported content is the *prompt*, not a trailing appendix
                # on the system message. Sending only the headline left the
                # actual result at the far end of a long system prompt, and the
                # reply reliably reported "the report came back empty" while the
                # card body carried the full output.
                prompt=(
                    f"{card['headline']}\n\n"
                    f"{card.get('body') or ''}".strip()
                ),
                mcp_config={},
                timeout=reply_timeout,
                # No tools: an unattended turn must not be able to act.
                message_history=[
                    {"role": m["role"], "content": m["content"]} for m in history
                ],
                # Never resume: this reply is not part of the provider history.
                provider_session_id=None,
                resume=False,
                approve_permissions=False,
                audit_context={
                    "source": "chat.subagent_report",
                    "organization_id": org_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "model": model,
                    "engine": engine.name,
                },
            )
        except Exception:
            logger.warning(
                "Subagent report reply failed for session %s; will retry",
                session_id,
                exc_info=True,
            )
            self._defer_reply(
                session_id,
                signal=signal,
                card=card,
                context=context,
                event_row=event_row,
                reason="reply turn failed",
            )
            return
        finally:
            await _release_session_lock(session_id, org_id)

        reply = _clip(result, REPLY_MAX_CHARS)
        if not reply:
            # An empty completion is usually a transient engine hiccup, not a
            # verdict. Retry rather than silently leaving the card unanswered.
            self._defer_reply(
                session_id,
                signal=signal,
                card=card,
                context=context,
                event_row=event_row,
                reason="reply turn returned nothing",
            )
            return
        # One turn id stitches the card and the reply into a single unit: the
        # transcript renders the card inside the reply's row, and a reload shows
        # the same pairing the stream produced.
        report_turn_id = str(uuid4())
        try:
            message = await repo.add_message(
                session_id,
                role="assistant",
                content=reply,
                org_id=org_id,
                user_id=user_id,
                turn_id=report_turn_id,
                metadata={
                    "kind": REPORT_REPLY_KIND,
                    "report_event_id": str(event_row.get("id") or ""),
                    "report_kind": signal["kind"],
                    "request_id": signal["request_id"],
                },
            )
        except Exception:
            logger.warning(
                "Failed to persist subagent report reply for session %s",
                session_id,
                exc_info=True,
            )
            return
        self._last_reply_at[session_id] = asyncio.get_running_loop().time()
        message_id = message.get("id")
        if message_id and event_row.get("id"):
            try:
                async with scoped_acquire(organization_id=org_id) as conn:
                    await conn.execute(
                        """UPDATE llm_session_events
                           SET message_id = $2, turn_id = $3
                           WHERE id = $1
                             AND session_id = $4
                             AND event_type = $5""",
                        _as_uuid(event_row["id"]),
                        _as_uuid(message_id),
                        _as_uuid(report_turn_id),
                        _as_uuid(session_id),
                        REPORT_EVENT_TYPE,
                    )
            except Exception:
                pass

    async def _build_system_message(
        self, session: dict[str, Any], org_id: str, card: dict[str, Any]
    ) -> str:
        """Compose the report turn's system prompt from the session's own agent."""
        parts: list[str] = []
        agent_id = session.get("agent_definition_id")
        if agent_id:
            try:
                from lucent.db.definitions import DefinitionRepository

                agent = await DefinitionRepository(await _get_pool()).get_agent(
                    agent_id, org_id
                )
                if agent and agent.get("content"):
                    parts.append(agent["content"])
            except Exception:
                logger.debug("Could not load agent content for report turn", exc_info=True)
        # The reported content rides in the *prompt* (see run_session above), so
        # the system message carries only the instructions. Appending the body
        # here as well would duplicate it and push it behind a long agent
        # definition, where it went unread.
        parts.append(REPORT_SYSTEM_PREAMBLE)
        return "\n\n".join(parts)

    # -- concurrency helpers -------------------------------------------

    @staticmethod
    def _awaiting_user_turn(session_id: str) -> bool:
        from lucent.api.routers.chat import active_chat_turn_count

        return active_chat_turn_count(session_id) > 0

    @staticmethod
    async def _wait_for_user_turn(session_id: str) -> bool:
        from lucent.api.routers.chat import active_chat_turn_count

        deadline = asyncio.get_running_loop().time() + REPLY_TURN_WAIT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.0)
            if active_chat_turn_count(session_id) == 0:
                return True
        return False

    def _reply_too_soon(self, session_id: str) -> bool:
        last = self._last_reply_at.get(session_id)
        if last is None:
            return False
        elapsed = asyncio.get_running_loop().time() - last
        return elapsed < REPLY_MIN_INTERVAL_SECONDS


def _severity_for(status: str) -> str:
    if status in {"failed", "needs_rework"}:
        return "attention"
    if status in {"completed"}:
        return "success"
    if status in {"review", "needs_review"}:
        return "review"
    if status in {"cancelled"}:
        return "muted"
    return "info"


async def _get_pool():
    from lucent.db import get_pool

    return await get_pool()


async def _acquire_session_lock(session_id: str, org_id: str) -> None:
    """Cross-replica mutual exclusion for one session's report turn."""
    from lucent.db.pool import scoped_acquire

    try:
        async with scoped_acquire(organization_id=org_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_lock($1, hashtext($2)::int)",
                SUBAGENT_REPORT_LOCK_NAMESPACE,
                session_id,
            )
    except Exception:
        logger.debug("Could not acquire report session lock", exc_info=True)


async def _release_session_lock(session_id: str, org_id: str) -> None:
    from lucent.db.pool import scoped_acquire

    try:
        async with scoped_acquire(organization_id=org_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_unlock($1, hashtext($2)::int)",
                SUBAGENT_REPORT_LOCK_NAMESPACE,
                session_id,
            )
    except Exception:
        logger.debug("Could not release report session lock", exc_info=True)


def _stale_beyond_age_hours(last_message_at: Any) -> bool:
    if not last_message_at:
        return False
    from datetime import datetime, timezone

    try:
        stamp = last_message_at
        if not isinstance(stamp, datetime):
            stamp = datetime.fromisoformat(str(stamp))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
    return age_hours > REPLY_MAX_AGE_HOURS


subagent_report_service = SubagentReportService()
