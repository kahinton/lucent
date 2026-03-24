---
name: dependency-management
description: 'Track and update project dependencies, audit for vulnerabilities, and manage version constraints. Use when auditing dependencies for vulnerabilities, updating versions, or resolving version conflicts.'
---

# Dependency Management

## Before Starting

```
search_memories(query="dependency update vulnerability", tags=["dependencies"], limit=5)
```

## Audit Procedure

### 1. Identify the Dependency Manifest

Every ecosystem has one:
- **Python**: `pyproject.toml`, `requirements.txt`, `setup.cfg`
- **Node/JS**: `package.json` + `package-lock.json` or `yarn.lock`
- **Go**: `go.mod` + `go.sum`
- **Rust**: `Cargo.toml` + `Cargo.lock`
- **Java**: `pom.xml` or `build.gradle`
- **.NET**: `*.csproj`

Read it. Understand the version constraints and what's pinned vs. flexible.

### 2. Check for Outdated Packages

```bash
# Python
pip list --outdated

# Node
npm outdated

# Go
go list -u -m all

# Rust
cargo outdated
```

### 3. Check for Vulnerabilities

```bash
# Python
pip-audit                              # or: safety check

# Node
npm audit

# Go
govulncheck ./...

# Rust
cargo audit
```

For any reported CVE:
- **Critical/High severity**: Update immediately. These are exploitable.
- **Medium**: Schedule update within the current sprint.
- **Low/Informational**: Note and address when convenient.

### 4. Update Dependencies

For each update:

1. Update the version constraint in the manifest file
2. Install the update: `pip install -e ".[dev]"` / `npm install` / equivalent
3. **Run the full test suite** — dependency updates can introduce subtle breakage
4. Check for deprecation warnings in test output
5. If the project runs in containers, rebuild: `docker compose build <service>`
6. Read the changelog for breaking changes on major version bumps

### 5. Version Constraint Guidelines

| Constraint type | When to use |
|----------------|-------------|
| Minimum version (`>=1.0.0`) | Default — allows flexibility |
| Compatible release (`~=1.4` / `^1.4`) | Want patch updates but not major changes |
| Exact pin (`==1.4.2`) | Only when a specific version is required for compatibility — document why |
| Range (`>=1.0,<2.0`) | When you know a ceiling exists |

Keep constraints as loose as possible while ensuring compatibility. Document exact pins with inline comments.

## Anti-Patterns

- Never update all dependencies at once — bulk updates make it impossible to isolate which package introduced a regression; update one package (or one related group) at a time and run tests between each.
- Don't ignore transitive vulnerability paths — a CVE in a direct dependency's dependency is still exploitable; run `pip-audit` / `npm audit` which trace the full dependency graph, not just the top-level manifest.
- Never skip running the test suite after a dependency update — even patch-version updates can change behavior silently; failing to test after updating is the most common way dependency updates cause production incidents.

### 6. Record Findings

```
create_memory(
  type="technical",
  content="## Dependency Audit: <date>\n\n**Vulnerabilities found**: <count and severity>\n**Updates applied**: <list>\n**Breaking risks**: <any compatibility concerns>\n**Test results**: <pass/fail after updates>",
  tags=["dependencies", "security"],
  importance=6,
  shared=true
)
```