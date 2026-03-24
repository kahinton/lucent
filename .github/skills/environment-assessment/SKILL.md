---
name: environment-assessment
description: 'Assess a new environment to understand tools, domain, collaborators, and capabilities. Use when entering a new workspace, when the environment changes significantly, or when no memory tagged "environment" exists.'
---

# Environment Assessment

## When to Run

- First time in a new workspace
- No memory tagged `environment` exists
- Environment has changed significantly (new tools, new domain, new team)
- Dispatched as a daemon `assessment` task

## Phase 1: Discover Tools

```bash
# File system layout
ls -la
find . -maxdepth 2 -type d | head -30

# Language runtimes
which python node go rustc cargo dotnet java 2>/dev/null

# Build & package management
ls pyproject.toml package.json Cargo.toml go.mod Makefile justfile docker-compose.yml Dockerfile 2>/dev/null

# CI/CD
ls .github/workflows/ Jenkinsfile .gitlab-ci.yml 2>/dev/null

# MCP servers
cat .vscode/mcp.json .mcp.json 2>/dev/null

# Linters & formatters
ls .eslintrc* .prettierrc* ruff.toml rustfmt.toml .golangci.yml 2>/dev/null
```

## Phase 2: Understand the Domain

1. **Read the README** — fastest way to understand purpose
2. **Check documentation** — `docs/`, inline docs, architecture notes
3. **Examine source structure** — patterns, architecture, organization
4. **Read recent git history** — what kind of work is happening?
5. **Classify**: Software? Legal? Research? Operations? Mixed?

## Phase 3: Map Collaborators

```
search_memories(type="individual", limit=10)
```

```bash
git log --format='%aN' | sort -u    # Who contributes?
```

## Phase 4: Inventory Capabilities

Check what agents and skills are already configured:
```bash
curl -s http://localhost:<port>/api/definitions/agents?status=active
curl -s http://localhost:<port>/api/definitions/skills?status=active
```

For each, note its purpose and quality.

## Phase 5: Gap Analysis

Compare what exists against what the domain needs:

| Domain | Typical needs |
|--------|--------------|
| Software | code, testing, security, docs, deployment, debugging |
| Legal | research, drafting, compliance, case analysis |
| Research | literature review, methodology, data analysis, writing |
| Operations | monitoring, incident response, runbooks, deployment |

Prioritize gaps: Critical (blocks autonomous work) > High (reduces quality) > Medium (improves efficiency) > Low (nice-to-have)

## Phase 6: Save the Profile

```
create_memory(
  type="technical",
  content="## Environment Profile: <project>\n\n**Domain**: <classification>\n**Language**: <primary>\n**Framework**: <core framework>\n**Infrastructure**: <how it runs>\n**Tools**: <categorized list>\n**Collaborators**: <who works here>\n**Capabilities**: <what agents/skills exist>\n**Gaps**: <prioritized list of missing capabilities>",
  tags=["environment", "daemon"],
  importance=8,
  shared=true
)
```

If a previous profile exists, `update_memory` instead of creating a duplicate.

## Rules

- The assessment should take 2-5 minutes, not 20 — be efficient
- Start broad, narrow down — better to discover too much than too little
- Update the profile when things change — don't recreate from scratch
- If you can't determine the domain from the environment, say so

## Anti-Patterns

- Don't spend more than 5 minutes on assessment because deep investigation belongs in later tasks — the goal here is a useful profile, not a complete audit.
- Don't recreate the environment profile from scratch when it already exists because you'll overwrite accumulated knowledge — use `update_memory` instead.
- Don't skip the gap analysis phase because generating capabilities without knowing what's missing leads to duplication and misaligned tooling.
- Don't assume the domain from the file structure alone because surface-level signals mislead — read the README and recent git history to confirm.