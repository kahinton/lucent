---
name: onboarding
description: 'Guide new contributors through project setup, architecture overview, coding conventions, and first-contribution workflow'
---

# Onboarding

Guide new contributors through project setup, architecture overview, coding conventions, and first-contribution workflow.

## When to Use

- A new contributor is setting up the project for the first time
- Someone asks how the project is structured or how to get started
- A contributor needs help understanding coding conventions or the development workflow
- Onboarding documentation needs to be reviewed or updated

## Prerequisites

- **Python 3.12+** installed
- **Docker** and **Docker Compose** installed
- **Git** configured with access to the repository

## Project Setup

### Step 1: Clone and Install

```bash
git clone <repository-url>
cd hindsight

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the project in development mode
pip install -e ".[dev]"
```

### Step 2: Start Local Services

The project uses Docker Compose to run PostgreSQL and the Lucent server locally:

```bash
docker compose up -d
```

This starts:
- **PostgreSQL** — data store for memories
- **Lucent server** — the MCP-compatible memory API

Verify services are running: `docker compose ps`

### Step 3: Run Tests

```bash
pytest tests/
```

All tests should pass on a clean checkout. If tests fail, check that Docker services are running and healthy.

### Step 4: Verify Code Formatting

The project uses **ruff** for linting and formatting:

```bash
ruff check .        # Lint
ruff format --check .  # Check formatting
ruff format .       # Auto-format
```

## Project Structure

| Directory | Purpose |
|---|---|
| `src/lucent/` | Core application — MCP server, tools, memory operations, API |
| `daemon/` | Autonomous daemon loop — perceive→reason→decide→act cognitive cycle |
| `tests/` | Test suite (pytest) |
| `.github/skills/` | Skill definitions for the AI agent |
| `.github/agents/` | Agent definitions |
| `docker/` | Docker configuration files |
| `docs/` | Project documentation |
| `examples/` | Usage examples |

## Coding Conventions

- **Type hints** are required on all function signatures
- **Docstrings** are required on public APIs
- **Import order**: stdlib → third-party → local (enforced by ruff)
- **Formatting**: enforced by `ruff format` — do not manually override
- **Tests**: every new feature or bug fix should include tests in `tests/`
- Configuration lives in `pyproject.toml`

## First Contribution Workflow

1. Create a feature branch from `main`
2. Make your changes with tests
3. Run `ruff check . && ruff format --check . && pytest tests/` to validate
4. Commit with a clear, descriptive message
5. Open a pull request against `main`

## Getting Help

- Read `README.md` for a project overview
- Read `CONTRIBUTING.md` for detailed contribution guidelines
- Check `docs/` for architecture and design documentation
- Review existing tests in `tests/` for examples of how components are used
