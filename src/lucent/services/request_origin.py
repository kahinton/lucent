"""Where a request actually came from — as a chain of linked hops.

``requests.source`` is a five-value enum — ``user``, ``api``, ``cognitive``,
``daemon``, ``schedule``. That answers "a person or the machine", never "which
background job". Someone reading an Activity page therefore sees "Lucent" and
has no way to tell whether a morning briefing, a memory-maintenance pass, or a
chat conversation produced the work.

A prose sentence ("created by the scheduled Learning Extraction workflow") still
leaves the reader unable to *get there*. What they want is the work chain: the
workflow, the request it fired, the task that ran, the conversation — each hop
clickable.

The provenance is recorded, just spread over several tables:

* ``schedule_runs.request_id`` — a workflow links the run it materialised to the
  request it created. Authoritative workflow lineage.
* ``requests.origin_session_id`` — a chat session stamps the conversation that
  authored the request (migration 075).
* ``tool_call_audit_log`` — every ``create_request`` tool call, with the calling
  surface in ``source`` (``daemon.memory_maintenance_fanout``,
  ``chat.stream_v2``, …), the ``task_id``/``request_id``/``session_id`` it ran
  *under*, and the id of the request it created (first field of
  ``output_preview``).

The daemon case is the one that needs the task hop: a request with
``source='daemon'`` was created by a task, that task belongs to a parent
request, and the parent request owns the workflow run. Collapsing that to
"Lucent" throws away three real navigation targets.

Read-only and best-effort: every lookup failure degrades to a coarser chain
rather than hiding the request.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from lucent.db.pool import scoped_acquire_on

# ``create_request`` returns ``json.dumps({"id": ..., ...})`` as its payload and
# the audit log stores a redacted copy. The id is the first key, so an anchored
# match on the payload prefix is unambiguous — unlike searching the whole
# preview, which could hit an id mentioned inside the description.
_CREATED_REQUEST_RE = re.compile(r'^\{"id":\s*"([0-9a-fA-F-]{36})"')

# How a workflow reached the request. Reads as an adjective.
_TRIGGER_ADJECTIVES = {
    "schedule": "scheduled",
    "manual": "run manually",
    "webhook": "webhook",
    "integration_event": "integration event",
}

# Internal daemon surfaces, named the way the daemon's own logs name them.
_DAEMON_SURFACES = {
    "daemon.memory_maintenance_fanout": "memory maintenance",
    "daemon.task": "a background task",
    "daemon.task_completion_continuation": "a task follow-up",
}

# Who "asked" for the work, in the detail page's own words. The raw enum value
# is meaningless to a reader: "daemon" is not a person, and neither is
# "cognitive" — both are just Lucent.
_ACTOR_LABELS = {
    "user": "You",
    "api": "An API client",
    "cognitive": "Lucent",
    "daemon": "Lucent",
    "schedule": "Lucent",
}


def actor_label(source: str | None) -> str:
    """The person-facing name for a ``requests.source`` value."""
    return _ACTOR_LABELS.get(str(source or ""), "Lucent")


def _hop(
    *,
    kind: str,
    label: str,
    href: str | None = None,
    meta: str | None = None,
) -> dict[str, Any]:
    """One link in the work chain."""
    return {"kind": kind, "label": label, "href": href, "meta": meta}


def _origin(
    *,
    kind: str,
    label: str,
    source: str | None,
    chain: list[dict[str, Any]],
    detail: str | None = None,
) -> dict[str, Any]:
    # The primary link is the nearest ancestor — the first hop a reader would
    # click to go one step back.
    primary = next((hop for hop in chain if hop.get("href")), {})
    return {
        "kind": kind,
        "label": label,
        "actor": actor_label(source),
        "source": source,
        "detail": detail,
        "chain": chain,
        "url": primary.get("href"),
        "url_label": "Open where this came from" if primary.get("href") else None,
    }


def _created_request_id(output_preview: str | None) -> str | None:
    """Extract the id of the request a ``create_request`` call created.

    ``output_preview`` is a redacted JSON *string* (the audit layer stores text,
    not jsonb) and error payloads are plain sentences — hence the anchored
    pattern plus a tolerant parse instead of ``json.loads`` alone.
    """
    if not output_preview:
        return None
    match = _CREATED_REQUEST_RE.match(str(output_preview))
    if match:
        return match.group(1).lower()
    try:
        parsed = json.loads(output_preview)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("id"):
        return str(parsed["id"]).lower()
    return None


class RequestOriginService:
    """Resolve the originating work chain for one or many requests."""

    def __init__(self, pool):
        self.pool = pool

    async def resolve(
        self,
        request_id: str,
        *,
        org_id: str,
    ) -> dict[str, Any] | None:
        origins = await self.resolve_many([request_id], org_id=org_id)
        return origins.get(str(request_id))

    async def resolve_many(
        self,
        request_ids: Iterable[str],
        *,
        org_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return ``{request_id: origin}`` for every id that resolves.

        Missing entries are not an error — a request with no recoverable
        provenance simply has no chain to show.
        """
        ids = [str(rid) for rid in request_ids if rid]
        if not ids:
            return {}

        async with scoped_acquire_on(self.pool, organization_id=org_id) as conn:
            request_rows = await conn.fetch(
                """SELECT id::text AS id, source, title,
                          origin_session_id::text AS origin_session_id,
                          created_at
                   FROM requests
                   WHERE organization_id = $1::uuid AND id = ANY($2::uuid[])""",
                org_id,
                ids,
            )
            if not request_rows:
                return {}

            runs = await _workflow_runs(conn, org_id, ids)

            # ── calling surface (audit log) ──────────────────────────────
            # Bounded by the oldest request in this batch: an audit row is
            # written immediately after the request it created, so nothing
            # earlier can be relevant. Keeps the scan proportional to the page
            # rather than to the whole audit history.
            oldest = min(row["created_at"] for row in request_rows)
            audit_rows = await conn.fetch(
                """SELECT source, task_id::text AS task_id,
                          request_id::text AS request_id,
                          session_id::text AS session_id,
                          output_preview
                   FROM tool_call_audit_log
                   WHERE organization_id = $1::uuid
                     AND tool_name = 'create_request'
                     AND status = 'success'
                     AND created_at >= $2""",
                org_id,
                oldest,
            )
            wanted = set(ids)
            audit_by_created: dict[str, dict[str, Any]] = {}
            for row in audit_rows:
                created_id = _created_request_id(row["output_preview"])
                if created_id and created_id in wanted:
                    audit_by_created.setdefault(created_id, dict(row))

            # Sessions come from two places: the structured origin column, and
            # the audit row's session_id. The latter is the only surviving link
            # for older chat requests whose origin_session_id was never set.
            session_ids = {
                row["origin_session_id"]
                for row in request_rows
                if row["origin_session_id"]
            } | {
                row["session_id"]
                for row in audit_by_created.values()
                if row.get("session_id")
            }
            sessions = await _sessions(conn, org_id, session_ids)

            lineage = await _resolve_creating_context(
                conn, org_id, audit_by_created.values()
            )

        origins: dict[str, dict[str, Any]] = {}
        for row in request_rows:
            origins[row["id"]] = _compose(
                source=row["source"],
                audit=audit_by_created.get(row["id"]),
                run=runs.get(row["id"]),
                session=sessions.get(row["origin_session_id"] or ""),
                lineage=lineage,
                sessions=sessions,
            )
        return origins


def _compose(
    *,
    source: str | None,
    audit: dict[str, Any] | None,
    run: dict[str, Any] | None,
    session: dict[str, Any] | None,
    lineage: dict[str, Any],
    sessions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Turn the resolved lineage rows into one displayable chain.

    The returned ``chain`` lists ancestors farthest-first, ending at the nearest
    parent. An empty chain means the request has no recoverable ancestry (a
    human's own request, or history that predates the audit log).
    """
    parent_requests = lineage["requests"]
    parent_runs = lineage["runs"]
    parent_tasks = lineage["tasks"]

    # Direct workflow lineage wins: schedule_runs.request_id is written in the
    # same code path that created the request, so it cannot be stale.
    if run:
        return _workflow_origin(run, source=source)

    if session:
        return _chat_origin(session, source=source)

    if audit:
        surface = str(audit.get("source") or "")
        job = _DAEMON_SURFACES.get(surface)
        parent_request_id = audit.get("request_id")
        task_id = audit.get("task_id")

        # A task can be the creating context even when the audit row carries no
        # request id (older writers stamped only the task).
        task_row = parent_tasks.get(task_id or "")
        if not parent_request_id and task_row:
            parent_request_id = task_row.get("request_id")

        if parent_request_id:
            chain: list[dict[str, Any]] = []
            parent_run = parent_runs.get(str(parent_request_id))
            if parent_run:
                chain.append(
                    _hop(
                        kind="workflow",
                        label=parent_run.get("workflow_title") or "Untitled workflow",
                        href=f"/workflows/{parent_run['workflow_id']}",
                        meta=_TRIGGER_ADJECTIVES.get(
                            str(parent_run.get("trigger_type") or "schedule"),
                            "scheduled",
                        )
                        + " workflow",
                    )
                )
            parent_request = parent_requests.get(str(parent_request_id)) or {}
            if parent_request:
                chain.append(
                    _hop(
                        kind="request",
                        label=parent_request.get("title") or "Untitled request",
                        href=f"/activity/{parent_request_id}",
                        meta="request",
                    )
                )
            if task_row:
                chain.append(
                    _hop(
                        kind="task",
                        label=task_row.get("title") or "Untitled task",
                        href=(
                            f"/activity/{parent_request_id}#task-{task_row['id']}"
                            if parent_request_id
                            else None
                        ),
                        meta="task",
                    )
                )
            if chain:
                return _origin(
                    kind=chain[0]["kind"],
                    label=chain[0]["label"],
                    source=source,
                    chain=chain,
                    detail=f"Created during {job}." if job else None,
                )

        # No request ancestry — fall back to the conversation the call came
        # from, which the audit row records even when the request itself has no
        # origin_session_id.
        audit_session = sessions.get(str(audit.get("session_id") or ""))
        if audit_session:
            return _chat_origin(audit_session, source=source)

        # A chat surface with no surviving session row is still a conversation,
        # not an anonymous background job — but there is nowhere to link to, so
        # the chain stays empty rather than inventing a target.
        if surface.startswith("chat."):
            return _origin(
                kind="chat",
                label="A conversation",
                source=source,
                chain=[],
                detail="Created from a conversation.",
            )

        if surface:
            return _origin(
                kind="lucent",
                label=job or surface,
                source=source,
                chain=[],
                detail=f"Created by the background job “{surface}”.",
            )

    if source == "cognitive":
        return _origin(
            kind="lucent",
            label="Lucent's planning cycle",
            source=source,
            chain=[],
            detail="Created by Lucent reviewing its own goals and queue.",
        )

    if source == "daemon":
        return _origin(
            kind="lucent",
            label="Lucent",
            source=source,
            chain=[],
            detail="Created by a Lucent background job.",
        )

    if source == "api":
        return _origin(kind="api", label="An API client", source=source, chain=[])

    return _origin(kind="you", label="You", source=source, chain=[])


def _workflow_origin(run: dict[str, Any], *, source: str | None) -> dict[str, Any]:
    title = run.get("workflow_title") or "Untitled workflow"
    adjective = _TRIGGER_ADJECTIVES.get(
        str(run.get("trigger_type") or "schedule"), "scheduled"
    )
    return _origin(
        kind="workflow",
        label=f"{title} workflow",
        source=source,
        chain=[
            _hop(
                kind="workflow",
                label=title,
                href=f"/workflows/{run['workflow_id']}",
                meta=f"{adjective} workflow",
            )
        ],
    )


def _chat_origin(session: dict[str, Any], *, source: str | None) -> dict[str, Any]:
    title = session.get("title") or "Chat conversation"
    return _origin(
        kind="chat",
        label=title,
        source=source,
        chain=[
            _hop(
                kind="chat",
                label=title,
                href=f"/chat/{session['id']}",
                meta="conversation",
            )
        ],
    )


async def _sessions(
    conn,
    org_id: str,
    session_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not session_ids:
        return {}
    rows = await conn.fetch(
        """SELECT id::text AS id, title, kind, model
           FROM llm_sessions
           WHERE organization_id = $1::uuid AND id = ANY($2::uuid[])""",
        org_id,
        sorted(session_ids),
    )
    return {row["id"]: dict(row) for row in rows}


async def _workflow_runs(
    conn,
    org_id: str,
    request_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """``{request_id: workflow run}`` for requests a workflow materialised."""
    ids = list(request_ids)
    if not ids:
        return {}
    rows = await conn.fetch(
        """SELECT sr.request_id::text AS request_id,
                  s.id::text AS workflow_id,
                  s.title AS workflow_title,
                  COALESCE(s.trigger_type, 'schedule') AS trigger_type
           FROM schedule_runs sr
           JOIN schedules s ON s.id = sr.schedule_id
           WHERE s.organization_id = $1::uuid
             AND sr.request_id = ANY($2::uuid[])
           ORDER BY sr.created_at DESC""",
        org_id,
        ids,
    )
    runs: dict[str, dict[str, Any]] = {}
    for row in rows:
        runs.setdefault(row["request_id"], dict(row))
    return runs


async def _resolve_creating_context(
    conn,
    org_id: str,
    audit_rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve "what was running when this request was created".

    A create_request audit row carries the trusted ``task_id`` / ``request_id``
    of the session that called it. A task belongs to its own request, and that
    request usually owns a workflow run — which is the name a person
    recognises.

    Returns ``{"requests": {...}, "runs": {...}, "tasks": {...}}``. Empty
    mappings mean the creating context is unknown or predates the audit log;
    callers then fall back to the coarser ``source``-only answer.
    """
    rows = list(audit_rows)
    task_ids = {r["task_id"] for r in rows if r.get("task_id")}
    request_ids = {r["request_id"] for r in rows if r.get("request_id")}

    tasks: dict[str, dict[str, Any]] = {}
    if task_ids:
        task_rows = await conn.fetch(
            """SELECT id::text AS id, title, request_id::text AS request_id
               FROM tasks
               WHERE organization_id = $1::uuid AND id = ANY($2::uuid[])""",
            org_id,
            sorted(task_ids),
        )
        tasks = {row["id"]: dict(row) for row in task_rows}
        request_ids |= {row["request_id"] for row in task_rows if row["request_id"]}

    if not request_ids:
        return {"requests": {}, "runs": {}, "tasks": tasks}

    parent_rows = await conn.fetch(
        """SELECT id::text AS id, title, source
           FROM requests
           WHERE organization_id = $1::uuid AND id = ANY($2::uuid[])""",
        org_id,
        sorted(request_ids),
    )
    parents = {row["id"]: dict(row) for row in parent_rows}
    runs = await _workflow_runs(conn, org_id, sorted(request_ids))
    return {"requests": parents, "runs": runs, "tasks": tasks}
