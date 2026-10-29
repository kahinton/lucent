"""Tool approval and clarifying-question behavior for chat.

The interesting properties are all about *not* being asked and never hanging:
read-only tools run, Lucent's own tools are gated once (server-side) rather than
twice, an unanswered prompt becomes a refusal, and a turn that ends releases any
waiting coroutine.
"""

from __future__ import annotations

import asyncio

import pytest

from lucent.llm.tool_approval import (
    DEFAULT_APPROVAL_MODE,
    MAX_CHOICES_PER_QUESTION,
    MAX_QUESTIONS_PER_REQUEST,
    EngineApproval,
    InteractionBroker,
    ToolApprovalDecision,
    canonical_tool_name,
    is_internal_mcp_server,
    normalize_approval_mode,
    normalize_choice,
    normalize_question,
    tool_requires_approval,
    tool_risk,
    tool_server_from_mcp_name,
)


# ── classification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("search_memories", "search_memories"),
        ("mcp__memory-server__search_memories", "search_memories"),
        ("mcp_memory-server_search_memories", "search_memories"),
        ("memory-server_search_memories", "search_memories"),
        ("", ""),
        (None, ""),
    ],
)
def test_canonical_tool_name_strips_engine_namespacing(raw, expected):
    assert canonical_tool_name(raw) == expected


def test_read_only_tools_never_need_approval():
    for name in ("search_memories", "list_handoffs", "read_user_file", "ask_user"):
        assert tool_requires_approval(name) is False


def test_mutating_and_unknown_tools_need_approval():
    assert tool_requires_approval("create_memory") is True
    # Unknown defaults to approval: an unrecognized tool may do anything.
    assert tool_requires_approval("some_external_tool") is True
    assert tool_requires_approval("") is True


def test_tool_risk_buckets():
    assert tool_risk("search_memories") == "read"
    assert tool_risk("create_request") == "write"
    assert tool_risk("run_shell") == "execute"
    assert tool_risk("exec_sandbox_command") == "execute"


@pytest.mark.parametrize(
    "value,expected",
    [("ask", "ask"), ("ASK", "ask"), ("auto", "auto"), ("nonsense", DEFAULT_APPROVAL_MODE), (None, "auto")],
)
def test_normalize_approval_mode(value, expected):
    assert normalize_approval_mode(value) == expected


def test_default_mode_is_auto():
    assert DEFAULT_APPROVAL_MODE == "auto"


# ── namespaced server detection ────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_name,server",
    [
        ("mcp__memory-server__search_memories", "memory-server"),
        ("mcp__github__create_issue", "github"),
        ("mcp_memory-server_search_memories", "memory-server"),
        ("search_memories", None),
        ("run_shell", None),
    ],
)
def test_tool_server_from_mcp_name(tool_name, server):
    assert tool_server_from_mcp_name(tool_name) == server


def test_is_internal_mcp_server():
    assert is_internal_mcp_server("memory-server") is True
    assert is_internal_mcp_server("github") is False
    assert is_internal_mcp_server(None) is False


# ── EngineApproval ─────────────────────────────────────────────────────────


def test_engine_approval_disabled_by_default():
    approval = EngineApproval()
    assert approval.enabled is False
    assert approval.should_gate("run_shell") is False


def test_engine_approval_skips_lucents_own_tools():
    """Lucent's tools are gated in the MCP server, so the engine must not ask twice."""
    approval = EngineApproval(mode="ask", session_id="s")
    assert approval.should_gate("mcp__memory-server__create_memory") is False
    assert approval.should_gate("mcp_memory-server_create_memory") is False


def test_engine_approval_gates_external_and_builtin_tools():
    approval = EngineApproval(mode="ask", session_id="s")
    assert approval.should_gate("mcp__github__create_issue") is True
    assert approval.should_gate("run_shell") is True
    assert approval.should_gate("web_fetch") is False  # read-only built-in


# ── broker ────────────────────────────────────────────────────────────────


def _record(events: list[dict]):
    return lambda payload: events.append(payload)


@pytest.mark.asyncio
async def test_approval_waits_for_and_applies_the_answer():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def answer():
        while not any(e.get("type") == "tool_approval_request" for e in events):
            await asyncio.sleep(0)
        request_id = next(
            e["request_id"] for e in events if e.get("type") == "tool_approval_request"
        )
        assert broker.resolve(
            request_id,
            answer={"decision": "approve"},
            session_id="sess",
            user_id=None,
        )

    task = asyncio.create_task(answer())
    decision = await broker.request_approval(
        session_id="sess", tool_name="create_memory", tool_input={}, timeout_seconds=5
    )
    await task

    assert decision.approved is True
    # The prompt and its outcome both reached the browser.
    kinds = [e["type"] for e in events]
    assert "tool_approval_request" in kinds
    assert "tool_approval_result" in kinds


@pytest.mark.asyncio
async def test_denial_carries_a_reason_to_the_model():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def deny():
        while not events:
            await asyncio.sleep(0)
        broker.resolve(
            events[0]["request_id"],
            answer={"decision": "deny", "note": "not that memory"},
            session_id="sess",
            user_id=None,
        )

    task = asyncio.create_task(deny())
    decision = await broker.request_approval(
        session_id="sess", tool_name="delete_memory", tool_input={}, timeout_seconds=5
    )
    await task

    assert decision.approved is False
    text = decision.as_tool_result("delete_memory")
    assert "delete_memory" in text or "delete memory" in text
    assert "not that memory" in text
    # The model must be told not to just retry.
    assert "Do not retry" in text


@pytest.mark.asyncio
async def test_approval_fails_closed_without_a_live_channel():
    """A conversation that asked for approval must not run tools unsupervised."""
    broker = InteractionBroker()
    decision = await broker.request_approval(
        session_id="missing", tool_name="create_memory", tool_input={}, timeout_seconds=5
    )
    assert decision.approved is False
    assert "could not reach you" in decision.note


@pytest.mark.asyncio
async def test_remembered_tool_is_not_asked_again():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def approve_once():
        while not events:
            await asyncio.sleep(0)
        broker.resolve(
            events[0]["request_id"],
            answer={"decision": "approve", "remember": True},
            session_id="sess",
            user_id=None,
        )

    task = asyncio.create_task(approve_once())
    first = await broker.request_approval(
        session_id="sess", tool_name="create_memory", tool_input={}, timeout_seconds=5
    )
    await task
    assert first.approved is True

    prompts_before = sum(1 for e in events if e["type"] == "tool_approval_request")
    second = await broker.request_approval(
        session_id="sess", tool_name="create_memory", tool_input={}, timeout_seconds=5
    )
    assert second.approved is True
    # No second prompt was shown.
    assert sum(1 for e in events if e["type"] == "tool_approval_request") == prompts_before


@pytest.mark.asyncio
async def test_closing_the_channel_releases_waiting_approval():
    """A turn that ends must not leave a coroutine parked until its timeout."""
    broker = InteractionBroker()
    broker.open_channel("sess", _record([]))

    task = asyncio.create_task(
        broker.request_approval(
            session_id="sess", tool_name="create_memory", tool_input={}, timeout_seconds=60
        )
    )
    await asyncio.sleep(0.05)
    broker.close_channel("sess")
    decision = await asyncio.wait_for(task, timeout=2)

    assert decision.approved is False


@pytest.mark.asyncio
async def test_timed_out_approval_denies_and_reports_result():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    decision = await broker.request_approval(
        session_id="sess", tool_name="create_memory", tool_input={}, timeout_seconds=1
    )
    assert decision.approved is False
    result = next(e for e in events if e["type"] == "tool_approval_result")
    assert result["decision"] == "deny"
    assert result["reason"] == "timed out"


@pytest.mark.asyncio
async def test_resolve_ignores_a_foreign_session():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    task = asyncio.create_task(
        broker.request_approval(
            session_id="sess", tool_name="create_memory", tool_input={}, timeout_seconds=30
        )
    )
    while not events:
        await asyncio.sleep(0)
    request_id = events[0]["request_id"]

    assert (
        broker.resolve(
            request_id,
            answer={"decision": "approve"},
            session_id="other",
            user_id=None,
        )
        is None
    )
    broker.close_channel("sess")
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_question_returns_the_answer():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def answer():
        while not events:
            await asyncio.sleep(0)
        broker.resolve(
            events[0]["request_id"],
            answer={"answer": "the second one", "choice": None},
            session_id="sess",
            user_id=None,
        )

    task = asyncio.create_task(answer())
    result = await broker.ask_question(
        session_id="sess",
        question="Which request did you mean?",
        choices=["the first", "the second"],
        allow_freeform=True,
        header="Clarify",
        timeout_seconds=5,
    )
    await task

    assert result["answered"] is True
    assert result["answer"] == "the second one"
    kinds = [e["type"] for e in events]
    assert "user_question" in kinds
    assert "user_question_result" in kinds


@pytest.mark.asyncio
async def test_question_without_a_channel_tells_the_model_to_use_a_handoff():
    broker = InteractionBroker()
    result = await broker.ask_question(
        session_id=None,
        question="Which one?",
        choices=[],
        allow_freeform=True,
        header="",
        timeout_seconds=5,
    )
    assert result["answered"] is False
    assert "send_handoff" in result["reason"]


@pytest.mark.asyncio
async def test_question_timeout_reports_unanswered():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    result = await broker.ask_question(
        session_id="sess",
        question="Which one?",
        choices=[],
        allow_freeform=True,
        header="",
        timeout_seconds=1,
    )
    assert result["answered"] is False
    assert "did not answer" in result["reason"]
    outcome = next(e for e in events if e["type"] == "user_question_result")
    assert outcome["answered"] is False


@pytest.mark.asyncio
async def test_pending_snapshot_is_scoped_to_its_session():
    broker = InteractionBroker()
    broker.open_channel("sess-a", _record([]))
    task = asyncio.create_task(
        broker.ask_question(
            session_id="sess-a",
            question="Which one?",
            choices=[],
            allow_freeform=True,
            header="",
            timeout_seconds=30,
        )
    )
    await asyncio.sleep(0.05)
    try:
        pending = broker.pending_for_session("sess-a")
        assert len(pending) == 1
        assert pending[0]["question"] == "Which one?"
        assert pending[0]["kind"] == "question"
        assert broker.pending_for_session("sess-b") == []
    finally:
        broker.close_channel("sess-a")
        await asyncio.wait_for(task, timeout=2)


def test_decision_as_tool_result_names_the_tool():
    decision = ToolApprovalDecision(approved=False, note="")
    text = decision.as_tool_result("mcp__memory-server__create_memory")
    assert "create_memory" in text
    assert "denied" in text.lower()


# ── batched clarifying questions ───────────────────────────────────────────


def test_normalize_choice_accepts_strings_and_objects():
    assert normalize_choice("Monday") == {
        "label": "Monday",
        "description": "",
        "recommended": False,
    }
    assert normalize_choice(
        {"label": "Friday", "description": "Ships sooner", "recommended": True}
    ) == {"label": "Friday", "description": "Ships sooner", "recommended": True}
    # Junk is dropped rather than rendering an unlabeled button.
    assert normalize_choice({"description": "no label"}) is None
    assert normalize_choice("   ") is None


def test_normalize_question_supplies_defaults_and_caps_choices():
    question = normalize_question(
        {
            "question": "Which day?",
            "header": "Launch",
            "choices": ["Monday"] + [f"opt{i}" for i in range(20)],
            "allow_free_form": False,
        }
    )
    assert question["question"] == "Which day?"
    assert question["header"] == "Launch"
    assert question["allow_free_form"] is False
    assert len(question["choices"]) == MAX_CHOICES_PER_QUESTION
    assert normalize_question({"header": "no question text"}) is None


def test_normalize_question_defaults_to_allowing_free_text():
    question = normalize_question({"question": "Which one?"})
    assert question["allow_free_form"] is True
    assert question["choices"] == []


@pytest.mark.asyncio
async def test_batched_questions_resolve_together():
    """One card, one wait, many answers — the whole point of batching."""
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def answer():
        while not events:
            await asyncio.sleep(0)
        prompt = events[0]
        assert prompt["kind"] == "question"
        # Every question reached the browser on a single prompt payload.
        assert [q["question"] for q in prompt["questions"]] == [
            "Which day?",
            "Which channel?",
        ]
        broker.resolve(
            prompt["request_id"],
            answer={
                "answers": [
                    {"answer": "Friday", "choice": "Friday"},
                    {"answer": "email"},
                ]
            },
            session_id="sess",
            user_id=None,
        )

    responder = asyncio.create_task(answer())
    result = await broker.ask_questions(
        session_id="sess",
        questions=[
            {"question": "Which day?", "choices": ["Thursday", "Friday"]},
            {"question": "Which channel?"},
        ],
        timeout_seconds=5,
    )
    await responder

    assert result["answered"] is True
    assert [entry["answer"] for entry in result["answers"]] == ["Friday", "email"]
    # Positional metadata lets the model match an answer to its question.
    assert result["answers"][0]["question"] == "Which day?"
    assert result["answers"][1]["choice"] is None


@pytest.mark.asyncio
async def test_batched_answers_report_partial_completion():
    """An unanswered question must be visible, not silently blank."""
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def answer():
        while not events:
            await asyncio.sleep(0)
        broker.resolve(
            events[0]["request_id"],
            answer={"answers": [{"answer": "Friday"}, {}]},
            session_id="sess",
            user_id=None,
        )

    responder = asyncio.create_task(answer())
    result = await broker.ask_questions(
        session_id="sess",
        questions=[{"question": "Which day?"}, {"question": "Which channel?"}],
        timeout_seconds=5,
    )
    await responder

    assert result["answers"][0]["answered"] is True
    assert result["answers"][1]["answered"] is False
    # `answered` overall is true because at least one arrived.
    assert result["answered"] is True


@pytest.mark.asyncio
async def test_question_count_is_capped():
    """Ten questions is an interrogation, not a clarification."""
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))
    questions = [{"question": f"Q{index}?"} for index in range(9)]

    task = asyncio.create_task(
        broker.ask_questions(
            session_id="sess", questions=questions, timeout_seconds=5
        )
    )
    while not events:
        await asyncio.sleep(0)
    assert len(events[0]["questions"]) == MAX_QUESTIONS_PER_REQUEST
    broker.resolve(
        events[0]["request_id"],
        answer={"answers": [{"answer": "x"} for _ in range(MAX_QUESTIONS_PER_REQUEST)]},
        session_id="sess",
        user_id=None,
    )
    result = await task
    assert len(result["answers"]) == MAX_QUESTIONS_PER_REQUEST


@pytest.mark.asyncio
async def test_recommended_choice_survives_to_the_browser_and_the_model():
    broker = InteractionBroker()
    events: list[dict] = []
    broker.open_channel("sess", _record(events))

    async def answer():
        while not events:
            await asyncio.sleep(0)
        prompt = events[0]
        choices = prompt["questions"][0]["choices"]
        assert choices[1] == {
            "label": "Friday",
            "description": "Ships sooner",
            "recommended": True,
        }
        broker.resolve(
            prompt["request_id"],
            answer={"answers": [{"answer": "Thursday", "choice": "Thursday"}]},
            session_id="sess",
            user_id=None,
        )

    responder = asyncio.create_task(answer())
    result = await broker.ask_questions(
        session_id="sess",
        questions=[{
            "question": "Which day?",
            "choices": [
                "Thursday",
                {"label": "Friday", "description": "Ships sooner", "recommended": True},
            ],
        }],
        timeout_seconds=5,
    )
    await responder

    # The model is told what it suggested, so it can notice an override.
    assert result["answers"][0]["recommended"] == "Friday"
    assert result["answers"][0]["answer"] == "Thursday"
