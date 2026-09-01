"""Shared agent prompt composition.

Single source of truth for how an agent's granted skills and managed tools are
rendered into a system prompt. Both the daemon (sub-agent dispatch) and the
server (chat) import these helpers so an agent is composed identically no matter
which process runs it.

Skills are presented as an on-demand reference list (name, id, description). The
agent loads a skill's full instructions by calling ``get_skill_definition`` when
it needs them, mirroring how the cognitive identity references skills rather than
inlining every skill body into the prompt.
"""

from __future__ import annotations

import json
from typing import Any

SKILLS_HEADER = "--- SKILLS ---"
MANAGED_TOOLS_HEADER = "--- MANAGED TOOLS ---"

# Tool the agent must be able to call to load a skill's full instructions.
SKILL_FETCH_TOOL = "get_skill_definition"

_SKILLS_INTRO = (
    "These skills are reusable procedures available to you. Before doing work "
    f"that matches a skill, call `{SKILL_FETCH_TOOL}(skill_id)` to load its full "
    "instructions, then follow them. Do not guess a skill's contents from its "
    "name."
)

_MANAGED_TOOLS_INTRO = (
    "The following managed tools are granted to this agent. Use `run_managed_tool` "
    "only for the tools listed here; call it with `tool` set to the listed tool name "
    "and `arguments` set to an object matching that tool's input_schema. Lucent "
    "enforces the grant and runs each call in a sandbox. When the user's request "
    "requires a listed tool, invoke it rather than merely describing its availability."
)


def render_skills_section(skills: list[dict[str, Any]] | None) -> str:
    """Render granted skills as an on-demand reference list.

    Returns an empty string when there are no skills.
    """
    if not skills:
        return ""
    lines = [SKILLS_HEADER, _SKILLS_INTRO, ""]
    for skill in skills:
        name = skill.get("name") or "unnamed"
        skill_id = skill.get("id") or ""
        description = (skill.get("description") or "").strip()
        suffix = f" — {description}" if description else ""
        lines.append(f"- {name} (skill_id: {skill_id}){suffix}")
    return "\n".join(lines)


def render_managed_tools_section(tools: list[dict[str, Any]] | None) -> str:
    """Render granted managed tools with their input schemas.

    Returns an empty string when there are no tools.
    """
    if not tools:
        return ""
    tools = [
        tool
        for tool in tools
        if not (tool.get("runtime_config") or {}).get("mcp_proxy")
    ]
    if not tools:
        return ""
    lines = [MANAGED_TOOLS_HEADER, _MANAGED_TOOLS_INTRO, ""]
    for tool in tools:
        name = tool.get("name")
        description = tool.get("description") or ""
        schema = json.dumps(tool.get("input_schema") or {}, default=str)
        lines.append(f"- {name}: {description}\n  input_schema: {schema}")
    return "\n".join(lines)


def skill_names(skills: list[dict[str, Any]] | None) -> list[str]:
    """Extract the names of granted skills."""
    return [str(s["name"]) for s in (skills or []) if s.get("name")]


def managed_tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    """Extract the names of granted managed tools."""
    return [str(t["name"]) for t in (tools or []) if t.get("name")]
