"""Project standing context injection (system-prompt native, Projects v2).

Replaces the v1 hook-mediated project injection (before_model_call
``project-context`` hook with its 16k-token file-contents budget and
retrieval-select): the chat's project metadata now rides the system prompt
itself, assembled by the engine at the start of every turn.

Design contract (Kyle, 2026-09-12):
- Metadata-only standing block: project name, user-authored instructions
  (verbatim), and a one-line-per-file manifest [name, ID, description].
  File contents NEVER enter this block — the model reads them on demand
  through ``read_user_file`` anchored to the manifest IDs.
- Refreshed every turn: the session caller (chat.py) re-reads project name,
  instructions, and file manifest through the fail-closed
  ``(org, user)``-scoped :meth:`ProjectRepository.get_context_for_session`
  and threads them via ``session_state["_project_context"]``; the engine
  recomposes the block from that snapshot at each turn's system-prompt
  assembly. Instruction edits or file moves reflect on the next turn.
- Append-only: the block is appended AFTER the global/system prompt and
  composes alongside hook-mediated memory injections — never replaces them.
- Fail-closed: an absent/malformed/disabled project context produces no
  block and no event — unfiled chats look exactly like they always did.
  Scope lives entirely in the repository read; this renderer trusts nothing
  but the already-scoped threaded context and never touches the DB.
- Token efficiency: block size scales with file COUNT (one manifest line
  per file), never with file SIZE.

Observability: ``project_context_event``/``emit_project_context_event``
carry the metadata the chat ``project_context`` chip renders (project name,
file count, block size) as a SessionEvent so the existing hook-chip
pipeline keeps working unchanged.
"""

from __future__ import annotations

from typing import Any

from lucent import settings as runtime_settings
from lucent.logging import get_logger

logger = get_logger(__name__)

PROJECT_CONTEXT_HOOK_NAME = "project_context"

_MANIFEST_DESCRIPTION_FALLBACK = "no description — inspect with read_user_file"

_BLOCK_HEADER = (
    "## Project: {name}\n"
    "\n"
    "{instructions}\n"
    "\n"
    "## Project files\n"
    "Durable project files. Read one on demand with read_user_file using its "
    "file_id — never assume their contents:\n"
)

_NO_FILES_LINE = "No durable files are attached to this project yet."


def _project_context_from_session_state(
    session_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract the session caller's threaded project context (fail-closed).

    The context itself is loaded by the session caller (chat.py) through
    :meth:`ProjectRepository.get_context_for_session` — one scoped,
    fail-closed SQL set — and threaded in via ``session_state[
    "_project_context"]``. An absent or malformed key means plain "no
    project context" for this turn; nothing is inferred and nothing is
    loaded here.
    """
    if not isinstance(session_state, dict):
        return None
    context = session_state.get("_project_context")
    if not isinstance(context, dict):
        return None
    project = context.get("project")
    if not isinstance(project, dict) or not project.get("id"):
        return None
    return context


def _gate_enabled(audit_context: dict[str, Any] | None) -> bool:
    """Runtime kill-switch gate, org-scoped (fail-open to enabled by default).

    ``projects.context_enabled`` stays a deliberate off-switch for the
    injection. The org id comes from the engine's audit context (chat.py
    threads it on every call); with no org resolvable, the ambient runtime
    context is consulted as a fallback and the documented default (enabled)
    stands — matching the v1 hook's gate semantics.
    """
    organization_id = None
    if isinstance(audit_context, dict):
        organization_id = audit_context.get("organization_id")
    try:
        if not organization_id:
            organization_id = runtime_settings._current_organization_id()
        return bool(
            runtime_settings.project_context_enabled(
                organization_id=organization_id,
            )
        )
    except Exception:
        return True  # enabled by default; a settings failure must not kill injection


def _manifest_description(project_file: dict[str, Any]) -> str:
    """One-line description for a manifest entry, metadata-first.

    Prefer an explicit ``description`` (then ``summary``) from the file's
    metadata JSONB; fall back to display name when it differs from the
    stored filename; otherwise a fixed pointer to ``read_user_file``.
    """
    metadata = project_file.get("metadata")
    if isinstance(metadata, str):
        # Defensive: repo layer normally coerces JSONB to dict already.
        import json

        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = None
    if isinstance(metadata, dict):
        for key in ("description", "summary"):
            text = str(metadata.get(key) or "").strip()
            if text:
                return " ".join(text.split())
    display_name = str(project_file.get("display_name") or "").strip()
    filename = str(project_file.get("filename") or "").strip()
    if display_name and display_name != filename:
        return display_name
    return _MANIFEST_DESCRIPTION_FALLBACK


def render_project_context_block(
    session_state: dict[str, Any] | None,
    *,
    audit_context: dict[str, Any] | None = None,
) -> str | None:
    """Render the standing project block for this turn's system prompt.

    Returns ``None`` for unfiled chats, malformed/missing threaded context,
    or a disabled runtime gate — callers treat ``None`` as "no block this
    turn" and never as an error. Output is metadata-only: names, ids, and
    descriptions; no file contents, ever.
    """
    if not _gate_enabled(audit_context):
        return None
    context = _project_context_from_session_state(session_state)
    if not context:
        return None
    project = context.get("project") or {}
    project_name = str(project.get("name") or "").strip() or "Untitled project"
    instructions = str(project.get("instructions") or "").strip()

    files = [
        f for f in (context.get("files") or [])
        if isinstance(f, dict) and f.get("id")
    ]

    parts: list[str] = []
    if instructions:
        parts.append(_BLOCK_HEADER.format(name=project_name, instructions=instructions))
    else:
        parts.append(
            "## Project: {name}\n\n"
            "No standing instructions are set for this project.\n".format(
                name=project_name,
            )
        )

    if files:
        for project_file in files:
            file_id = str(project_file.get("id"))
            label = (
                str(project_file.get("display_name") or "").strip()
                or str(project_file.get("filename") or "").strip()
                or file_id
            )
            parts.append(
                f"- {label} (file_id: {file_id}) — {_manifest_description(project_file)}"
            )
    else:
        parts.append(_NO_FILES_LINE)

    return "\n".join(parts).strip() + "\n"


def project_context_event_metadata(
    session_state: dict[str, Any] | None,
    *,
    block: str,
) -> dict[str, Any]:
    """Chip/persistence metadata for one project-context injection event.

    ``block`` is the rendered block string; callers pass the exact text they
    appended to the system prompt so the reported size always matches what
    the model actually sees.
    """
    context = _project_context_from_session_state(session_state) or {}
    project = context.get("project") or {}
    files = [
        f for f in (context.get("files") or [])
        if isinstance(f, dict) and f.get("id")
    ]
    return {
        "project_id": str(project.get("id") or "")[:8],
        "name": str(project.get("name") or "").strip() or "Untitled project",
        "file_count": len(files),
        "bytes": len(block.encode("utf-8")),
    }


def emit_project_context_event(
    on_event: Any | None,
    session_state: dict[str, Any] | None,
    *,
    block: str,
) -> None:
    """Emit the project-context observability event through the engine's
    event funnel.

    Uses the same ``OTHER``/``_hook`` channel the hook pipeline uses so the
    chat ``hook_context`` persistence and the ``project_context`` chip keep
    working without any new transport. Never raises into the turn.
    """
    if on_event is None:
        return
    try:
        from lucent.llm.engine import SessionEvent, SessionEventType

        metadata = project_context_event_metadata(session_state, block=block)
        on_event(
            SessionEvent(
                type=SessionEventType.OTHER,
                tool_name="_hook",
                content=block[:2000],
                raw={
                    "hook": PROJECT_CONTEXT_HOOK_NAME,
                    "phase": "system_prompt",
                    "decision": "inject",
                    **metadata,
                },
            )
        )
    except Exception:
        logger.debug("Failed to emit project context event", exc_info=True)