---
name: dependency-management
description: 'Track and update Python dependencies, audit for vulnerabilities, manage version constraints in pyproject.toml'
---

# Dependency Management

Manage Python dependencies for the Lucent project, including version tracking, vulnerability auditing, and constraint management in pyproject.toml.

## When to Use

- Adding or updating a dependency
- Auditing for known vulnerabilities
- Reviewing dependency version constraints
- Investigating compatibility issues between packages
- Preparing for a release (dependency freeze/audit)

## Process

### Step 1: Understand the Dependency Landscape

1. Read `pyproject.toml` for current dependencies and version constraints
2. Check `[project.dependencies]` (runtime) vs `[project.optional-dependencies]` (dev/test)
3. Note any pinned versions vs minimum-version constraints
4. Run `pip list --outdated` to identify stale packages

### Step 2: Audit for Vulnerabilities

1. Run `pip-audit` (install with `pip install pip-audit` if needed)
2. Review any reported CVEs — assess severity and exploitability
3. For critical vulnerabilities, update immediately
4. For low-severity issues, note and schedule update

### Step 3: Update Dependencies

1. Update version constraint in `pyproject.toml`
2. Run `pip install -e ".[dev]"` to install updated dependency
3. Run the full test suite: `python -m pytest tests/ -x`
4. Check for deprecation warnings in test output
5. Verify the server starts cleanly: `docker compose up --build -d`

### Step 4: Version Constraint Guidelines

- Use minimum version constraints (`>=1.0.0`) for flexibility
- Pin exact versions only when a specific version is required for compatibility
- Use compatible release (`~=1.4`) when you want to allow patch updates
- Keep constraints as loose as possible while ensuring compatibility
- Document why any exact pins exist

### Step 5: Current Dependencies (Reference)

Runtime:
- `mcp[cli]>=1.0.0` — MCP protocol SDK
- `asyncpg>=0.29.0` — PostgreSQL async driver
- `pydantic>=2.0.0` — Data validation
- `python-dotenv>=1.0.0` — Environment configuration
- `fastapi>=0.115.0` — REST API framework
- `uvicorn>=0.32.0` — ASGI server
- `jinja2>=3.1.0` — Template rendering (web UI)
- `python-multipart>=0.0.9` — Form data parsing
- `bcrypt>=4.0.0` — Password hashing
- `PyNaCl>=1.5.0` — Cryptographic operations

### Step 6: Report

Output a structured summary:
- **Current state**: Which dependencies are outdated
- **Vulnerabilities**: Any known CVEs and their severity
- **Updates applied**: What was changed and why
- **Breaking risks**: Any potential compatibility concerns
- **Test results**: Confirmation tests pass after updates

## Best Practices

- Always run the full test suite after dependency changes
- Check changelogs for breaking changes before major version bumps
- Keep `pyproject.toml` as the single source of truth for dependencies
- Prefer well-maintained packages with active security response
- Document any version pins with inline comments explaining why
