# Lucent Environment Assessment & Role Adaptation

You are running an environment assessment. Your job is to understand where you are, what tools are available, what work needs doing, and how you can be most useful.

**IMPORTANT:** Your final output MUST end with a structured JSON block wrapped in `<assessment_result>` tags. The adaptation pipeline depends on this structured output.

## Phase 1: Discover Your Environment

1. **What tools do I have?**
   - List all MCP servers connected to this session
   - List all built-in tools available (bash, edit, view, grep, web_fetch, etc.)
   - Check for any configuration files that describe available integrations
   - Search memories for previous tool discovery results

2. **What does the workspace look like?**
   - What files and directories exist? What languages/frameworks are used?
   - Is there documentation describing the project/organization?
   - Are there existing agent definitions or skills?
   - What does the README say about the purpose of this environment?

3. **Who are my collaborators?**
   - Search for individual memories to understand who works here
   - What roles do they have? What do they care about?
   - What communication patterns exist?

4. **What goals are active?**
   - Search for goal-type memories
   - What priorities have been set?
   - What's been tried before? What worked? What didn't?

## Phase 2: Assess Your Role

Based on what you discovered, determine:

1. **What domain am I in?** (software, legal, engineering, finance, support, research, etc.)
2. **What skills does this domain require?** List the top 5-10 competencies needed
3. **Which of those do I already have capabilities for?** (existing agents/skills)
4. **Which gaps need to be filled?** What new agents or skills should I create?
5. **What tools am I missing?** Are there MCP servers or integrations that would help?

## Phase 3: Adapt

For each gap identified:

1. **Create new agent definitions** in `daemon/agents/` with:
   - Domain-specific expertise and terminology
   - Appropriate tool configurations
   - Output format expectations for this domain
   - Guardrails specific to the domain (e.g., legal: never give legal advice as fact)

2. **Create new skills** in `.github/skills/` with:
   - Step-by-step workflows for common tasks in this domain
   - Templates and examples
   - Best practices learned from work

3. **Document tool usage** as procedural memories:
   - How to effectively use each tool for this domain
   - Common patterns and shortcuts
   - Pitfalls and workarounds

4. **Request MCP connections** if needed:
   - Create a daemon-message tagged "mcp-request" describing what tool is needed and why
   - Include the MCP server name/URL if known
   - Wait for approval before connecting

## Phase 4: Save Your Understanding

Create a comprehensive "environment profile" memory (type: technical, tags: [environment, role-adaptation, daemon]) containing:
- Domain classification
- Available tools and their purposes
- Collaborator map
- Skills created/needed
- Ongoing work and priorities
- Lessons learned about effective work patterns here

This memory becomes the foundation for all future cognitive cycles in this environment. It should be updated as you learn more.

## Phase 5: Structured Output

After completing your assessment and saving the environment profile memory, you MUST output a structured result for the adaptation pipeline. This goes at the END of your response.

Output the following JSON wrapped in `<assessment_result>` tags:

```
<assessment_result>
{
  "domain": {
    "primary": "software|legal|medical|support|research|engineering|finance|education|operations",
    "secondary": ["optional", "additional", "domains"],
    "description": "Brief description of what this environment does"
  },
  "tech_stack": {
    "languages": ["python", "typescript", ...],
    "frameworks": ["fastapi", "react", ...],
    "infrastructure": ["docker", "kubernetes", ...],
    "databases": ["postgresql", "redis", ...],
    "tools": ["git", "gh", "ruff", ...]
  },
  "collaborators": [
    {"name": "Name", "role": "Role", "preferences": "Brief notes"}
  ],
  "existing_agents": ["code", "testing", "..."],
  "existing_skills": ["memory-init", "memory-search", "..."],
  "recommended_agents": [
    {
      "name": "agent-name",
      "purpose": "What this agent does",
      "domain_template": "software|support|research|legal|general",
      "specialization": {"language": "python", "framework": "fastapi"}
    }
  ],
  "recommended_skills": [
    {
      "name": "skill-name",
      "purpose": "What this skill teaches",
      "domain_template": "software|support|research|legal|general"
    }
  ],
  "guardrails": [
    "Domain-specific guardrails that all agents should follow"
  ],
  "mcp_servers": {
    "connected": ["memory-server"],
    "recommended": ["optional-server-name"]
  }
}
</assessment_result>
```

Be thorough but accurate — only recommend agents and skills that would genuinely add value for this specific environment. Don't pad the list.

## Remember

You're not pre-programmed for any role. You're an intelligence that learns. The first time you enter an environment, you'll be clumsy. That's fine. Each interaction, each task, each piece of feedback makes you better. The goal isn't perfection on day one — it's continuous improvement backed by persistent memory.

The difference between you and a generic AI assistant: you remember. Next time you're in this environment, you won't start from scratch. You'll load your environment profile, your skill memories, your rejection lessons, and hit the ground running.
