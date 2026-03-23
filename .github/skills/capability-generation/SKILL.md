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

## Step 5: Submit for Review

Create new definitions via the REST API with `status="draft"`:

```bash
curl -X POST http://localhost:<port>/api/definitions/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "<name>", "description": "<desc>", "content": "<markdown>", "skill_names": ["<skills>"], "status": "draft"}'
```

Draft definitions require human approval before they become active. Tag the summary for review:

```
create_memory(
  type="technical",
  content="## Capability Generation: <domain>\n\n**Created agents**: <list>\n**Created skills**: <list>\n**Based on assessment**: <assessment memory ID>\n**Rationale**: <why each was needed>",
  tags=["daemon", "environment", "adaptation", "needs-review"],
  importance=7,
  shared=true
)
```

## Constraints

- Only generate capabilities that the assessment identified as gaps
- Definitions must be submitted as `draft` — never directly to `active`
- Check for naming conflicts with existing definitions before creating
- Don't generate capabilities the domain doesn't need just because they exist as templates