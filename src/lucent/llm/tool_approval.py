"""Human-in-the-loop tool approval and clarifying questions for chat.

Chat surfaces in every mainstream assistant let the person decide whether the
model may run a tool on its own (OpenAI's "always allow / ask every time",
Anthropic's per-tool permission modes, Copilot's approval prompts). Lucent's
chat previously had no equivalent: the tool surface was composed from the agent
definition and every call simply ran.

This module is the single, engine-agnostic seam for that control:

* **Classification** — :data:`READ_ONLY_TOOLS` names the Lucent tools with no
  side effects. Anything not in it (including tools from external MCP servers
  and engine built-ins such as ``run_shell``) requires approval. Unknown tools
  default to *requiring* approval, which is the safe direction.
* **Broker** — :class:`InteractionBroker` holds a per-conversation channel so a
  blocking tool call inside an engine can ask the browser a question and await
  the answer. Both tool approval and ``ask_user`` ride the same channel, so
  resolution is one route and one UI component family.

Why the broker is in-process: Lucent's API server runs a single uvicorn worker
(``uvicorn.run(...)`` with no ``workers=``), so the engine's awaiting coroutine
and the browser's resolve request are in the same process and event loop. A
multi-replica deployment would need the wait moved to a durable channel; that
is deliberately out of scope here and is called out in the resolve route.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from lucent.logging import get_logger

logger = get_logger("llm.tool_approval")

#: Supported per-conversation tool-permission modes.
APPROVAL_MODES = ("auto", "ask")
#: Default mode. Auto-run matches where the industry has settled; "ask" is the
#: opt-in feeling of control.
DEFAULT_APPROVAL_MODE = "auto"

# Tools that only read. They never prompt, even in "ask" mode — asking someone
# to approve `search_memories` is noise, and every provider draws the line in
# the same place (read-only operations run, mutations ask).
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # Lucent memory and identity reads.
    "get_current_user_context",
    "get_existing_tags",
    "get_memories",
    "get_memory",
    "get_memory_versions",
    "get_tag_suggestions",
    "search_memories",
    "search_memories_full",
    "export_memories",
    # Definitions / catalog reads.
    "get_agent_definition",
    "get_hook_definition",
    "get_skill_definition",
    "get_tool_definition",
    "get_tool_runtime_kinds",
    "list_agent_definitions",
    "list_available_models",
    "list_engines",
    "list_hook_definitions",
    "list_mcp_server_definitions",
    "list_pending_capability_consents",
    "list_planning_targets",
    "list_proposals",
    "list_remotes",
    "list_skill_definitions",
    "list_task_kinds",
    "list_tool_definitions",
    # Work / handoff / schedule reads.
    "analyze_tool_failure_patterns",
    "get_handoff",
    "get_request_details",
    "get_schedule_details",
    "get_workflow_details",
    "list_active_work",
    "list_handoffs",
    "list_pending_requests",
    "list_pending_tasks",
    "list_queued_tasks",
    "list_reviews",
    "list_sandbox_templates",
    "list_schedules",
    "list_workflows",
    # Files (read only) and read-only network helpers.
    "list_user_files",
    "read_user_file",
    "fetch_open_meteo_forecast",
    # Asking the person a question is not a side effect.
    "ask_user",
    # Engine built-ins that only read.
    "view",
    "list_directory",
    "grep",
    "web_fetch",
    "web_search",
    "read_file",
})

# Tools whose approval prompt deserves a stronger warning in the UI: they run
# code or drive sandboxed execution rather than editing records.
EXECUTION_TOOLS: frozenset[str] = frozenset({
    "exec_sandbox_command",
    "run_managed_tool",
    "run_shell",
    "run_in_terminal",
    "bash",
    "apply_patch",
    "shell",
})

# Prefixes engines put in front of an MCP tool name. Copilot uses
# ``mcp_<server>_<tool>``, Claude Code uses ``mcp__<server>__<tool>``, and some
# surfaces use ``<server>-<tool>``. Stripping them keeps one classification for
# every engine.
_NAME_PREFIXES = (
    "mcp__",
    "mcp_",
)


def canonical_tool_name(tool_name: str | None) -> str:
    """Return the bare tool name an engine reported, without MCP namespacing."""
    name = str(tool_name or "").strip()
    if not name:
        return ""
    lowered = name.lower()
    for prefix in _NAME_PREFIXES:
        if lowered.startswith(prefix):
            name = name[len(prefix):]
            break
    # ``mcp__lucent__search_memories`` / ``memory-server_search_memories``.
    for separator in ("__", "-"):
        if separator in name:
            head, _, tail = name.partition(separator)
            if head.lower() in {"memory-server", "memoryserver", "lucent", "lucent-mcp"}:
                name = tail
                break
    if name.startswith("memory-server_"):
        name = name[len("memory-server_"):]
    return name


def normalize_approval_mode(value: Any) -> str:
    """Coerce a stored/requested mode to a supported value (default auto)."""
    mode = str(value or "").strip().lower()
    return mode if mode in APPROVAL_MODES else DEFAULT_APPROVAL_MODE


def tool_requires_approval(tool_name: str | None) -> bool:
    """Whether running this tool needs the person's permission in "ask" mode.

    Unknown tools require approval: an unrecognized tool may do anything, so
    the conservative answer is the correct one.
    """
    name = canonical_tool_name(tool_name)
    if not name:
        return True
    return name not in READ_ONLY_TOOLS


def tool_risk(tool_name: str | None) -> str:
    """Rough risk bucket for the approval card: read | write | execute."""
    name = canonical_tool_name(tool_name)
    if name in READ_ONLY_TOOLS:
        return "read"
    if name in EXECUTION_TOOLS:
        return "execute"
    return "write"


def tool_display_name(tool_name: str | None) -> str:
    """Human-readable tool label for prompts and cards."""
    name = canonical_tool_name(tool_name)
    return name.replace("_", " ") or "tool"


#: Name Lucent gives its own MCP server in every engine's config. Tools from this
#: server are gated server-side (``lucent.llm.tool_approval_gate``), so an engine
#: with its own approval callback must NOT also prompt for them.
INTERNAL_MCP_SERVER_NAME = "memory-server"


def is_internal_mcp_server(server_name: str | None) -> bool:
    """Whether an MCP server is Lucent's own (already gated server-side)."""
    return str(server_name or "").strip().lower() == INTERNAL_MCP_SERVER_NAME


def tool_server_from_mcp_name(tool_name: str | None) -> str | None:
    """Extract the MCP server name from an engine's namespaced tool name.

    Claude Code reports MCP tools as ``mcp__<server>__<tool>``; Copilot uses
    ``mcp_<server>_<tool>``. Returns ``None`` for a non-MCP (built-in) tool.
    """
    raw = str(tool_name or "")
    if raw.startswith("mcp__"):
        parts = raw[len("mcp__"):].split("__", 1)
        return parts[0] if parts else None
    if raw.startswith("mcp_"):
        remainder = raw[len("mcp_"):]
        if remainder.startswith(f"{INTERNAL_MCP_SERVER_NAME}_"):
            return INTERNAL_MCP_SERVER_NAME
        parts = remainder.split("_", 1)
        return parts[0] if parts else None
    return None


@dataclass(slots=True)
class EngineApproval:
    """Per-turn approval context an engine needs to gate its own tool calls.

    Lucent's tools are gated inside the MCP server, which covers every engine.
    What the server cannot see is a tool the engine fetched from somewhere else:
    an external MCP server granted to the agent, or a provider built-in such as a
    shell. Engines that own their tool loop use this to gate exactly those,
    without re-prompting for Lucent's own tools.

    ``None`` (the default, and what every non-chat caller passes) means "no
    gating" — daemon and background work never waits on a person.
    """

    mode: str = DEFAULT_APPROVAL_MODE
    session_id: str | None = None
    timeout_seconds: int = 120

    @property
    def enabled(self) -> bool:
        return normalize_approval_mode(self.mode) == "ask"

    def should_gate(self, tool_name: str | None) -> bool:
        """Whether *tool_name* still needs gating in the engine.

        Lucent's own tools arrive already gated by the MCP middleware; gating
        them again would ask the person twice.
        """
        if not self.enabled:
            return False
        if is_internal_mcp_server(tool_server_from_mcp_name(tool_name)):
            return False
        return tool_requires_approval(tool_name)

    async def request(
        self, tool_name: str, tool_input: dict[str, Any] | None
    ) -> ToolApprovalDecision:
        """Ask the person about a tool call the engine is about to make."""
        return await interaction_broker.request_approval(
            session_id=self.session_id,
            tool_name=str(tool_name or ""),
            tool_input=tool_input or {},
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(slots=True)
class ToolApprovalDecision:
    """The person's answer to "may the model run this tool?"."""

    approved: bool
    note: str = ""

    def as_tool_result(self, tool_name: str) -> str:
        """Text handed back to the model when a call was refused."""
        reason = self.note.strip()
        suffix = f" Reason given: {reason}" if reason else ""
        return (
            f"The user denied permission to run `{canonical_tool_name(tool_name) or tool_name}`."
            f"{suffix} Do not retry the same call; continue without it, ask the user "
            "how to proceed with `ask_user`, or explain what you would do instead."
        )


#: Signature engines call before executing a tool that needs permission.
ApprovalHandler = Callable[[str, dict[str, Any]], Any]


@dataclass(slots=True)
class _Channel:
    """Live interactive channel for one chat session."""

    emit: Callable[[dict[str, Any]], None]
    user_id: str | None = None
    #: Tools the person chose to always allow for this conversation.
    approved_tools: set[str] = field(default_factory=set)


#: How many questions one `ask_user` call may ask at once. Batching questions a
#: person must answer together is genuinely useful; batching ten of them is an
#: interrogation, so the cap is enforced at the tool and the broker.
MAX_QUESTIONS_PER_REQUEST = 5
#: How many suggested answers one question may offer (labels + descriptions).
MAX_CHOICES_PER_QUESTION = 6


def normalize_choice(choice: Any) -> dict[str, Any] | None:
    """Normalize one suggested answer to ``{label, description, recommended}``.

    Accepts a plain string (the common case) or an object carrying a
    ``recommended`` flag and an optional ``description``. Returns ``None`` for
    entries with no usable label so a malformed suggestion is dropped rather
    than rendering an empty button.
    """
    if isinstance(choice, dict):
        label = str(choice.get("label") or choice.get("value") or "").strip()
        if not label:
            return None
        return {
            "label": label,
            "description": str(choice.get("description") or "").strip(),
            "recommended": bool(choice.get("recommended")),
        }
    label = str(choice or "").strip()
    if not label:
        return None
    return {"label": label, "description": "", "recommended": False}


def normalize_question(raw: Any, *, allow_free_form: bool = True) -> dict[str, Any] | None:
    """Normalize one question payload from the tool or the API.

    Shape: ``{question, header, choices: [{label, description, recommended}],
    allow_free_form}``. Returns ``None`` when the question text is empty.
    """
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("question") or raw.get("prompt") or "").strip()
    if not text:
        return None
    choices: list[dict[str, Any]] = []
    for entry in raw.get("choices") or []:
        normalized = normalize_choice(entry)
        if normalized is not None:
            choices.append(normalized)
        if len(choices) >= MAX_CHOICES_PER_QUESTION:
            break
    free_form = raw.get("allow_free_form", raw.get("allow_freeform", allow_free_form))
    return {
        "question": text,
        "header": str(raw.get("header") or "").strip(),
        "choices": choices,
        "allow_free_form": bool(free_form),
    }


@dataclass(slots=True)
class PendingInteraction:
    """One approval prompt or clarifying question awaiting an answer."""

    id: str
    kind: str  # "approval" | "question"
    session_id: str
    user_id: str | None
    tool_name: str | None
    tool_input: Any
    question: str
    choices: list[str]
    allow_freeform: bool
    header: str
    created_at: float
    deadline: float
    future: asyncio.Future
    #: Batched clarifying questions. A single question is just a one-entry list;
    #: approvals leave it empty.
    questions: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        """JSON-safe shape for the API and the SSE payload."""
        payload = {
            "request_id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "tool": self.tool_name,
            "input": self.tool_input,
            "question": self.question,
            "choices": list(self.choices),
            "allow_freeform": self.allow_freeform,
            "header": self.header,
            "risk": tool_risk(self.tool_name) if self.kind == "approval" else "read",
            "created_at": self.created_at,
            "expires_at": self.deadline,
        }
        if self.questions:
            payload["questions"] = [dict(question) for question in self.questions]
        return payload


class InteractionBroker:
    """Routes blocking interaction requests from engines to the browser.

    One channel per chat session. A channel is opened for the duration of a
    streaming turn; anything that runs outside a turn (the daemon, a background
    report) finds no channel and is told so rather than blocking forever.
    """

    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._pending: dict[str, PendingInteraction] = {}

    # ── channel lifecycle ────────────────────────────────────────────────

    def open_channel(
        self,
        session_id: str | None,
        emit: Callable[[dict[str, Any]], None],
        *,
        user_id: str | None = None,
    ) -> str | None:
        """Register the live channel for a session and return its key."""
        key = str(session_id or "").strip()
        if not key:
            return None
        # A previous turn on the same session must not leave a stale emitter
        # (its queue is closed once that turn ends), and its unanswered prompts
        # belong to a turn that is over.
        self.close_channel(key)
        self._channels[key] = _Channel(emit=emit, user_id=user_id)
        return key

    def close_channel(self, session_id: str | None) -> None:
        """Drop the channel and cancel anything still waiting on it."""
        key = str(session_id or "").strip()
        if not key:
            return
        self._channels.pop(key, None)
        for request_id, pending in list(self._pending.items()):
            if pending.session_id != key:
                continue
            self._pending.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_result(None)

    def has_channel(self, session_id: str | None) -> bool:
        return str(session_id or "").strip() in self._channels

    # ── requests ─────────────────────────────────────────────────────────

    async def request_approval(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        tool_input: Any,
        timeout_seconds: int,
    ) -> ToolApprovalDecision:
        """Ask the person whether the model may run *tool_name*.

        Fails closed when there is no live channel: the conversation asked for
        approval, so running unsupervised would defeat the setting.
        """
        key = str(session_id or "").strip()
        channel = self._channels.get(key)
        if channel is None:
            return ToolApprovalDecision(
                approved=False,
                note=(
                    "this conversation could not reach you for approval, so the tool "
                    "was not run"
                ),
            )
        bare = canonical_tool_name(tool_name)
        if bare in channel.approved_tools:
            return ToolApprovalDecision(approved=True, note="allowed for this chat")

        pending = self._create_pending(
            kind="approval",
            session_id=key,
            user_id=channel.user_id,
            tool_name=bare,
            tool_input=tool_input,
            question="",
            choices=[],
            allow_freeform=False,
            header="",
            timeout_seconds=timeout_seconds,
        )
        channel.emit({"type": "tool_approval_request", **pending.public()})
        answer = await self._await(pending)
        if answer is None:
            channel.emit({
                "type": "tool_approval_result",
                "request_id": pending.id,
                "session_id": key,
                "tool": bare,
                "decision": "deny",
                "reason": "timed out",
            })
            return ToolApprovalDecision(
                approved=False, note="the request timed out before the user answered"
            )
        decision = str(answer.get("decision") or "deny")
        if decision == "approve" and answer.get("remember"):
            if canonical_tool_name(bare) not in READ_ONLY_TOOLS:
                channel.approved_tools.add(canonical_tool_name(bare))
        channel.emit({
            "type": "tool_approval_result",
            "request_id": pending.id,
            "session_id": key,
            "tool": bare,
            "decision": decision,
            "reason": answer.get("note") or "",
        })
        return ToolApprovalDecision(
            approved=decision == "approve", note=str(answer.get("note") or "")
        )

    async def ask_questions(
        self,
        *,
        session_id: str | None,
        questions: list[dict[str, Any]],
        timeout_seconds: int,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the person one or more clarifying questions in a single turn.

        Batching matters for the same reason it does in every other assistant:
        two questions asked one after another cost two model round-trips and two
        waits, while asking both together costs one. The person answers them on
        one card and the whole answer set returns to the model at once.

        Returns ``{"answered": False, ...}`` when no channel exists (unattended
        contexts such as the daemon) or when nothing came back in time, so the
        tool can tell the model to proceed differently instead of hanging.
        ``answers`` is always a list parallel to the questions asked, so a model
        can read it positionally even when some entries went unanswered.
        """
        normalized = [
            question
            for question in (normalize_question(raw) for raw in questions or [])
            if question is not None
        ][:MAX_QUESTIONS_PER_REQUEST]
        if not normalized:
            return {"answered": False, "reason": "no question text was provided"}

        key = str(session_id or "").strip()
        channel = self._channels.get(key)
        if channel is None:
            return {
                "answered": False,
                "answers": [{"answered": False} for _ in normalized],
                "reason": (
                    "No one is watching this session, so a question cannot be answered. "
                    "Use send_handoff with requires_response=true when running unattended."
                ),
            }
        pending = self._create_pending(
            kind="question",
            session_id=key,
            user_id=channel.user_id or user_id,
            tool_name="ask_user",
            tool_input=None,
            # The single-question fields mirror the first question so a client
            # that predates batches still renders something useful.
            question=normalized[0]["question"],
            choices=[choice["label"] for choice in normalized[0]["choices"]],
            allow_freeform=bool(normalized[0]["allow_free_form"]),
            header=str(normalized[0]["header"]),
            timeout_seconds=timeout_seconds,
            questions=normalized,
        )
        channel.emit({"type": "user_question", **pending.public()})
        answer = await self._await(pending)

        if answer is None:
            channel.emit({
                "type": "user_question_result",
                "request_id": pending.id,
                "session_id": key,
                "answered": False,
                "answer": "",
                "answers": [{"answered": False, "answer": ""} for _ in normalized],
                "reason": "timed out",
            })
            return {
                "answered": False,
                "answers": [{"answered": False, "answer": ""} for _ in normalized],
                "reason": (
                    "The user did not answer in time. Continue with your best judgment "
                    "and say what you assumed."
                ),
            }

        # Accept the batched shape, and also the single-answer shape so a client
        # that only submits one value still resolves correctly.
        raw_answers = answer.get("answers")
        if not isinstance(raw_answers, list):
            single = str(answer.get("answer") or "")
            raw_answers = [{"answer": single, "choice": answer.get("choice")}]
        resolved: list[dict[str, Any]] = []
        for index, question in enumerate(normalized):
            entry = raw_answers[index] if index < len(raw_answers) else None
            if isinstance(entry, str):
                entry = {"answer": entry}
            entry = entry if isinstance(entry, dict) else {}
            text = str(entry.get("answer") or entry.get("choice") or "").strip()
            choice = entry.get("choice")
            answered = bool(text) or bool(choice)
            resolved.append({
                "header": question["header"],
                "question": question["question"],
                "answered": answered,
                "answer": text,
                "choice": str(choice) if choice else None,
                # Echo the recommendation so the model can see whether the person
                # went with its suggestion.
                "recommended": next(
                    (
                        choice_entry["label"]
                        for choice_entry in question["choices"]
                        if choice_entry.get("recommended")
                    ),
                    None,
                ),
            })

        channel.emit({
            "type": "user_question_result",
            "request_id": pending.id,
            "session_id": key,
            "answered": any(entry["answered"] for entry in resolved),
            "answer": " | ".join(
                entry["answer"] for entry in resolved if entry["answered"]
            ),
            "answers": resolved,
        })
        return {
            "answered": any(entry["answered"] for entry in resolved),
            "answers": resolved,
        }

    async def ask_question(
        self,
        *,
        session_id: str | None,
        question: str,
        choices: list[Any] | None,
        allow_freeform: bool,
        header: str,
        timeout_seconds: int,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask a single clarifying question (thin wrapper over ``ask_questions``).

        Kept for callers and tests that model one question; the batched path is
        the implementation, so both share one card and one resolve route.
        """
        result = await self.ask_questions(
            session_id=session_id,
            questions=[{
                "question": question,
                "header": header,
                "choices": list(choices or []),
                "allow_free_form": allow_freeform,
            }],
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        )
        answers = result.get("answers") or []
        first = answers[0] if answers else {}
        return {
            "answered": bool(result.get("answered")),
            "answer": first.get("answer", ""),
            "choice": first.get("choice"),
            "reason": result.get("reason"),
        }

    # ── resolution (browser → waiting coroutine) ──────────────────────────

    def pending_for_session(self, session_id: str | None) -> list[dict[str, Any]]:
        self._prune()
        key = str(session_id or "").strip()
        return [
            pending.public()
            for pending in self._pending.values()
            if pending.session_id == key
        ]

    def resolve(
        self,
        request_id: str,
        *,
        answer: dict[str, Any],
        session_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any] | None:
        """Deliver an answer to a waiting request.

        Returns the resolved request's public shape, or ``None`` when the id is
        unknown, already answered, or belongs to another session/user. Callers
        turn ``None`` into a 404/409 rather than leaking existence.
        """
        self._prune()
        pending = self._pending.get(str(request_id))
        if pending is None or pending.future.done():
            return None
        if session_id and pending.session_id != str(session_id):
            return None
        if pending.user_id and user_id and str(pending.user_id) != str(user_id):
            return None
        self._pending.pop(pending.id, None)
        pending.future.set_result(answer)
        return pending.public()

    # ── internals ────────────────────────────────────────────────────────

    def _create_pending(
        self,
        *,
        kind: str,
        session_id: str,
        user_id: str | None,
        tool_name: str | None,
        tool_input: Any,
        question: str,
        choices: list[str],
        allow_freeform: bool,
        header: str,
        timeout_seconds: int,
        questions: list[dict[str, Any]] | None = None,
    ) -> PendingInteraction:
        self._prune()
        timeout = max(5, int(timeout_seconds or 180))
        pending = PendingInteraction(
            id=str(uuid4()),
            kind=kind,
            session_id=session_id,
            user_id=user_id,
            tool_name=tool_name,
            tool_input=tool_input,
            question=question,
            choices=choices,
            allow_freeform=allow_freeform,
            header=header,
            created_at=time.time(),
            deadline=time.time() + timeout,
            future=asyncio.get_running_loop().create_future(),
            questions=list(questions or []),
        )
        self._pending[pending.id] = pending
        return pending

    @staticmethod
    async def _await(pending: PendingInteraction) -> dict[str, Any] | None:
        try:
            remaining = max(0.0, pending.deadline - time.time())
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            raise

    def _prune(self) -> None:
        """Drop requests whose wait already ended (aborted turns, timeouts)."""
        if not self._pending:
            return
        now = time.time()
        for request_id, pending in list(self._pending.items()):
            if pending.future.done() or pending.deadline + 60 < now:
                self._pending.pop(request_id, None)
        # Bound the map even under pathological churn.
        if len(self._pending) > 200:
            for request_id in list(self._pending)[:-100]:
                self._pending.pop(request_id, None)


#: Process-wide broker. One uvicorn worker, so this is the whole story.
interaction_broker = InteractionBroker()


__all__ = [
    "APPROVAL_MODES",
    "DEFAULT_APPROVAL_MODE",
    "EXECUTION_TOOLS",
    "INTERNAL_MCP_SERVER_NAME",
    "MAX_CHOICES_PER_QUESTION",
    "MAX_QUESTIONS_PER_REQUEST",
    "READ_ONLY_TOOLS",
    "ApprovalHandler",
    "EngineApproval",
    "InteractionBroker",
    "PendingInteraction",
    "ToolApprovalDecision",
    "canonical_tool_name",
    "interaction_broker",
    "is_internal_mcp_server",
    "normalize_approval_mode",
    "normalize_choice",
    "normalize_question",
    "tool_display_name",
    "tool_requires_approval",
    "tool_risk",
    "tool_server_from_mcp_name",
]
