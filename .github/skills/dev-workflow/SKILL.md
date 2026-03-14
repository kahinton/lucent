---
name: dev-workflow
description: 'Standard development workflow for the Lucent project — code, test, review cycle with project-specific conventions'
---

# Dev Workflow — Lucent Project

## Before Starting Any Task

1. **Search memories** for related past work: `search_memories(query="feature/module name")`
2. Read existing code in the area you're changing — understand patterns before modifying
3. Check for existing tests that cover the area

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
ruff check src/lucent/
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

If you fixed a tricky bug, made a design decision, or learned something non-obvious — save it as a memory immediately. Don't wait until end of conversation.

## Project Conventions

| Convention | Details |
|-----------|---------|
| Python version | 3.12+ |
| Linter | ruff (config in pyproject.toml) |
| Test runner | pytest + pytest-asyncio |
| Line length | 100 characters |
| SQL | Raw SQL with asyncpg parameterized queries (`$1`, `$2`) — no ORM |
| Migrations | Numbered raw SQL files in `src/lucent/db/migrations/` |
| API framework | FastAPI with Pydantic v2 models |
| Auth | Session cookies + API keys, RBAC via deps |

## Common Gotchas

- **asyncpg pool**: Don't hold connections across await boundaries — use `async with pool.acquire()` blocks
- **Test isolation**: Tests use a shared `db_pool` fixture but clean up after themselves
- **Docker volumes**: If code changes aren't reflected, the volume cache may be stale — restart the container
- **MCP tools field**: When configuring MCP servers for Copilot SDK, `"tools": ["*"]` is REQUIRED
- **Daemon imports**: The daemon runs as `python -m daemon.daemon` — imports need to work from that context
