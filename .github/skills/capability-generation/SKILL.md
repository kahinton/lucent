---
name: capability-generation
description: 'Generate domain-appropriate agents and skills from environment assessment results. Use after environment assessment completes, when entering a new domain, or when gap analysis reveals missing capabilities.'
---

# Dynamic Capability Generation

Takes structured assessment output and automatically generates domain-appropriate agent definitions and skills. This is the core mechanism that makes Lucent deployable into ANY environment.

## When to Use

- After an environment assessment completes
- When entering a new domain (legal, ops, research, etc.)
- When gap analysis reveals missing agents or skills
- When the daemon dispatches a `capability-generation` task

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Check existing capabilities | `tags=["environment", "agent-registry"]`, `limit=10` |
| `memory-server-search_memories` | Load assessment results | `tags=["environment", "assessment"]`, `limit=5` |
| `memory-server-create_memory` | Save adaptation summary | `type="technical"`, `tags=["daemon","environment","adaptation","agent-registry"]`, `shared=true` |

### REST API Calls (via `curl` or `httpx`)

| Endpoint | Method | Purpose | Body |
|----------|--------|---------|------|
| `GET /api/definitions/agents?status=active` | GET | List existing agents | — |
| `GET /api/definitions/skills?status=active` | GET | List existing skills | — |
| `POST /api/definitions/agents` | POST | Create agent definition | `{name, description, system_prompt, scope, status}` |
| `PATCH /api/definitions/agents/{id}` | PATCH | Update agent definition | `{system_prompt}` |

Example create agent call:
```python
import httpx
response = httpx.post(
    "http://localhost:8766/api/definitions/agents",
    headers={"Authorization": f"Bearer {api_key}"},
    json={
        "name": "legal-research",
        "description": "Research legal precedents and compliance requirements",
        "system_prompt": "You are a legal research specialist...",
        "scope": "built-in",
        "status": "active"
    }
)
```

## Pipeline Overview

```
Assessment Output → Signal Parsing → Domain Classification → Archetype Mapping → Generation → Validation
```

## Phase 1: Parse Domain Signals

### Step 1: Load Assessment and Existing Capabilities

```
memory-server-search_memories(tags=["environment", "assessment"], limit=5)
```

Then check what already exists:
```bash
curl -s "http://localhost:8766/api/definitions/agents?status=active" \
  -H "Authorization: Bearer $API_KEY"
```

Extract structured signals:

| Signal Category | Examples | Source |
|----------------|----------|--------|
| **Tech Stack** | Languages, frameworks, databases, infrastructure | `tech_stack` field |
| **Team Roles** | Developer, support engineer, researcher, legal analyst | `collaborators` field |
| **Workflows** | CI/CD, ticket triage, case management, peer review | Inferred from tools + domain |
| **Tools** | Git, Docker, Jira, LexisNexis, Slack | `tech_stack.tools` + `mcp_servers` |
| **Domain Indicators** | File types, directory structures, terminology | Assessment description |

Key indicators by domain:
- **Software**: `src/`, `tests/`, `package.json`, `pyproject.toml`, CI configs, linters
- **Legal**: `contracts/`, `cases/`, `briefs/`, legal databases, compliance tools, regulatory references
- **Ops/Support**: `runbooks/`, `playbooks/`, ticketing systems, monitoring tools, SLA references
- **Research**: `papers/`, `data/`, `notebooks/`, citation managers, statistical tools

## Phase 2: Map to Domain Archetypes

### Software Domain
| Agent | Purpose |
|-------|---------|
| `code-review` | Review code changes for correctness, security, performance |
| `testing` | Write and maintain tests, ensure coverage |
| `security` | Identify vulnerabilities and recommend fixes |
| `documentation` | Create and maintain docs for code, APIs, processes |
| `deployment` | Manage CI/CD pipelines and infrastructure |

### Legal Domain
| Agent | Purpose |
|-------|---------|
| `legal-research` | Research precedents, regulations, compliance requirements |
| `contract-review` | Analyze contracts for risks, obligations, terms |
| `compliance` | Monitor and ensure regulatory compliance |

### Ops/Support Domain
| Agent | Purpose |
|-------|---------|
| `triage` | Classify and route incoming issues |
| `incident-response` | Handle and resolve production incidents |
| `knowledge-base` | Maintain and improve knowledge base |

### Research Domain
| Agent | Purpose |
|-------|---------|
| `literature-review` | Survey existing research and synthesize findings |
| `data-analysis` | Analyze datasets and produce structured insights |

## Phase 3: Generate Capabilities

### Decision: Create vs Skip

- IF agent with same name exists (from existing agents check) → **SKIP**, never overwrite
- ELIF assessment includes explicit `recommended_agents` → use those
- ELSE → use archetype recommendations for the detected domain

### Generate Agent Definition

The generation uses the `AdaptationPipeline` class in `daemon/adaptation.py`. For manual generation:

1. Select template based on domain: `daemon/templates/agents/`
2. Build context with domain-specific parameters (language, framework, tools, guardrails)
3. Render template via Jinja2
4. Create via API:
   ```python
   httpx.post(
       "http://localhost:8766/api/definitions/agents",
       headers={"Authorization": f"Bearer {api_key}"},
       json={"name": name, "description": desc, "system_prompt": prompt, "scope": "built-in", "status": "active"}
   )
   ```

## Phase 4: Validate Generated Capabilities

Every generated agent must pass this checklist:

- [ ] **Has clear purpose**: The agent's role is specific, not vague
- [ ] **Has domain context**: Includes relevant domain description
- [ ] **Has appropriate tools**: Lists tools relevant to its function
- [ ] **Has guardrails**: Includes safety constraints appropriate to the domain
- [ ] **Has memory tags**: Specifies tags for organizing its output
- [ ] **Has feedback protocol**: Includes review/approval workflow
- [ ] **No conflicting agents**: Doesn't duplicate an existing agent's role

Every generated skill must pass this checklist:

- [ ] **Has frontmatter**: Valid `name` and `description` in YAML frontmatter
- [ ] **Has triggers**: Clearly states when to use this skill
- [ ] **Has process steps**: Step-by-step instructions, not just goals
- [ ] **Has best practices**: Domain-relevant guidance
- [ ] **Has pitfall warnings**: Common mistakes to avoid

## Phase 5: Store Results

```
memory-server-create_memory(
  type="technical",
  content="## Capability Generation: [domain]\n\n**Domain**: [detected domain]\n**Agents generated**: [list]\n**Agents skipped** (already existed): [list]\n**Skills generated**: [list]\n**Validation warnings**: [any issues]",
  tags=["daemon", "environment", "adaptation", "agent-registry"],
  importance=7,
  shared=true
)
```

## Tips

- Start with the domain's core archetypes, then add specialized agents as needed
- Cross-domain environments (e.g., software + legal) should get agents from BOTH domains
- Always check what already exists before generating — never overwrite existing definitions
- Validation warnings don't block generation — they're logged for quality improvement
