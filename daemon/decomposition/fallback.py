"""Pure prompt and fallback-task construction for request decomposition."""

from __future__ import annotations

import re
from typing import Any


def build_decomposition_prompt(request: dict[str, Any]) -> str:
    """Build a focused prompt that decomposes exactly one existing request."""
    title = request.get("title", "")
    description = request.get("description", "") or ""
    request_id = request.get("request_id", "")
    target_repo = request.get("target_repo") or ""
    target_paths = request.get("target_paths") or []
    priority = request.get("priority") or "medium"
    milestone_index = request.get("goal_milestone_index")
    milestone_description = (request.get("goal_milestone_description") or "").strip()
    target_block = f"\n- target_repo: {target_repo}" if target_repo else ""
    if target_paths:
        target_block += f"\n- target_paths: {target_paths}"
    milestone_block = ""
    if milestone_index and milestone_description:
        milestone_block = (
            "\n\nSTRUCTURED GOAL SCOPE:\n"
            f"This request advances ONLY goal milestone {milestone_index}: "
            f"{milestone_description}\n"
            "Create tasks only for this milestone. Do not decompose the whole goal, "
            "do not create tasks for later milestones, and ignore any parent-goal "
            "description sections that are unrelated to this milestone."
        )
    return (
        "You are running a focused decomposition session. Your ONLY job is to "
        "break the following request into a sensible list of tasks so the user "
        "can see the breakdown before they decide whether to approve it.\n\n"
        f"REQUEST ID: {request_id}\n"
        f"TITLE: {title}\n"
        f"PRIORITY: {priority}\n"
        f"{target_block}\n\n"
        f"{milestone_block}\n\n"
        f"DESCRIPTION:\n{description}\n\n"
        "Procedure:\n"
        "1. Call list_available_models() once to see enabled models and the "
        "default_model. Use default_model unless a task has a clear need for a "
        "specialized fast, reasoning, agentic, or visual model.\n"
        "2. Decide on a task breakdown — typically 1 to 5 tasks. Each task should "
        "have a clear, actionable title and a short description of what it does.\n"
        "3. Call list_agent_definitions(status='active') and choose only "
        "agent_type names from agents visible to this request owner. For each task, "
        "call create_task(request_id=..., title=..., description=..., "
        "agent_type=..., sequence_order=..., model=<optional>). Omit model for "
        "standard/default tasks; set model only when "
        "the available model list gives a concrete reason to specialize. Set "
        "sequence_order so dependent tasks come after their prerequisites (0 for "
        "the first batch, 1 for the next, etc.).\n"
        "   When two or more sequential tasks need the same isolated workspace, "
        "call list_sandbox_templates() and use the same approved "
        "sandbox_template_id for each task with "
        "sandbox_overrides={\"reuse_within_request\": true}. They MUST use "
        "strictly increasing sequence_order values; parallel tasks must never "
        "share a sandbox.\n"
        "   If the request has target_repo or asks for docs/files/reports that belong "
        "in a repository, every relevant task description MUST name the exact target "
        "repo/path(s), require durable file persistence, require reporting paths plus "
        "commit/URL, and require record_task_output when possible. Memory-only or "
        "chat-only deliverables must be treated as incomplete.\n"
        "4. DO NOT create the request — it already exists. DO NOT call any "
        "approval, status update, or rejection tools. Your output is a short "
        "summary of how many tasks you created and why this breakdown.\n"
        "5. If the request description is too vague to break down meaningfully, "
        "create a single 'research' task to investigate further and stop there.\n\n"
        "Stay focused. Do not search memories, do not create new requests, do "
        "not start sub-agents. Just call create_task one or more times for the "
        "above request, then return your summary."
    )


def extract_suggested_breakdown_items(description: str) -> list[str]:
    """Extract numbered items from an explicit suggested breakdown section."""
    lines = (description or "").splitlines()
    start = next(
        (index + 1 for index, line in enumerate(lines)
         if "suggested task breakdown" in line.strip().lower()),
        None,
    )
    if start is None:
        return []
    items: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if items and re.match(r"^[A-Z][A-Za-z /-]{2,}:\s*$", stripped):
            break
        if match := re.match(r"^\d+[.)]\s+(.+)$", stripped):
            items.append(match.group(1).strip())
    return items


def strip_suggested_breakdown_section(description: str) -> str:
    """Remove a suggested full-goal breakdown from request details."""
    output: list[str] = []
    skipping = False
    for line in (description or "").splitlines():
        stripped = line.strip()
        if "suggested task breakdown" in stripped.lower():
            skipping = True
            continue
        if skipping:
            if not stripped or re.match(r"^\d+[.)]\s+", stripped):
                continue
            skipping = False
        output.append(line)
    return "\n".join(output).strip()


def fallback_agent_type(item: str) -> str:
    """Choose a conservative built-in agent type for one fallback item."""
    text = item.lower()
    if any(token in text for token in ("repo", "repository", "readme", "docs", "file")):
        return "code"
    if any(token in text for token in (
        "market", "research", "customer", "competitor", "pricing", "formation",
        "compliance", "zoning", "licensing",
    )):
        return "research"
    return "planning"


def fallback_task_title(item: str) -> str:
    """Turn a breakdown item into a concise task title."""
    title = re.split(r"\s+[—-]\s+|\.\s+", item.strip(), maxsplit=1)[0]
    return (title.strip(" .:-") or item.strip())[:120]


def build_fallback_tasks(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Build deterministic task specs when a planner returns narrative only."""
    description = request.get("description") or ""
    milestone_index = request.get("goal_milestone_index")
    milestone_description = (request.get("goal_milestone_description") or "").strip()
    items = (
        [milestone_description]
        if milestone_index and milestone_description
        else extract_suggested_breakdown_items(description)
    )
    if not items:
        return [{
            "title": f"Plan and decompose: {request.get('title', 'Request')[:80]}",
            "description": (
                "The automated decomposition session returned a narrative response without "
                "creating tasks. Review the parent request, produce a visible task breakdown, "
                "and execute only planning/documentation-safe next steps."
            ),
            "agent_type": "planning",
            "sequence_order": 0,
        }]
    target_repo = request.get("target_repo") or ""
    target_paths = request.get("target_paths") or []
    raw_details = description.strip()
    details = strip_suggested_breakdown_section(raw_details) if milestone_index else raw_details
    target_context = (
        f"\n\nTarget repo: {target_repo or 'unspecified'}\nTarget paths: {target_paths or []}"
        if target_repo or target_paths else ""
    )
    request_context = (
        f"\n\nMilestone-scoped request details:\n{details}"
        if milestone_index and details else ""
    )
    boundary = (
        "\n\nThis task was created by deterministic daemon fallback because "
        "the planner session returned text instead of creating tasks. "
        "Respect the parent request boundaries and avoid real-world business "
        "filings, purchases, vendor/customer contact, or binding commitments."
    )
    persistence = (
        "\n\nDurable output requirement: persist the milestone deliverable "
        f"as concrete file changes in target repo {target_repo}; report "
        "the changed paths and commit/URL. Memory-only or chat-only output "
        "does not complete this fallback task."
        if target_repo else ""
    )
    return [{
        "title": fallback_task_title(item),
        "description": f"Fallback decomposition item: {item}{target_context}{request_context}{boundary}{persistence}",
        "agent_type": fallback_agent_type(item),
        "sequence_order": index,
    } for index, item in enumerate(items)]
