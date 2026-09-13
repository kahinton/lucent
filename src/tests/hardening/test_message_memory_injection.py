"""Message-phase proactive memory injection pipeline tests.

Covers the M2 implementation of the living-memory inject-on-message design
(docs/design/living-memory-inject-pipeline.md):

1. ``extract_semantic_terms`` golden cases — deterministic, no search call:
   stopword-only messages yield no terms; paths/URLs/emails and pure numbers
   are dropped; order preserved; term cap applied (§3).
2. Round-1 turn guard — the hook acts only on the first model call after a
   user message, never on later tool-loop rounds (§5.3 rule 1).
3. Injection layer — client-side ``similarity_score`` threshold (~0.30),
   dedup-before-cap via the session-scoped ``injected_memory_ids`` set, and
   the zero-term short-circuit that must never reach the search API (§4, §5).
4. Cross-phase guard — ``query`` removed from FILE_ARGUMENT_KEYS so the
   pipeline's own searches cannot re-trigger the tool-phase hook (§5.4/defect 6).
5. Multi-turn extraction source (M3) — chat.py threads the raw latest user
   text via ``session_state["_latest_user_text"]``; the hook prefers it over
   the (possibly transcript-flattened) message content, falls back cleanly
   when the key is absent, and session dedup still suppresses repeats across
   turns that carry fresh terms.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lucent.llm.hooks import (
    FILE_ARGUMENT_KEYS,
    HookManager,
    build_message_lookup_plan,
    build_message_lookup_queries,
    extract_file_references,
    extract_semantic_terms,
    _is_first_model_call_after_user_message,
)


class FakeBridge:
    """Records call_tool invocations and replays canned search results.

    Tool-aware: ``search_memories_full`` replays ``results``; ``get_memories``
    replays ``full`` (a canned batched full-content fetch, keyed by id).
    """

    def __init__(
        self,
        results: list[dict[str, Any]],
        full: dict[str, str] | None = None,
    ):
        self.results = results
        self.full = full or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, payload: dict[str, Any]) -> str:
        self.calls.append((name, payload))
        if name == "get_memories":
            memories = [
                {"id": memory_id, "content": content}
                for memory_id, content in self.full.items()
                if not payload.get("memory_ids") or memory_id in payload["memory_ids"]
            ]
            return json.dumps({"memories": memories})
        return json.dumps({"memories": self.results})


def _memory(mid: str, score: float, content: str = "x") -> dict[str, Any]:
    return {"id": mid, "content": content, "tags": [], "similarity_score": score}


# ── extract_semantic_terms (design §3) ──────────────────────────────────────


def test_extract_terms_stopword_only_message_yields_nothing() -> None:
    assert extract_semantic_terms("Hey thanks! That is all for now, I think.") == []
    assert extract_semantic_terms("ok ok ok yes no it is the of to") == []


def test_extract_terms_keeps_meaningful_tokens_in_order() -> None:
    terms = extract_semantic_terms("I need to fix the memory consolidation in the daemon")
    assert terms == ["fix", "memory", "consolidation", "daemon"]


def test_extract_terms_drops_pure_numbers_numeric_fragments_and_short_tokens() -> None:
    terms = extract_semantic_terms("meeting at 2026 and 42 things")
    assert terms == ["meeting", "things"]
    # Sub-3-char fragments, contraction leftovers, numeric fragments (v1.2 -> v1/2).
    assert extract_semantic_terms("a an it don t v1.2 x@y.com 2026") == ["com"]
    # NOTE (design nuance): D4's path/URL token shapes cannot survive the \W+
    # split — path characters are delimiters, so "src/lucent" yields the words
    # "src"/"lucent". Those become legitimate terms; the client-side
    # similarity threshold (§4) is the guard against weak matches, by design.


def test_extract_terms_drops_path_url_code_shaped_tokens() -> None:
    # Pure punctuation/path tokens reduce to their word fragments or nothing.
    assert extract_semantic_terms("look at /var/log/ and {x} and ###") == ["look", "var", "log"]
    assert extract_semantic_terms("file:///etc/passwd") == ["file", "etc", "passwd"]


def test_extract_terms_caps_at_default_sixty_four_terms() -> None:
    terms = extract_semantic_terms(
        "memory consolidation pipeline injection threshold similarity dedup session engine "
        "retrieval ranking context handoff review workflow project dashboard"
    )
    assert len(terms) == 17
    assert terms[0] == "memory"
    assert terms[-1] == "dashboard"


def test_long_messages_keep_later_topics_in_bounded_followup_queries() -> None:
    terms = [f"topic{index}" for index in range(16)]
    assert build_message_lookup_queries(terms) == [
        "topic0 topic1 topic2 topic3 topic4 topic5 topic6 topic7",
        "topic8 topic9 topic10 topic11",
        "topic12 topic13 topic14 topic15",
    ]


def test_sprawling_sentence_query_windows_include_its_tail() -> None:
    queries = build_message_lookup_queries([f"topic{index}" for index in range(64)])
    assert len(queries) == 5
    assert queries[0].startswith("topic0")
    assert queries[-1].endswith("topic63")


def test_lookup_plan_covers_latest_request_anchors_and_message_positions() -> None:
    plan = build_message_lookup_plan(
        "Opening context about migration safety. "
        "The middle discusses service boundaries. "
        "Please investigate `src/auth/session.py` and fix Project Aurora."
    )
    assert plan[0] == {
        "source": "latest_request",
        "query": "investigate src auth session fix project aurora",
    }
    assert any(entry["source"] == "anchor" for entry in plan)
    assert any(entry["source"] == "middle" for entry in plan)
    assert any(entry["source"] == "opening" for entry in plan)


def test_extract_terms_empty_input() -> None:
    assert extract_semantic_terms("") == []
    assert extract_semantic_terms(None) == []  # type: ignore[arg-type]


# ── round-1 turn guard (design §5.3 rule 1) ────────────────────────────────


def test_round1_guard_first_model_call() -> None:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    assert _is_first_model_call_after_user_message(messages) is True


def test_round1_guard_blocks_later_rounds() -> None:
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": "r"},
    ]
    assert _is_first_model_call_after_user_message(messages) is False


def test_round1_guard_no_user_message() -> None:
    assert _is_first_model_call_after_user_message([{"role": "system", "content": "s"}]) is False


# ── injection layer (design §2, §4, §5) ────────────────────────────────────


@pytest.mark.asyncio
async def test_message_hook_injects_qualifying_memories_as_system_message_source() -> None:
    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.55, "signal memory"),
        _memory("22222222-2222-2222-2222-222222222222", 0.12, "noise memory"),
    ])
    state: dict[str, Any] = {}
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "how does threshold filtering work?"}],
        memory_bridge=bridge,
    )
    assert len(outcome.injectable_executions) == 1
    execution = outcome.injectable_executions[0]
    assert execution.hook_name == "message-memory-lookup"
    assert "signal memory" in execution.text
    assert "noise memory" not in execution.text
    assert execution.metadata["scores"] == [0.55]
    assert execution.metadata["skipped"] == 1
    # The search API was called with the extracted terms — nothing else.
    assert [(name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"] == [
        ("search_memories_full", "threshold filtering work")
    ]
    # Full content came from ONE batched get_memories call (hook-UX).
    assert [name for name, _payload in bridge.calls if name == "get_memories"] == ["get_memories"]
    assert state["injected_memory_ids"] == {"11111111-1111-1111-1111-111111111111"}


@pytest.mark.asyncio
async def test_message_hook_searches_later_long_message_topics() -> None:
    class QueryBridge(FakeBridge):
        async def call_tool(self, name: str, payload: dict[str, Any]) -> str:
            self.calls.append((name, payload))
            if name == "get_memories":
                return json.dumps({"memories": []})
            if payload["query"] == "later topic detail final":
                return json.dumps({"memories": [
                    _memory("12121212-1212-1212-1212-121212121212", 0.72, "later-topic memory"),
                ]})
            return json.dumps({"memories": []})

    bridge = QueryBridge([])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{
            "role": "user",
            "content": (
                "first second third fourth fifth sixth seventh eighth "
                "later topic detail final"
            ),
        }],
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    assert "later-topic memory" in execution.text
    assert execution.metadata["queries"] == [
        "first second third fourth fifth sixth seventh eighth",
        "later topic detail final",
    ]


@pytest.mark.asyncio
async def test_message_hook_rejects_results_far_below_their_query_winner() -> None:
    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.72, "strong match"),
        _memory("22222222-2222-2222-2222-222222222222", 0.45, "weak match"),
    ])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    execution = outcome.injectable_executions[0]
    assert "strong match" in execution.text
    assert "weak match" not in execution.text
    assert execution.metadata["below_score_spread"] == 1


@pytest.mark.asyncio
async def test_message_hook_zero_terms_skips_search_entirely() -> None:
    bridge = FakeBridge([])
    manager = HookManager()
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "thanks! that is all"}],
        memory_bridge=bridge,
    )
    assert outcome.injectable_executions == []
    assert bridge.calls == []  # zero-term short-circuit: no API call, no silence line


@pytest.mark.asyncio
async def test_message_hook_requires_memory_bridge() -> None:
    manager = HookManager()
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "meaningful question about memory"}],
        memory_bridge=None,
    )
    assert outcome.injectable_executions == []


@pytest.mark.asyncio
async def test_message_hook_noop_on_later_rounds() -> None:
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6)])
    manager = HookManager(session_state={})
    base = [{"role": "user", "content": "explain the injection threshold"}]
    first = await manager.before_model_call(messages=base, memory_bridge=bridge)
    assert len(first.injectable_executions) == 1
    later = await manager.before_model_call(
        messages=[*base, {"role": "assistant", "content": "a"}, {"role": "tool", "content": "r"}],
        memory_bridge=bridge,
    )
    assert later.injectable_executions == []
    assert len(later.executions) == 0  # round guard short-circuits pre-search
    assert len(bridge.calls) == 2  # turn-1 search + its batched get_memories
    assert [name for name, _ in bridge.calls].count("search_memories_full") == 1  # no re-query on round 2


@pytest.mark.asyncio
async def test_message_hook_dedups_across_turns_via_session_state() -> None:
    results = [_memory("11111111-1111-1111-1111-111111111111", 0.6, "same memory")]
    state: dict[str, Any] = {}
    manager = HookManager(session_state=state)
    messages = [{"role": "user", "content": "injection threshold question"}]
    first = await manager.before_model_call(messages=messages, memory_bridge=FakeBridge(results))
    assert len(first.injectable_executions) == 1
    # Fresh manager (new turn, new HookManager) sharing the same session state.
    second_manager = HookManager(session_state=state)
    second = await second_manager.before_model_call(
        messages=messages, memory_bridge=FakeBridge(results),
    )
    # Deduped, not re-injected — but the silence line still fires with the
    # dedup count (hook-UX): a no-injection turn is distinguishable from a
    # didn't-run turn.
    assert len(second.executions) == 1
    silence = second.executions[0]
    assert silence.text == "memory-lookup: 0 injected (1 deduped, 0 below threshold)"
    assert silence.metadata["deduped"] == 1
    assert silence.metadata["below_threshold"] == 0
    # The silence line IS injectable hook output: the engine renders it into
    # the model-visible hook block on this turn.
    assert len(second.injectable_executions) == 1


@pytest.mark.asyncio
async def test_message_hook_all_below_threshold_is_silent() -> None:
    bridge = FakeBridge([
        _memory("11111111-1111-1111-1111-111111111111", 0.2),
        _memory("22222222-2222-2222-2222-222222222222", 0.05),
    ])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "genuine topic words appear here"}],
        memory_bridge=bridge,
    )
    # Once the search ran, silence is disambiguated (hook-UX): one compact
    # line stating why nothing was injected. It renders via injectable path.
    assert len(outcome.executions) == 1
    silence = outcome.executions[0]
    assert silence.text == "memory-lookup: 0 injected (0 deduped, 2 below threshold)"
    assert len(outcome.injectable_executions) == 1


@pytest.mark.asyncio
async def test_message_hook_no_hits_emits_silence_line() -> None:
    bridge = FakeBridge([])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "obscure topic with zero hits"}],
        memory_bridge=bridge,
    )
    assert len(outcome.executions) == 1
    silence = outcome.executions[0]
    assert silence.text == "memory-lookup: 0 injected (0 deduped, 0 below threshold)"
    # The silence line is still hook output: metadata carries the query for
    # observability, and it renders via the injectable path.
    assert silence.metadata["memory_count"] == 0
    assert len(outcome.injectable_executions) == 1


@pytest.mark.asyncio
async def test_message_hook_injection_emits_no_silence_line() -> None:
    # When injection happened the hook block must NOT carry a silence line —
    # the "0 injected" line only exists on no-injection turns.
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.6, "hit memory")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    assert len(outcome.executions) == 1
    execution = outcome.executions[0]
    assert "memory-lookup: 0 injected" not in execution.text
    assert "Relevant accessible memories" in execution.text


@pytest.mark.asyncio
async def test_message_hook_result_includes_non_signal_metadata() -> None:
    bridge = FakeBridge([_memory("11111111-1111-1111-1111-111111111111", 0.5, "content here")])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "living memory pipeline"}],
        memory_bridge=bridge,
    )
    metadata = outcome.injectable_executions[0].metadata
    assert metadata["terms"] == ["living", "memory", "pipeline"]
    assert metadata["memory_ids"] == ["11111111"]
    assert metadata["min_similarity"] == pytest.approx(0.30)
    assert metadata["memory_count"] == 1


# ── cross-phase guard (design §5.4, defect 6) ──────────────────────────────


def test_query_removed_from_file_argument_keys() -> None:
    assert "query" not in FILE_ARGUMENT_KEYS


def test_search_query_argument_no_longer_triggers_file_extraction() -> None:
    refs = extract_file_references("search_memories_full", {"query": "some search words"})
    assert refs == []


# ── multi-turn extraction source (living-memory M3) ─────────────────────────


def _history_prompt_style_messages(turns: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Reproduce chat.py's multi-turn engine input: the prompt the engine sees
    is _history_prompt's flattened transcript, so on turn 2+ the hook's
    ``messages[-1]`` user content is the whole transcript, not the turn text."""
    transcript = "Previous conversation:\n"
    for index, (user, _assistant) in enumerate(turns[:-1]):
        transcript += f"User: {user}\n"
        transcript += f"Assistant: {_assistant}\n"
    transcript += f"\nUser: {turns[-1][0]}"
    return [{"role": "user", "content": transcript}]


@pytest.mark.asyncio
async def test_message_hook_extracts_from_threaded_raw_text_on_later_turns() -> None:
    # Turn 2 of a conversation: the flattened transcript in the user message
    # would yield stale head terms; the threaded raw text must win instead.
    bridge = FakeBridge([
        _memory("33333333-3333-3333-3333-333333333333", 0.6, "public site plan"),
    ])
    state: dict[str, Any] = {"_latest_user_text": "now let's plan the public site"}
    manager = HookManager(session_state=state)
    messages = _history_prompt_style_messages([
        ("give feedback on the discussion post", "Sure."),
        ("now let's plan the public site", None),
    ])
    outcome = await manager.before_model_call(messages=messages, memory_bridge=bridge)
    assert len(outcome.injectable_executions) == 1
    execution = outcome.injectable_executions[0]
    assert execution.metadata["terms"] == ["plan", "public", "site"]
    # Search used the threaded turn's terms — never the transcript head.
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [
        ("search_memories_full", "plan public site")
    ]
    # The threaded key is pure hook input: dedup bookkeeping is untouched.
    assert "_latest_user_text" in state
    assert state["injected_memory_ids"] == {"33333333-3333-3333-3333-333333333333"}
    # Search used the threaded turn's terms; full content came from one batched get.
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "plan public site")]
    assert [name for name, _ in bridge.calls if name == "get_memories"] == ["get_memories"]


@pytest.mark.asyncio
async def test_message_hook_falls_back_to_message_content_without_threaded_text() -> None:
    # No ``_latest_user_text`` (fresh-conversation first message via engines
    # that thread no session state, or non-chat callers): the hook must still
    # extract from the latest user message as before.
    bridge = FakeBridge([
        _memory("44444444-4444-4444-4444-444444444444", 0.6, "threshold memory"),
    ])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "explain the injection threshold"}],
        memory_bridge=bridge,
    )
    assert len(outcome.injectable_executions) == 1
    assert outcome.injectable_executions[0].metadata["terms"] == [
        "explain", "injection", "threshold",
    ]
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "explain injection threshold")]


@pytest.mark.asyncio
async def test_message_hook_dedup_still_suppresses_with_fresh_terms() -> None:
    # Turn 2 brings a genuinely new topic (fresh terms, new query), but the
    # search still returns turn 1's memories — dedup must suppress them and
    # only inject new qualifying memories.
    state: dict[str, Any] = {"_latest_user_text": "now let's plan the public site"}
    first = FakeBridge([
        _memory("55555555-5555-5555-5555-555555555555", 0.7, "feedback memory"),
    ])
    first_manager = HookManager(session_state=state)
    first_outcome = await first_manager.before_model_call(
        messages=[{"role": "user", "content": "give feedback on the discussion post"}],
        memory_bridge=first,
    )
    assert len(first_outcome.injectable_executions) == 1
    assert state["injected_memory_ids"] == {"55555555-5555-5555-5555-555555555555"}
    second = FakeBridge([
        _memory("55555555-5555-5555-5555-555555555555", 0.7, "feedback memory"),
        _memory("66666666-6666-6666-6666-666666666666", 0.65, "site plan memory"),
    ])
    second_manager = HookManager(session_state=state)
    second_outcome = await second_manager.before_model_call(
        messages=[{"role": "user", "content": "now let's plan the public site"}],
        memory_bridge=second,
    )
    # Turn-1 memory deduped; the fresh-topic memory still injected.
    ids = second_outcome.injectable_executions[0].metadata["memory_ids"]
    assert ids == ["66666666"]
    assert "55555555" not in ids
    assert state["injected_memory_ids"] == {
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666",
    }
    # Each turn searched its own threaded terms (turn 2 did not reuse turn 1).
    assert [
        (name, payload["query"]) for name, payload in second.calls if name == "search_memories_full"
    ] == [
        ("search_memories_full", "plan public site")
    ]

# ── hook-UX: full-content injection + explicit silence line (2026-09-10) ────


@pytest.mark.asyncio
async def test_message_hook_injects_full_content_from_batched_get_memories() -> None:
    # Search returns truncated previews; the hook must fetch full content for
    # the selected ids with exactly ONE batched get_memories call and inject
    # that instead of the preview.
    preview = "preview of the memory content"
    bridge = FakeBridge(
        [_memory("11111111-1111-1111-1111-111111111111", 0.6, preview)],
        full={"11111111-1111-1111-1111-111111111111": "FULL body of the memory, " * 80},
    )
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    assert len(outcome.executions) == 1
    text = outcome.executions[0].text
    metadata = outcome.executions[0].metadata
    # Full content injected, not the truncated preview.
    assert "FULL body of the memory" in text
    assert preview not in text
    # Exactly one get_memories call carrying ALL selected ids (batched).
    get_calls = [(name, payload) for name, payload in bridge.calls if name == "get_memories"]
    assert len(get_calls) == 1
    assert get_calls[0][1] == {
        "memory_ids": ["11111111-1111-1111-1111-111111111111"],
    }
    assert metadata["full_content_count"] == 1
    assert metadata["full_content_bytes"] > 0
    # The search API itself is untouched: same single search call as before.
    search_calls = [
        (name, payload["query"]) for name, payload in bridge.calls
        if name == "search_memories_full"
    ]
    assert search_calls == [("search_memories_full", "injection threshold question")]


@pytest.mark.asyncio
async def test_message_hook_budget_fallback_to_preview() -> None:
    # First result fits the byte budget and gets full content; the second
    # pushes past the budget and keeps its 1000-char preview line.
    full_first = "F" * 4000
    full_second = "S" * 4000
    preview_second = "second preview truncated"
    bridge = FakeBridge(
        [
            _memory("11111111-1111-1111-1111-111111111111", 0.6, "first preview"),
            _memory("22222222-2222-2222-2222-222222222222", 0.55, preview_second),
        ],
        full={
            "11111111-1111-1111-1111-111111111111": full_first,
            "22222222-2222-2222-2222-222222222222": full_second,
        },
    )
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    text = outcome.executions[0].text
    metadata = outcome.executions[0].metadata
    assert full_first in text
    assert preview_second in text  # over budget → preview line
    assert full_second not in text
    assert metadata["full_content_count"] == 1
    assert metadata["full_content_bytes"] == len(full_first.encode("utf-8"))
    assert [entry["full"] for entry in metadata["memories"]] == [True, False]


@pytest.mark.asyncio
async def test_message_hook_full_fetch_failure_falls_back_to_preview() -> None:
    # A failing get_memories call must degrade to the old preview-only
    # injection, not break the hook.
    class ExplodingFullBridge(FakeBridge):
        async def call_tool(self, name: str, payload: dict[str, Any]) -> str:
            if name == "get_memories":
                raise RuntimeError("memory server hiccup")
            return await super().call_tool(name, payload)

    preview = "preview content survives"
    bridge = ExplodingFullBridge(
        [_memory("11111111-1111-1111-1111-111111111111", 0.6, preview)],
    )
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    assert len(outcome.executions) == 1
    text = outcome.executions[0].text
    assert "Relevant accessible memories" in text
    assert preview in text
    assert outcome.executions[0].metadata["full_content_count"] == 0


@pytest.mark.asyncio
async def test_message_hook_no_memories_returned_from_get_memories_falls_back() -> None:
    # get_memories returning nothing (e.g. deleted between search and get)
    # falls back to the search preview — no crash, no empty injection.
    bridge = FakeBridge(
        [_memory("11111111-1111-1111-1111-111111111111", 0.6, "preview fallback")],
        full={},
    )
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    text = outcome.executions[0].text
    assert "preview fallback" in text
    assert outcome.executions[0].metadata["full_content_count"] == 0


@pytest.mark.asyncio
async def test_message_hook_multiline_full_content_injected_verbatim() -> None:
    # Full content is a memory's markdown body — multi-line. It must be
    # injected as-is (the single-line flattening applies only to previews).
    full = "## Heading\n\nbody line\n- bullet"
    bridge = FakeBridge(
        [_memory("11111111-1111-1111-1111-111111111111", 0.6, "flat preview")],
        full={"11111111-1111-1111-1111-111111111111": full},
    )
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "injection threshold question"}],
        memory_bridge=bridge,
    )
    assert full in outcome.executions[0].text


@pytest.mark.asyncio
async def test_hook_runtime_error_emitted_as_hook_error_line() -> None:
    # Hook runtime errors must surface as ``hook error: <class>`` in the hook
    # block (hook-UX) instead of vanishing — distinguishable from silence.
    # Static-context runner monkeypatched to raise inside _run_event_hooks.
    import lucent.llm.hooks as hooks_mod

    manager = HookManager(hooks=[
        {
            "name": "exploding-hook",
            "trigger_event": "before_model_call",
            "action_type": "static_context",
            "content": "",
            "config": {},
        },
    ])
    original = hooks_mod._run_static_context_hook

    def _raise(**_kwargs):
        raise ValueError("boom")

    hooks_mod._run_static_context_hook = _raise
    try:
        outcome = await manager._run_event_hooks(
            event="before_model_call",
            messages=[{"role": "user", "content": "anything"}],
        )
    finally:
        hooks_mod._run_static_context_hook = original
    assert len(outcome.executions) == 1
    execution = outcome.executions[0]
    assert execution.text == "hook error: ValueError"
    assert execution.metadata["hook_error"] == "ValueError"
    assert execution.hook_name == "exploding-hook"


# ── turn-anchoring (2026-09-11 fix): explicit current-message text ──────────
#
# Turn-1 silence regression class (request 1c38345a): the hook's extraction
# source must be the CURRENT user message every turn — round 1 of the turn's
# model loop — never a session-state side channel whose write ordering can
# drift, and never the transcript-flattened prompt. The engine now threads
# ``current_user_text`` explicitly; chat.py passes it every turn.


@pytest.mark.asyncio
async def test_message_hook_fires_on_first_turn_with_explicit_text() -> None:
    # Turn 1 of a conversation: no session state at all (fresh engine call),
    # but the engine threads the current message explicitly. The hook must
    # search — a turn-1 zero-candidate search emits the silence line, it is
    # NEVER a silent no-op.
    bridge = FakeBridge([])
    manager = HookManager(session_state={})
    outcome = await manager.before_model_call(
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "tell me about the injection threshold"},
        ],
        memory_bridge=bridge,
        current_user_text="tell me about the injection threshold",
    )
    assert [(name, payload["query"]) for name, payload in bridge.calls] == [
        ("search_memories_full", "tell injection threshold"),
    ]
    assert len(outcome.executions) == 1
    assert outcome.executions[0].text == (
        "memory-lookup: 0 injected (0 deduped, 0 below threshold)"
    )


@pytest.mark.asyncio
async def test_message_hook_explicit_text_wins_over_stale_threaded_text() -> None:
    # Fragile-ordering regression: session_state still holds the PREVIOUS
    # turn's text (side channel not yet rewritten this turn) while the engine
    # passes the CURRENT message explicitly. The explicit text must win —
    # the hook must never search the prior turn's topic.
    bridge = FakeBridge([
        _memory("77777777-7777-7777-7777-777777777777", 0.6, "consolidation memory"),
    ])
    state: dict[str, Any] = {"_latest_user_text": "plan the public site launch"}
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[
            {"role": "user", "content": "how does the daemon sleep cycle work"},
        ],
        memory_bridge=bridge,
        current_user_text="how does the daemon sleep cycle work",
    )
    assert outcome.injectable_executions[0].metadata["terms"] == [
        "daemon", "sleep", "cycle", "work",
    ]
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "daemon sleep cycle work")]


@pytest.mark.asyncio
async def test_message_hook_threaded_text_beats_message_fallback() -> None:
    # Legacy channel still honored when no explicit text is passed: threaded
    # session-state text must win over the (flattened) message content.
    bridge = FakeBridge([
        _memory("88888888-8888-8888-8888-888888888888", 0.6, "site plan memory"),
    ])
    state: dict[str, Any] = {"_latest_user_text": "plan the public site"}
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[
            {"role": "user", "content": "Previous conversation:\nUser: give feedback"},
        ],
        memory_bridge=bridge,
    )
    assert outcome.injectable_executions[0].metadata["terms"] == [
        "plan", "public", "site",
    ]


@pytest.mark.asyncio
async def test_message_hook_silence_line_all_filtered_results() -> None:
    # Post-search no-injection turns must render an explicit silence line —
    # candidates exist but every one is filtered (below threshold here), so
    # the counters name the reason instead of looking like a didn't-run turn.
    bridge = FakeBridge([
        _memory("99999999-9999-9999-9999-999999999999", 0.10, "weak match"),
        _memory("9aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 0.15, "weaker match"),
    ])
    state: dict[str, Any] = {"_latest_user_text": "explain the injection threshold"}
    manager = HookManager(session_state=state)
    outcome = await manager.before_model_call(
        messages=[{"role": "user", "content": "explain the injection threshold"}],
        memory_bridge=bridge,
        current_user_text="explain the injection threshold",
    )
    assert len(outcome.executions) == 1
    execution = outcome.executions[0]
    assert execution.text == "memory-lookup: 0 injected (0 deduped, 2 below threshold)"
    assert execution.metadata["below_threshold"] == 2
    assert execution.metadata["deduped"] == 0
    assert execution.metadata["memory_count"] == 0
    # Search ran with the explicit current-turn text.
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "explain injection threshold")]


@pytest.mark.asyncio
async def test_message_hook_silence_line_after_dedup_suppression() -> None:
    # Same memory qualifies on turn 2 but was injected on turn 1: dedup
    # suppression is a post-search no-injection turn, so it renders the
    # silence line with the deduped counter — never a silent no-op.
    state: dict[str, Any] = {}
    first = FakeBridge([
        _memory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 0.7, "sleep cycle memory"),
    ])
    first_manager = HookManager(session_state=state)
    await first_manager.before_model_call(
        messages=[{"role": "user", "content": "explain the daemon sleep cycle"}],
        memory_bridge=first,
        current_user_text="explain the daemon sleep cycle",
    )
    assert state["injected_memory_ids"] == {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
    second = FakeBridge([
        _memory("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 0.7, "sleep cycle memory"),
    ])
    second_manager = HookManager(session_state=state)
    outcome = await second_manager.before_model_call(
        messages=[{"role": "user", "content": "explain the daemon sleep cycle again"}],
        memory_bridge=second,
        current_user_text="explain the daemon sleep cycle again",
    )
    # No memory injected — but the silence line IS emitted as an injectable
    # execution (that is how it reaches the model and the hook chip), with
    # the deduped counter naming the reason.
    assert len(outcome.executions) == 1
    assert outcome.executions[0].text == (
        "memory-lookup: 0 injected (1 deduped, 0 below threshold)"
    )
    assert outcome.executions[0].metadata["deduped"] == 1


# ── engine-level round-1 wiring (langchain_engine._run_with_tools) ─────────


class _StubChatModel:
    """Minimal LangChain-compatible chat model stub (plain-text answer)."""

    def bind_tools(self, _schemas):
        return self

    async def ainvoke(self, _messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content="plain answer")


async def _run_engine_turn(
    *,
    prompt: str,
    message_history: list[dict[str, Any]] | None,
    current_user_text: str | None,
    session_state: dict[str, Any] | None,
    bridge: FakeBridge,
) -> str | None:
    """Drive one real ``LangChainEngine._run_with_tools`` turn with stubs.

    Stubs: the chat model (no provider/model lookups) and the MCP bridge
    factory (no network) so the memory bridge — and therefore the message-
    memory-lookup hook — is exercised through the engine's real round-1
    wiring. ``langchain_max_tool_rounds`` is pinned to 1 so the loop
    terminates after the stub's plain-text answer.
    """
    import lucent.llm.langchain_engine as engine_mod
    from lucent.llm.langchain_engine import LangChainEngine

    engine = LangChainEngine()

    async def _fake_get_chat_model(_model, **_kwargs):
        return _StubChatModel()

    async def _fake_create_bridges(_self, _mcp_config, audit_context=None):
        return [], {}, bridge, []

    original_get_model = engine_mod._get_chat_model
    original_create_bridges = LangChainEngine._create_bridges
    original_rounds = engine_mod.runtime_settings.langchain_max_tool_rounds
    engine_mod._get_chat_model = _fake_get_chat_model
    LangChainEngine._create_bridges = _fake_create_bridges
    engine_mod.runtime_settings.langchain_max_tool_rounds = (
        lambda organization_id=None: 1
    )
    try:
        return await engine._run_with_tools(
            model="stub-model",
            system_message="system",
            prompt=prompt,
            mcp_config={"stub-memory": {"type": "http", "url": "http://stub.invalid"}},
            on_event=None,
            message_history=message_history,
            hooks=None,
            audit_context=None,
            approve_permissions=False,
            managed_tools=None,
            expected_surface=None,
            session_state=session_state,
            current_user_text=current_user_text,
        )
    finally:
        engine_mod._get_chat_model = original_get_model
        LangChainEngine._create_bridges = original_create_bridges
        engine_mod.runtime_settings.langchain_max_tool_rounds = original_rounds


@pytest.mark.asyncio
async def test_engine_message_hook_fires_on_turn_1() -> None:
    # Full engine path, turn 1 of a conversation: no prior history, no
    # session state, no explicit text. The hook must fire on the fresh-
    # conversation fallback (the prompt IS the current turn's text) — a
    # zero-candidate turn-1 search emits the silence line and is NEVER a
    # silent no-op. Regression for the turn-1 "total silence" report.
    bridge = FakeBridge([])
    result = await _run_engine_turn(
        message_history=None,
        current_user_text=None,
        session_state={},
        bridge=bridge,
        prompt="explain the injection threshold",
    )
    assert result == "plain answer"
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "explain injection threshold")]


@pytest.mark.asyncio
async def test_engine_message_hook_uses_current_not_prior_text() -> None:
    # Turn 2 with a flattened transcript prompt: the engine's threaded
    # current_user_text must win over the transcript head. The search must
    # use turn 2's terms — never turn 1's.
    bridge = FakeBridge([])
    await _run_engine_turn(
        message_history=[
            {"role": "user", "content": "plan the public site launch"},
            {"role": "assistant", "content": "Sounds good."},
        ],
        current_user_text="how does the daemon sleep cycle work",
        session_state={"_latest_user_text": "plan the public site launch"},
        bridge=bridge,
        prompt=(
            "Previous conversation:\n"
            "User: plan the public site launch\n"
            "Assistant: Sounds good.\n\n"
            "User: how does the daemon sleep cycle work"
        ),
    )
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "daemon sleep cycle work")]


@pytest.mark.asyncio
async def test_engine_message_hook_falls_back_to_loop_last_human_message() -> None:
    # No explicit text (caller without the kwarg, e.g. a daemon-style session
    # whose prompt is the raw turn text): the engine derives the current
    # turn's text from the LAST human message in the loop. Round-1 fires.
    bridge = FakeBridge([])
    await _run_engine_turn(
        message_history=[
            {"role": "user", "content": "plan the public site launch"},
            {"role": "assistant", "content": "Sounds good."},
        ],
        current_user_text=None,
        session_state={},
        bridge=bridge,
        prompt="how does the daemon sleep cycle work",
    )
    assert [
        (name, payload["query"]) for name, payload in bridge.calls if name == "search_memories_full"
    ] == [("search_memories_full", "daemon sleep cycle work")]


# ── engine API pass-through (current_user_text reaches the hook manager) ────


@pytest.mark.asyncio
async def test_engine_run_session_streaming_passes_current_user_text() -> None:
    # The session API threads the kwarg into _run_with_tools, which reaches
    # the hook as the explicit extraction source (turn 2 of a conversation,
    # prior turn's text still in the legacy side channel).
    from lucent.llm.langchain_engine import LangChainEngine

    engine = LangChainEngine()
    captured: dict[str, Any] = {}

    async def _fake_run_with_tools(self, **kwargs):
        captured.update(kwargs)
        return "ok"

    original = LangChainEngine._run_with_tools
    LangChainEngine._run_with_tools = _fake_run_with_tools
    try:
        result = await engine.run_session_streaming(
            model="stub-model",
            system_message="system",
            prompt="prompt",
            current_user_text="how does the daemon sleep cycle work",
            session_state={"_latest_user_text": "plan the public site launch"},
        )
    finally:
        LangChainEngine._run_with_tools = original
    assert result == "ok"
    assert captured["current_user_text"] == "how does the daemon sleep cycle work"
    assert captured["session_state"] == {"_latest_user_text": "plan the public site launch"}
