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

## Pipeline Overview

```
Assessment Output → Signal Parsing → Domain Classification → Archetype Mapping → Generation → Validation
```

## Phase 1: Parse Domain Signals

Extract structured signals from the assessment result:

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

Each domain has a set of **archetypes** — proven agent/skill combinations that work well:

### Software Domain
| Agent | Purpose | Template |
|-------|---------|----------|
| `code-review` | Review code changes for correctness, security, performance | `software` |
| `testing` | Write and maintain tests, ensure coverage | `software` |
| `security` | Identify vulnerabilities and recommend fixes | `software` |
| `documentation` | Create and maintain docs for code, APIs, processes | `software` |
| `deployment` | Manage CI/CD pipelines and infrastructure | `software` |

| Skill | Purpose | Template |
|-------|---------|----------|
| `code-review` | Structured code review process | `software-code-review` |
| `dev-workflow` | Standard development workflow | `software-dev-workflow` |

### Legal Domain
| Agent | Purpose | Template |
|-------|---------|----------|
| `legal-research` | Research precedents, regulations, compliance requirements | `legal` |
| `contract-review` | Analyze contracts for risks, obligations, terms | `legal` |
| `compliance` | Monitor and ensure regulatory compliance | `legal` |

| Skill | Purpose | Template |
|-------|---------|----------|
| `case-analysis` | Structured legal case analysis | `legal-case-analysis` |
| `compliance-review` | Regulatory compliance review process | `legal-compliance` |

### Ops/Support Domain
| Agent | Purpose | Template |
|-------|---------|----------|
| `triage` | Classify and route incoming issues | `support` |
| `incident-response` | Handle and resolve production incidents | `support` |
| `knowledge-base` | Maintain and improve knowledge base | `support` |

| Skill | Purpose | Template |
|-------|---------|----------|
| `triage` | Issue triage and classification | `support-triage` |
| `incident-response` | Incident response procedures | `support-triage` |

### Research Domain
| Agent | Purpose | Template |
|-------|---------|----------|
| `literature-review` | Survey existing research and synthesize findings | `research` |
| `data-analysis` | Analyze datasets and produce structured insights | `research` |

| Skill | Purpose | Template |
|-------|---------|----------|
| `methodology` | Research methodology and rigor | `research-methodology` |

## Phase 3: Generate Capabilities

For each recommended agent/skill:

1. **Select template** based on `domain_template` field
2. **Build context** with domain-specific parameters (language, framework, tools, guardrails)
3. **Render template** via Jinja2
4. **Write file** to `daemon/agents/` (agents) or `.github/skills/` (skills)
5. **Skip existing** — never overwrite agents or skills that already exist

The generation uses the `AdaptationPipeline` class in `daemon/adaptation.py`.

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

After generation and validation:

1. Create an adaptation summary memory (type: `technical`, tags: `[daemon, environment, adaptation, agent-registry]`)
2. Include the agent registry — all available agents (existing + generated)
3. Record what was generated, skipped, and any validation warnings

## Tips

- Start with the domain's core archetypes, then add specialized agents as needed
- When the assessment output includes explicit `recommended_agents`, use those — they reflect what the assessment agent actually observed
- When it doesn't, fall back to archetype recommendations based on domain classification
- Cross-domain environments (e.g., software + legal) should get agents from BOTH domains
- Always check what already exists before generating — the `existing_agents` and `existing_skills` fields prevent duplication
- Validation warnings don't block generation — they're logged for quality improvement
