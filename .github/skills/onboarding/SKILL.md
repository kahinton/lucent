---
name: onboarding
description: 'Guide new contributors through project setup, architecture overview, and first-contribution workflow. Use when a new contributor needs setup, architecture overview, guidance on their first contribution, someone asks how to contribute, or a development environment needs to be configured from scratch.'
---

# Onboarding

## Procedure

### Step 1: Verify Environment

Confirm all prerequisites before proceeding — missing tools cause cryptic failures later.

| Prerequisite | How to verify | If missing |
|-------------|---------------|------------|
| Git + repo access | `git --version` and `git ls-remote <repo-url>` | Install git, configure SSH keys or token |
| Docker + Compose | `docker --version && docker compose version` | Install Docker Desktop or engine |
| Language runtime | Check `pyproject.toml`, `package.json`, or build config for version | Install the required version |

If any prerequisite fails, stop and resolve it before continuing.

### Step 2: Explain Architecture

Read the project structure and explain the codebase layout to the contributor:

```bash
ls -la                     # Root directory layout
find . -maxdepth 2 -type d | head -30   # Directory structure
cat README.md              # Project description and setup
```

Identify and explain these key directories:

| Directory type | What to look for | Why it matters |
|---------------|-----------------|----------------|
| **Source code** | `src/`, `lib/`, `app/` | Where the main application lives |
| **Tests** | `tests/`, `test/`, `__tests__/` | Test suite location and organization |
| **Configuration** | `docker-compose.yml`, CI configs, linter configs | How the project is built and deployed |
| **Documentation** | `docs/`, `README.md` | Existing guides and references |
| **Agent definitions** | `.github/agents/`, `.github/skills/` | AI agent and skill definitions |

Search memory for architecture context: `search_memories(query="architecture", tags=["environment"], limit=5)`

### Step 3: Set Up Dev Environment

#### 3a. Clone and Install

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
# Python
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# Node
npm install

# Or the equivalent for the project's ecosystem
```

#### 3b. Start Services

```bash
docker compose up -d
```

Wait for health checks to pass:
```bash
docker compose ps     # All services should show "healthy" or "running"
curl http://localhost:<port>/health   # Application health check
```

#### 3c. Run Tests

```bash
# Use the project's test runner — check build config for the command
# Common: pytest tests/ -v --tb=short | npm test | go test ./... | cargo test
```

**Tests passing is the only reliable signal that setup succeeded.** "Services running" is not sufficient. If tests fail, debug before proceeding.

### Step 4: Guide First Contribution

Walk the contributor through the full cycle:

1. Create a branch: `git checkout -b <feature-or-fix-description>`
2. Make a focused change — one concern per commit
3. Run tests and linting before committing
4. Commit with a clear message: `fix: description` or `feat: description`
5. Push and create a pull request

Confirm project-specific conventions — branch naming, commit message format, and PR requirements vary by project. Check `CONTRIBUTING.md` if present.

### Step 5: Establish Communication Patterns

Set the contributor up for ongoing success:

1. **Show how to search memory** — `search_memories(query="<topic>", limit=10)` for past decisions, conventions, and known gotchas
2. **Identify key contacts** — who reviews PRs, who owns which modules
3. **Point to documentation** — where to find guides, API docs, architectural decision records
4. **Explain the workflow** — how issues are triaged, how work is planned, how the daemon operates (if applicable)

## Anti-Patterns

- Don't skip prerequisite verification before setup because missing Docker, the correct language runtime, or repository access will cause cryptic failures mid-onboarding.
- Don't rush through the architecture overview because a contributor who doesn't understand the codebase structure will make changes in the wrong place and miss existing conventions.
- Don't skip running tests after setup because passing tests are the only reliable signal that the environment is actually working — "services running" is not sufficient.
- Don't assume the first contribution workflow is obvious because branch naming, commit conventions, and PR requirements vary by project and must be explicitly confirmed.