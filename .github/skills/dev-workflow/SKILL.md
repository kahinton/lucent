---
name: dev-workflow
description: 'Standard development workflow for the Lucent project — code, test, review cycle with project-specific conventions'
---

# Dev Workflow — Lucent Project

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Load task context before starting | `query="module or feature name"`, `limit=5` |
| `memory-server-create_memory` | Save findings, bugs fixed, design decisions | `type`, `content`, `tags`, `importance`, `shared` |
| `memory-server-update_memory` | Update existing memories with new info | `memory_id`, `content` |
| `memory-server-get_existing_tags` | Find consistent tags before creating | `limit=50` |

## Before Starting Any Task

1. **Search memories** for related past work:
   ```
   memory-server-search_memories(query="feature or module name", limit=5)
   ```
2. Read existing code in the area you're changing — understand patterns before modifying
3. Check for existing tests that cover the area

## Decision: When to Save a Memory

- IF you fixed a tricky bug → `create_memory(type="experience", content="cause + fix + lesson", tags=["bugs"], importance=7)`
- ELIF you made an architectural decision → `create_memory(type="technical", content="decision + reasoning + alternatives", tags=["architecture"], importance=8)`
- ELIF you discovered a non-obvious pattern in the codebase → `create_memory(type="technical", ...)`, importance=6
- ELIF task is routine/obvious → skip (don't pollute with noise)

## Development Cycle

### 1. Implement

- Make minimal, focused changes — one concern per commit
- Follow existing code patterns in the file you're editing
- Use type hints (Python 3.12+ style — `str | None` not `Optional[str]`)
- Keep imports organized: stdlib → third-party → local
- Don't add docstrings/comments to code you didn't change

### 2. Test

Run tests frequently — catch issues early:
```bash
# Run specific test file
pytest tests/test_<module>.py -v --tb=short

# Run full suite
pytest tests/ -q --tb=short

# Run with ruff linting
ruff check src/
```

Test configuration: `pyproject.toml` `[tool.pytest.ini_options]`, asyncio_mode = "auto"

### 3. Verify in Docker

If the change affects runtime behavior:
```bash
docker restart lucent-server
# Wait for health check, then verify
docker logs lucent-server --since 30s 2>&1 | head -10
```

Source files are volume-mounted so most changes hot-reload. Dependency changes or new files need a container rebuild: `docker compose build lucent`.

### 4. Capture What You Learned

If you fixed a tricky bug, made a design decision, or learned something non-obvious — save it:
```
memory-server-create_memory(
  type="experience",
  content="## [What happened]\n\n**Root cause**: ...\n**Fix**: ...\n**Lesson**: ...",
  tags=["bugs"],  # use get_existing_tags() first
  importance=7,
  shared=true
)
```

## Project Conventions

| Convention | Details |
|-----------|---------|
| Python version | 3.12+ |
| Linter | ruff (config in pyproject.toml) |
| Test runner | pytest + pytest-asyncio |
| Line length | 100 characters |
| SQL | Raw SQL with asyncpg parameterized queries (`$1`, `$2`) — no ORM |
| Migrations | Numbered raw SQL files in the project's `db/migrations/` directory |
| API framework | FastAPI with Pydantic v2 models |
| Auth | Session cookies + API keys, RBAC via deps |

## Common Gotchas

- **asyncpg pool**: Don't hold connections across await boundaries — use `async with pool.acquire()` blocks
- **Test isolation**: Tests use a shared `db_pool` fixture but clean up after themselves
- **Docker volumes**: If code changes aren't reflected, the volume cache may be stale — restart the container
- **MCP tools field**: When configuring MCP servers for Copilot SDK, `"tools": ["*"]` is REQUIRED
- **Daemon imports**: The daemon runs as `python -m daemon.daemon` — imports need to work from that context

## Example: Good Workflow

```
1. memory-server-search_memories(query="auth middleware", limit=5)
   → Found memory: "Session cookie stripping issue in auth.py — fixed 2026-02-10"
   → Apply lesson: check auth middleware before touching session handling

2. Read the relevant source file, understand the current pattern

3. Make targeted change to one function, not wholesale refactor

4. pytest tests/test_auth.py -v --tb=short → all pass

5. memory-server-create_memory(
     type="experience",
     content="## Auth middleware: session cookie behavior\n\nRoot cause: X\nFix: Y\nLesson: Z",
     tags=["auth", "bugs"],
     importance=7,
     shared=true
   )
```

## Example: Bad Workflow (Anti-Pattern)

```
❌ Skip memory search → repeat a known mistake
❌ Make changes across 5 unrelated modules in one PR → hard to review
❌ "I'll save the memory later" → context is lost, never saved
❌ Not running tests before declaring done → regressions ship
```
