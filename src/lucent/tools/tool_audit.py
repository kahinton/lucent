"""MCP tools for tool-call audit analysis and improvement proposals."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from lucent.db import DefinitionRepository, ToolAuditRepository, get_pool
from lucent.tools.memories import _get_current_user_context


def _serialize(obj: Any) -> str:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj)


def _parse_json_object(value: dict | str | None) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value or "{}")
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("proposal_evidence/config must be a JSON object")


def _owner_args_for_context(
    user_id: UUID,
    user_role: str | None,
    memory_scope: str | None,
) -> dict:
    if user_role == "daemon" and memory_scope != "user":
        return {"shared_with_org": True}
    return {"owner_user_id": str(user_id)}


def register_tool_audit_tools(mcp: FastMCP) -> None:
    """Register tool audit analysis tools with the MCP server."""

    @mcp.tool(
        description="""Analyze repeated tool-call failures from tool_call_audit_log.

Use during learning extraction or definition improvement planning. This reads
operational audit rows, not memories, and returns aggregated failure patterns
with evidence suitable for proposing agent/skill/hook updates.

Args:
    since_days: Lookback window in days (default 7, max 90)
    min_failures: Minimum repeated failures before reporting a pattern (default 3)
    limit: Max patterns to return (default 20)
    summary_only: When True, return counts + pattern keys only (no per-failure
        evidence). Use this when the caller only needs the pattern list — e.g.
        to keep the response well under tool-call ingestion limits.

Returns: JSON containing repeated failure patterns, evidence, and recommended actions."""
    )
    async def analyze_tool_failure_patterns(
        since_days: int = 7,
        min_failures: int = 3,
        limit: int = 20,
        summary_only: bool = False,
    ) -> str:
        _user_id, org_id, _role, _memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        pool = await get_pool()
        repo = ToolAuditRepository(pool)
        result = await repo.analyze_failure_patterns(
            org_id=str(org_id),
            since_days=since_days,
            min_failures=min_failures,
            limit=limit,
            summary_only=summary_only,
        )
        result["note"] = (
            "Use propose_definition_improvement for patterns with enough evidence. "
            "Do not create memories for raw audit rows; only definition proposals "
            "or later distilled learning memories should be created."
        )
        return json.dumps(result, default=_serialize)

    @mcp.tool(
        description="""Propose an agent/skill/hook definition improvement for human approval.

Use after analyze_tool_failure_patterns finds a repeated failure pattern. The
proposal starts in status='proposed' and must be approved before it changes
runtime behavior. Always include proposal_reason and proposal_evidence so the
reviewer can see why the change is suggested.

Recommended use:
- Repeated failures by one agent/tool -> propose a focused skill and include
  recommended_agent_id/recommended_agent_type in evidence.
- Repeated failures tied to an existing skill -> propose an improved skill.
- Cross-agent failures for one tool -> propose a generic skill or hook.

Args:
    definition_type: 'skill', 'agent', or 'hook'
    name: Proposed definition name
    description: Short description shown in proposal lists
    content: Full definition content (SKILL.md, AGENT.md, or hook content)
    proposal_reason: Human-readable reason for the suggestion
    proposal_evidence: JSON object from analyze_tool_failure_patterns or similar evidence
    recommended_agent_id: Optional agent ID this proposed skill should later be granted to
    recommended_agent_type: Optional agent type/name this proposal targets
    trigger_event: Hook trigger event when definition_type='hook'
    action_type: Hook action type when definition_type='hook'
    config: Hook config JSON object when definition_type='hook'

Returns: JSON with the proposed definition ID/status and review note."""
    )
    async def propose_definition_improvement(
        definition_type: str,
        name: str,
        description: str,
        content: str,
        proposal_reason: str,
        proposal_evidence: dict | str | None = None,
        recommended_agent_id: str = "",
        recommended_agent_type: str = "",
        trigger_event: str = "before_tool_call",
        action_type: str = "static_context",
        config: dict | str | None = None,
    ) -> str:
        user_id, org_id, user_role, memory_scope, _ = await _get_current_user_context()
        if not org_id:
            return json.dumps({"error": "No organization context"})
        if not user_id:
            return json.dumps({"error": "No user context"})
        if definition_type not in {"agent", "skill", "hook"}:
            return json.dumps({"error": "definition_type must be 'agent', 'skill', or 'hook'"})
        if not name or len(name) > 64:
            return json.dumps({"error": "name is required and must be <= 64 characters"})
        if not content:
            return json.dumps({"error": "content is required"})
        if not proposal_reason:
            return json.dumps({"error": "proposal_reason is required"})
        try:
            evidence = _parse_json_object(proposal_evidence)
            hook_config = _parse_json_object(config)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if recommended_agent_id:
            evidence.setdefault("recommended_agent_id", recommended_agent_id)
        if recommended_agent_type:
            evidence.setdefault("recommended_agent_type", recommended_agent_type)
        evidence.setdefault("proposal_source", "tool_failure_learning")

        repo = DefinitionRepository(await get_pool())
        owner_args = _owner_args_for_context(user_id, user_role, memory_scope)
        try:
            if definition_type == "agent":
                result = await repo.create_agent(
                    name=name,
                    description=description,
                    content=content,
                    org_id=str(org_id),
                    created_by=str(user_id),
                    proposal_reason=proposal_reason,
                    proposal_evidence=evidence,
                    **owner_args,
                )
            elif definition_type == "skill":
                result = await repo.create_skill(
                    name=name,
                    description=description,
                    content=content,
                    org_id=str(org_id),
                    created_by=str(user_id),
                    proposal_reason=proposal_reason,
                    proposal_evidence=evidence,
                    **owner_args,
                )
            else:
                result = await repo.create_hook(
                    name=name,
                    description=description,
                    trigger_event=trigger_event,
                    action_type=action_type,
                    content=content,
                    config=hook_config,
                    org_id=str(org_id),
                    created_by=str(user_id),
                    proposal_reason=proposal_reason,
                    proposal_evidence=evidence,
                    **owner_args,
                )
        except Exception as exc:
            return json.dumps({"error": f"Failed to create proposal: {exc}"})

        return json.dumps(
            {
                "id": str(result["id"]),
                "name": result["name"],
                "definition_type": definition_type,
                "status": result["status"],
                "proposal_reason": result.get("proposal_reason"),
                "proposal_evidence": result.get("proposal_evidence") or evidence,
                "note": (
                    "Submitted for human review. Approval is required before this "
                    "definition changes agent behavior. If this is a skill proposal, "
                    "grant it to the recommended agent after approval."
                ),
            },
            default=_serialize,
        )
