---
name: dev-workflow
description: 'Standard development workflow'
---

# Dev Workflow

Workflow skill for python/fastapi development.

## When to Use

- Starting a new feature or bug fix
- Running the development cycle (code → test → review)
- Setting up the development environment
- Debugging test failures

## Development Workflow

### Step 1: Understand the Task

1. Read the task description or issue
2. Search memories for related past work
3. Identify affected files and modules
4. Check for existing tests that cover this area

### Step 2: Set Up

1. Ensure virtual environment is active
2. Run `pip install -e ".[dev]"` if dependencies changed
3. Verify `ruff` and `pytest` are available

### Step 3: Implement

1. Make minimal, focused changes
2. Follow existing code patterns and conventions
3. Add type annotations/hints where expected
4. Write clear commit-worthy code (no TODOs left behind)

### Step 4: Test

1. Run `pytest` for the affected test files
2. Run `pytest --tb=short` for a quick overview
3. Run `ruff check .` for linting

### Step 5: Document

1. Update relevant documentation if behavior changed
2. Add inline comments only where logic is non-obvious
3. Save a memory of what you learned

## Environment Maintenance

- Keep dependencies up to date
- Watch for deprecation warnings
- Monitor test execution time
- Clean up temporary files

## Tips

- Run tests frequently — catch issues early
- Read test failures carefully before changing code
- Check git diff before finishing — review your own changes
- Search for similar patterns in the codebase before inventing new ones
