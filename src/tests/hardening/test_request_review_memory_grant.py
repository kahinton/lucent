"""The post-completion review must be *able* to do its mandatory memory work.

Regression coverage for the 2026-11-20 defect (request ``b0028a64``, and the
reviews queued behind it):

The review prompt and the daemon's decision applier both require
``update_memory`` on every linked memory before a decision may be emitted. The
review agent was dispatched with a healthy-looking composition —
``tools: ["*"]`` on the internal memory-server carrier — but the Claude Code
engine translated the server config and silently dropped that policy, so every
memory call came back "you haven't granted it yet". A review could therefore see
a defect it could not remedy, and its NEEDS_REWORK was bounced onto the task
agent, burning the request's whole rework budget on something the task agent had
nothing to do with.

Three behaviours are pinned here:

1. The engine hands the carrier's tool policy to Claude Code as ``allowed_tools``
   (attaching an MCP server does not authorise its tools).
2. A review whose composition could not supply a memory tool parks for a person
   and never requeues task work.
3. The mandatory-update rule still *bites* when the tool WAS available — the fix
   is that the requirement was impossible, not that it was too strict.

Everything is exercised through the pre-existing public call paths
(``ClaudeCodeEngine._build_options``, ``lifecycle._process_request_review_task``)
so these tests fail against the code before the fix rather than erroring on a
missing import.
"""

from __future__ import annotations

import asyncio

import daemon.daemon as dm
from daemon.review import lifecycle
from lucent.llm import claude_code_engine as cc

ORG_ID = "0f9abaa4-7489-47ab-8d6c-7c5be8d69d51"
USER_ID = "7b2129ec-392c-4787-85b7-184ae8c23ed4"
REQUEST_ID = "b0028a64-1c2f-4d55-9a1b-0e3f5f7c9a10"
REVIEW_TASK_ID = "e243d6c3-8a44-4b0e-9f21-6d7c1b2a3f40"
WORK_TASK_ID = "5c6ca55c-77de-4a1b-8c33-9e2f4a6b8d50"
MEMORY_ID = "fb8ca12d-3b6e-4f81-a2c7-5d9e0b1f4a60"

#: The carrier the daemon actually builds for a dispatched task
#: (``lucent.tool_policy.memory_server_tools_for_task`` → ``["*"]``).
CARRIER_GRANTED = {
    "memory-server": {
        "type": "http",
        "url": "http://lucent-server:8000/api/mcp",
        "headers": {"Authorization": "Bearer scoped"},
        "tools": ["*"],
        "internal": True,
    }
}

#: A composition with no memory server at all — nothing in it can write memories.
CARRIER_ABSENT: dict = {}

LINKED_MEMORY = {
    "memory_id": MEMORY_ID,
    "relation": "supports",
    "memory_type": "technical",
    "content": "Reaper leases are renewed every 30s.",
    "status": "active",
}

NEEDS_REWORK_OUTPUT = f"""REQUEST_REVIEW_DECISION: NEEDS_REWORK
TASK_IDS_TO_REWORK: {WORK_TASK_ID}
FEEDBACK: Recorded outputs is empty; the design lives only in the task result prose.
MEMORIES_UPDATED: none
"""

#: The exact refusal text the Claude Code runtime returns for an MCP tool the
#: session never allowlisted (emitted by the CLI, so it is matched as text).
REFUSED_OUTPUT = f"""REQUEST_REVIEW_DECISION: NEEDS_REWORK
TASK_IDS_TO_REWORK: {WORK_TASK_ID}
FEEDBACK: I could not record the deliverable: Claude requested permissions to use
mcp__memory-server__record_task_output, but you haven't granted it yet. The same
refusal blocked mcp__memory-server__update_memory on the linked memory.
MEMORIES_UPDATED: none
"""


# ── 1. Engine: the carrier's tool policy reaches Claude Code ───────────────


def _build_options(monkeypatch, mcp_config):
    captured: dict = {}

    def fake_options(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(cc, "_ensure_sdk", lambda: True)
    monkeypatch.setattr(cc, "_ClaudeAgentOptions", fake_options)
    engine = cc.ClaudeCodeEngine()
    engine._build_options(
        model="claude-sonnet-4",
        system_message="review",
        mcp_config=mcp_config,
        reasoning_effort=None,
        approve_permissions=True,
        provider_session_id=None,
        resume=False,
        audit_context={},
        include_partial=True,
        native_env=None,
        approval=None,
        system_prompt_path=None,
    )
    return captured


def test_engine_allowlists_composed_mcp_tools(monkeypatch):
    """Attaching memory-server is not authorising it; the wildcard must survive."""
    captured = _build_options(monkeypatch, CARRIER_GRANTED)

    assert "memory-server" in captured["mcp_servers"]
    assert captured["allowed_tools"] == ["mcp__memory-server"]


def test_engine_allowlists_named_tools_not_backported_as_wildcard(monkeypatch):
    """A narrow carrier stays narrow — the translation must not widen it."""
    captured = _build_options(
        monkeypatch,
        {"memory-server": {"type": "http", "url": "http://x", "tools": ["update_memory"]}},
    )
    assert captured["allowed_tools"] == ["mcp__memory-server__update_memory"]


def test_engine_omits_allowlist_when_no_server_declares_one(monkeypatch):
    captured = _build_options(monkeypatch, CARRIER_ABSENT)
    assert "allowed_tools" not in captured


def test_engine_does_not_pre_approve_external_connector_tools(monkeypatch):
    """An allow rule bypasses can_use_tool, so external connectors keep prompting."""
    captured = _build_options(
        monkeypatch,
        {
            **CARRIER_GRANTED,
            "mcp-github": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "tools": ["*"],
            },
        },
    )
    assert captured["allowed_tools"] == ["mcp__memory-server"]


# ── 2/3. The applier: impossibility never charges the task agent ───────────


class _Api:
    """Records the review applier's outward effects."""

    def __init__(self, *, linked_memories):
        self.linked_memories = linked_memories
        self.events: list[tuple[str, str, dict | None]] = []
        self.status_updates: list[str] = []
        self.requeued: list[str] = []

    def event_types(self) -> list[str]:
        return [event_type for event_type, _, _ in self.events]

    def event(self, event_type: str) -> dict | None:
        for kind, _, metadata in self.events:
            if kind == event_type:
                return metadata
        return None

    async def add_event(self, task_id, event_type, detail=None, metadata=None, **kwargs):
        self.events.append((event_type, detail or "", metadata))
        return {"id": "event"}

    async def get_request(self, request_id, *args, **kwargs):
        return {"id": request_id, "title": "reaper design", "created_by": USER_ID}

    async def get_request_memories(self, request_id):
        return list(self.linked_memories)

    async def update_request_status(self, request_id, status, *args, **kwargs):
        self.status_updates.append(status)
        return {"id": request_id, "status": status}

    async def requeue_completed_task_for_rework(self, task_id, feedback, **kwargs):
        self.requeued.append(str(task_id))
        return True


class _Daemon:
    """Only the seam ``_process_request_review_task`` uses on the daemon."""

    def _parse_review_decision(self, text):
        return lifecycle._parse_review_decision(self, text)


def _install(monkeypatch, *, api, mcp_config):
    monkeypatch.setattr(dm, "RequestAPI", api)
    monkeypatch.setattr(dm, "MCP_CONFIG", mcp_config)
    monkeypatch.setattr(dm, "log", lambda *a, **k: None)


def _review_task() -> dict:
    return {
        "id": REVIEW_TASK_ID,
        "request_id": REQUEST_ID,
        "title": "Post-completion review",
        "agent_type": "request-review",
    }


def _run(monkeypatch, *, api, mcp_config, output) -> None:
    _install(monkeypatch, api=api, mcp_config=mcp_config)

    asyncio.run(
        lifecycle._process_request_review_task(_Daemon(), _review_task(), output)
    )


def test_review_with_linked_memories_and_no_memory_tool_does_not_blame_task_agent(
    monkeypatch,
):
    """The observed shape: linked memories, no memory tool, NEEDS_REWORK verdict.

    Before the fix the named work task was requeued — charging the task agent
    for a failure it did not cause — and the request went to needs_rework,
    spending a rework slot each time it happened.
    """
    api = _Api(linked_memories=[LINKED_MEMORY])

    _run(monkeypatch, api=api, mcp_config=CARRIER_ABSENT, output=NEEDS_REWORK_OUTPUT)

    assert api.requeued == [], "task work must not be reopened for a composition defect"
    assert "request_review_needs_rework" not in api.event_types()
    assert "request_review_memory_update_blocked" in api.event_types()
    # Parked for a person, not silently approved and not sent to needs_rework.
    assert api.status_updates == ["review"]

    blocked = api.event("request_review_memory_update_blocked")
    assert blocked is not None
    assert blocked["linked_memory_ids"] == [MEMORY_ID]
    # The reviewer's finding survives even though its verdict could not be used.
    assert "Recorded outputs is empty" in blocked["feedback"]


def test_review_output_reporting_a_refused_tool_is_treated_as_impossible(monkeypatch):
    """A healthy-looking carrier plus runtime refusals is still impossibility.

    This is the real incident: the composition carried ``tools: ["*"]`` and the
    engine dropped it, so only the review's own output showed the refusal.
    """
    api = _Api(linked_memories=[LINKED_MEMORY])

    _run(monkeypatch, api=api, mcp_config=CARRIER_GRANTED, output=REFUSED_OUTPUT)

    assert api.requeued == []
    assert "request_review_needs_rework" not in api.event_types()
    assert "request_review_memory_update_blocked" in api.event_types()
    assert api.status_updates == ["review"]


def test_missing_memory_update_stays_visible_when_the_tool_was_available(monkeypatch):
    """Do not weaken the rule: skipping a reachable memory update is still a failure."""
    approved_without_attestation = f"""REQUEST_REVIEW_DECISION: APPROVED
FEEDBACK: Everything looks good.
MEMORIES_UPDATED: none
"""
    api = _Api(linked_memories=[LINKED_MEMORY])

    _run(
        monkeypatch,
        api=api,
        mcp_config=CARRIER_GRANTED,
        output=approved_without_attestation,
    )

    assert "request_review_memory_update_missing" in api.event_types()
    assert "request_review_memory_update_blocked" not in api.event_types()
    assert "completed" not in api.status_updates
    assert api.status_updates == ["needs_rework"]


def test_review_without_linked_memories_is_unaffected(monkeypatch):
    """No linked memories means no obligation — the ordinary verdict still applies."""
    api = _Api(linked_memories=[])

    _run(monkeypatch, api=api, mcp_config=CARRIER_ABSENT, output=NEEDS_REWORK_OUTPUT)

    assert api.requeued == [WORK_TASK_ID]
    assert "request_review_memory_update_blocked" not in api.event_types()
