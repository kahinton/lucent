# Contributing to Lucent

Thank you for your interest in contributing to Lucent! This guide will help you get started.

## Prerequisites

- Python 3.12+
- Docker and Docker Compose
- Git

## Local Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/kahinton/lucent.git
   cd lucent
   ```

2. **Start the database:**
   ```bash
   docker compose up -d postgres
   ```

3. **Install dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Run the server:**
   ```bash
   lucent
   ```

   Or use Docker Compose for the full stack:
   ```bash
   docker compose up
   ```

## Running Tests

```bash
pytest
```

Tests require a running PostgreSQL instance. The default test configuration connects to `localhost:5433` (the Docker Compose port). Set `TEST_DATABASE_URL` to override.

## Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
ruff check src/ tests/
ruff format src/ tests/
```

Configuration is in `pyproject.toml`. Target Python version is 3.12, line length is 100.

## Pull Request Process

1. Fork the repository and create a feature branch from `main`.
2. Make your changes with clear, focused commits.
3. Ensure all tests pass and linting is clean.
4. Open a pull request with a description of what you changed and why.

## Reporting Issues

Use [GitHub Issues](https://github.com/kahinton/lucent/issues) to report bugs or request features. Include:
- Steps to reproduce (for bugs)
- Expected vs actual behavior
- Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the [Lucent Source Available License 1.0](LICENSE). Note that this is a source-available license, not an OSI-approved open source license.
