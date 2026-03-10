---
name: environment-assessment
description: 'Assess a new environment to understand tools, domain, collaborators, and capabilities. Use when entering a new workspace, when the environment changes significantly, or when context tagged "environment" is missing.'
---

# Environment Assessment

This skill enables Lucent to understand any environment it's deployed into — software, legal, engineering, support, research, or anything else.

## When to Use

- First time in a new workspace or repository
- No memory tagged `environment` exists
- Environment has changed significantly (new tools, new domain, new team)
- Daemon task `assessment` is dispatched

## Phase 1: Discover Tools

1. **File system**: List the workspace root. What's here?
2. **CLI tools**: Check for common tools — `git`, `docker`, `node`, `python`, `go`, `cargo`, `dotnet`, `gh`, language-specific CLIs
3. **MCP servers**: Check `.vscode/mcp.json`, `.mcp.json`, and `.github/plugin/.mcp.json` for connected MCP servers
4. **Configuration**: Look for `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Makefile`, `docker-compose.yml` — these reveal the tech stack
5. **CI/CD**: Check `.github/workflows/`, `Jenkinsfile`, `.gitlab-ci.yml` for automation
6. **Linters/formatters**: Check for `ruff`, `eslint`, `prettier`, `rustfmt`, `golint` configs

## Phase 2: Understand the Domain

1. **Read the README** — this is the fastest way to understand purpose
2. **Check documentation** — `docs/`, `wiki/`, inline docs
3. **Examine the source structure** — what patterns are used? What architecture?
4. **Look at recent git history** — what kind of work is happening?
5. **Classify the domain**: Software? Legal? Engineering? Research? Operations? Mixed?

## Phase 3: Map Collaborators

1. **Search for `individual` type memories** — who do you know?
2. **Check git log for authors** — who contributes?
3. **Look for team documentation** — org charts, role descriptions

## Phase 4: Inventory Existing Capabilities

1. **Agents**: List files in `daemon/agents/` — what sub-agents exist?
2. **Skills**: List directories in `.github/skills/` — what skills are defined?
3. **For each agent/skill**: Read it and summarize its purpose

## Phase 5: Gap Analysis

Compare what exists against what the domain needs:

| Domain | Likely Needed Agents | Likely Needed Skills |
|--------|---------------------|---------------------|
| Software | code, testing, security, docs, deployment | code-review, release, debugging |
| Legal | research, drafting, compliance, review | case-analysis, document-review |
| Engineering | design, simulation, review, safety | design-review, standards-check |
| Support | triage, response, escalation, knowledge | ticket-handling, customer-comms |
| Research | literature, analysis, writing, data | paper-review, methodology |

## Phase 6: Create Environment Profile

Save a memory (type: `technical`, tags: `[environment, role-adaptation, daemon]`) containing:

```
## Domain
[Classification and description]

## Tools Available
[Categorized list of all discovered tools]

## Tech Stack
[Languages, frameworks, databases, infrastructure]

## Collaborators
[Who works here, their roles, their preferences]

## Agents & Skills
[What exists, what's missing, what was created]

## Active Goals
[Current priorities and ongoing work]

## Patterns & Conventions
[Code style, commit conventions, review process, deployment process]
```

## Tips

- Start broad, narrow down — it's better to discover too much than too little
- The assessment should take 2-5 minutes, not 20
- Update the environment profile memory when things change — don't recreate from scratch
- If you can't determine the domain, ask the user
