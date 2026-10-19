"""Prompt-shape tests for request decomposition with execution pins."""

from __future__ import annotations

from daemon.decomposition.fallback import build_decomposition_prompt

_DESCRIPTION = "Implement the widget end to end and persist the docs in the repo."


def test_prompt_includes_description_and_pinned_execution_target():
    prompt = build_decomposition_prompt(
        {
            "request_id": "request-1",
            "title": "Build the widget",
            "description": _DESCRIPTION,
            "priority": "high",
            "backend_id": "backend-123",
            "requested_model": "qwen3-coder",
        }
    )

    assert _DESCRIPTION in prompt
    assert "\nPINNED BACKEND: backend-123" in prompt
    assert "\nPINNED MODEL: qwen3-coder" in prompt
    assert "The request author already chose this execution target" in prompt
    # The procedure must tell the planner to carry the pin onto every task.
    assert "backend_id=<optional>" in prompt
    assert "pass backend_id on every task" in prompt


def test_prompt_without_pins_omits_pinned_blocks():
    prompt = build_decomposition_prompt(
        {
            "request_id": "request-2",
            "title": "Unpinned request",
            "description": _DESCRIPTION,
            "priority": "medium",
        }
    )

    assert _DESCRIPTION in prompt
    # The generic procedure mentions "when PINNED BACKEND is set", so assert on
    # the rendered block markers rather than the bare words.
    assert "\nPINNED BACKEND:" not in prompt
    assert "\nPINNED MODEL:" not in prompt
    assert "The request author already chose this execution target" not in prompt
