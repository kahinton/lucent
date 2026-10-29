"""Server-side gate that can require the person's permission before a tool runs.

Every engine Lucent runs (LangChain, Copilot, Claude Code, Codex) reaches
Lucent's tools through the same internal MCP server, so the decision "may this
tool run?" belongs here rather than in four engine-specific paths. The gate
reads the per-request approval mode that chat threaded onto the MCP connection
and, when the mode is ``ask`` and the tool can change state, asks the browser
and waits for the answer.

Outside chat there is no live channel: daemon tasks, workflows, and background
report turns carry no approval mode, so the mode resolves to ``auto`` and no
call ever waits on a human.
"""

from __future__ import annotations

from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.types import CallToolResult, TextContent

from lucent.llm.context import get_llm_context
from lucent.llm.tool_approval import (
    canonical_tool_name,
    interaction_broker,
    normalize_approval_mode,
    tool_requires_approval,
)
from lucent.logging import get_logger

logger = get_logger("llm.tool_approval_gate")

#: How long an approval prompt waits. Bounded well under the chat idle timeout
#: so an unanswered prompt surfaces as a refusal rather than a dead turn.
_DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120


def _approval_timeout_seconds() -> int:
    """Approval wait budget, from the runtime setting when it is readable."""
    try:
        from lucent.settings import chat_tool_approval_timeout_seconds

        return chat_tool_approval_timeout_seconds()
    except Exception:  # noqa: BLE001 — a settings read must never block a tool
        return _DEFAULT_APPROVAL_TIMEOUT_SECONDS


class ToolApprovalMiddleware(ServerMiddleware[Any]):
    """Hold a state-changing ``tools/call`` until the person approves it.

    Only ``tools/call`` is inspected. Reads, listings, and ``tools/list`` run
    untouched, so the approval mode costs nothing when it is not needed.
    """

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params or {}
        tool_name = canonical_tool_name(params.get("name"))
        context = get_llm_context()
        mode = normalize_approval_mode(context.get("tool_approval_mode"))
        if mode != "ask" or not tool_name or not tool_requires_approval(tool_name):
            return await call_next(ctx)

        session_id = context.get("session_id")
        arguments = params.get("arguments")
        decision = await interaction_broker.request_approval(
            session_id=session_id,
            tool_name=tool_name,
            tool_input=arguments if isinstance(arguments, dict) else {},
            timeout_seconds=_approval_timeout_seconds(),
        )
        if decision.approved:
            logger.info(
                "Tool %s approved for session %s", tool_name, session_id or "-"
            )
            return await call_next(ctx)

        logger.info(
            "Tool %s denied for session %s (%s)",
            tool_name,
            session_id or "-",
            decision.note or "no reason given",
        )
        # Flagged as a tool error on purpose: the tool did not run, and the MCP
        # client only validates structured output for non-error results — an
        # unflagged result would fail validation against the tool's declared
        # output schema and surface as a transport error instead of a refusal.
        # The text still reaches the model, which is what it needs to adapt.
        return CallToolResult(
            content=[TextContent(type="text", text=decision.as_tool_result(tool_name))],
            is_error=True,
        )


__all__ = ["ToolApprovalMiddleware"]
