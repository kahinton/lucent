---
name: onboarding
description: 'Guide new contributors through project setup, architecture overview, and first-contribution workflow.'
---

# Onboarding

## Prerequisites

Before starting, verify:
- Git installed and configured with repository access
- Docker and Docker Compose installed
- Language runtime installed (check project's build config for required version)

## Setup

### 1. Clone and Install

```bash
git clone <repository-url>
cd <project-directory>
```

Check the project root for setup instructions:
- `README.md` — usually has setup steps
- `Makefile` / `justfile` — may have a `setup` or `install` target
- `docker-compose.yml` — may be the primary dev environment

For local development (if not purely Docker-based):
```bash
# Create a virtual environment (Python)
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# Or install dependencies (Node)
npm install

# Or the equivalent for the project's ecosystem
```

### 2. Start Services

```bash
docker compose up -d
```

Wait for health checks to pass:
```bash
docker compose ps     # All services should show "healthy" or "running"
curl http://localhost:<port>/health   # Application health check
```

### 3. Run Tests

```bash
# Use the project's test runner — check build config for the command
# Common patterns:
#   pytest tests/ -v --tb=short
#   npm test
#   go test ./...
#   cargo test
```

If tests pass, the environment is working.

## Architecture Overview

Read the project structure to understand the codebase:

```bash
ls -la                     # Root directory layout
find . -maxdepth 2 -type d | head -30   # Directory structure
cat README.md              # Project description and setup
```

Key directories to identify:
- **Source code** — where the main application lives
- **Tests** — test suite location and organization
- **Configuration** — Docker, CI/CD, linter configs
- **Documentation** — dedicated docs directory if present
- **Agent definitions and skills** — `.github/agents/` and `.github/skills/`

## First Contribution Workflow

1. Create a branch: `git checkout -b <feature-or-fix-description>`
2. Make a focused change — one concern per commit
3. Run tests and linting before committing
4. Commit with a clear message: `fix: description` or `feat: description`
5. Push and create a pull request

## Getting Help

```
search_memories(query="<topic you're confused about>", limit=10)
```

Check memory for past decisions, conventions, and known gotchas before asking — the answer may already exist.