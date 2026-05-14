---
name: capability-generation
description: 'Generate domain-appropriate agents and skills from environment assessment results. Use after environment assessment completes, when entering a new domain, or when gap analysis reveals missing capabilities.'
---

# Capability Generation

Takes assessment output and generates agent definitions and skills tailored to the detected domain. This is how the system adapts to any environment.

> **For detailed quality standards, structural templates, and evaluation checklists**, see the **definition-engineering** skill. This skill focuses on the gap-analysis-to-generation pipeline. The definition-engineering skill covers HOW to write world-class definitions.

## Prerequisites

Before running this skill:
1. An environment assessment must exist — search for `tags=["environment"]`
2. Existing capabilities must be inventoried — check `GET /api/definitions/agents?status=active` and `GET /api/definitions/skills?status=active`

## Step 1: Load Assessment and Existing Capabilities

```
search_memories(query="environment assessment", tags=["environment"], limit=5)
```

```bash
curl -s http://localhost:<port>/api/definitions/agents?status=active
curl -s http://localhost:<port>/api/definitions/skills?status=active
```

Identify:
- The domain (software, legal, research, operations, etc.)
- Available tools and infrastructure
- What agents and skills already exist
- Gaps between what exists and what the domain needs

## Step 2: Determine What's Missing

Map domain needs to capabilities:

| Domain | Typical agents needed | Typical skills needed |
|--------|----------------------|----------------------|
| Software engineering | code, research, documentation, security | code-review, testing, deployment, debugging |
| Legal | research, documentation, analysis | case-analysis, document-review, compliance |
| Research | research, documentation, planning | methodology, literature-review, data-analysis |
| Operations / SRE | code, assessment, planning | incident-response, monitoring, runbooks |
| Support | research, documentation | triage, knowledge-base, escalation |

Skip anything that already exists and is active.

## Step 3: Generate Agent Definitions

For each needed agent, create a definition following the standard structure:

```markdown
---
name: <agent-name>
description: <one-line purpose>
---

# <Agent Name> Agent

<Opening statement: what this agent is and its core function>

## Operating Principles
<2-3 sentences defining the behavioral contract>

## Execution Sequence
### 1. <First step>
<What to do, with exact tool calls>

### 2. <Second step>
...

## Decision Framework
<If/then rules for ambiguous situations>

## Boundaries
<What the agent does NOT do>
```

**Quality criteria:**
- Every step includes the specific tool calls or actions to take
- Decision framework covers the 3-5 most common ambiguities
- Boundaries prevent the most likely failure modes
- No generic platitudes — every instruction is actionable

## Step 4: Generate Skills

For each needed skill, create with this structure:

```markdown
---
name: <skill-name>
description: <when to use this skill>
---

# <Skill Title>

## Before Starting
<What context to load — memory searches>

## Procedure
### 1. <Step>
<Exact instructions with tool calls>
...

## Recording Results
<How to save findings to memory>

## Anti-Patterns
<What not to do>
```

**Quality criteria:**
- Procedures are step-by-step, not paragraph-form
- Tool calls use the exact MCP tool names and parameters
- Anti-patterns address real failure modes, not theoretical concerns

## Step 5: Submit for Review and Activation

Create concrete definition objects; do not stop at a document describing what
should exist. Use the MCP tools when available:

```
list_agent_definitions(status="active")
list_skill_definitions(status="active")
create_skill_definition(name="<skill>", description="<trigger>", content="<SKILL.md>", proposal_reason="...", proposal_evidence={...})
create_agent_definition(name="<agent>", description="<purpose>", content="<AGENT.md>", proposal_reason="...", proposal_evidence={...})
```

Definitions start as proposed and require approval before runtime use. Create a
follow-up `create_request` that names the proposed definition IDs and asks an
owner/admin to approve them and grant any active skills/hooks/servers. Do not
grant yourself access to runtime powers.

If the needed capability belongs in a built-in definition, create a follow-up
request targeted at `kahinton/lucent` and the specific `.github/agents/` or
`.github/skills/` path. Built-in source files are authoritative; a DB-only change
will not persist.

Record the result as an audit trail only after definitions/requests exist:

```
create_memory(
  type="experience",
  content="## Capability Activation: <domain>\n\n**Created/proposed agents**: <ids>\n**Created/proposed skills**: <ids>\n**Follow-up request**: <id if needed>\n**Based on assessment**: <assessment memory ID>\n**Rationale**: <why each was needed>",
  tags=["daemon", "environment", "adaptation", "capability-activation"],
  importance=7,
  shared=true
)
```

## Anti-Patterns

- Don't generate capabilities without a gap analysis because you'll create redundant or irrelevant definitions that clutter the system.
- Don't submit definitions directly as `active` because unreviewed definitions can introduce unstable or incorrect agent behavior — create proposed definitions and route them for approval.
- Don't skip checking for naming conflicts before creating because duplicate names cause ambiguity and may silently shadow existing definitions.
- Don't generate capabilities the domain doesn't need just because templates exist — template availability is not justification for creation.
- Don't create agents without defining their matching skills because a capable agent with no procedural knowledge produces inconsistent, unpredictable results.
- Don't write a capability-generation report without creating definitions or a follow-up request — documentation alone does not increase what the system can do.