# Development Guide

How to set up a local development environment, run tests, and contribute to Lucent.

## Prerequisites

- Python 3.12+
- Docker and Docker Compose
- Git

## Local Development Setup

### Option 1: Full Docker Stack

Run everything in containers — no local Python needed:

```bash
git clone https://github.com/kahinton/lucent.git
cd lucent
docker compose up -d
```

The dev Dockerfile (`Dockerfile.dev`) mounts source directories so changes are reflected without rebuilding.

### Option 2: Local Server + Docker Database

Run the database in Docker, the server locally for faster iteration:

```bash
# Start just the database
docker compose up -d postgres

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the package with dev dependencies
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env

# Run the server
export DATABASE_URL="postgresql://lucent:lucent_dev_password@localhost:5433/lucent"
lucent
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

## Project Structure

See [Architecture](architecture.md) for the full source layout and component descriptions.

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

## CI/CD

GitHub Actions runs on every pull request:

- **Linting**: Ruff check on `src/` and `tests/`
- **Tests**: Full pytest suite against PostgreSQL

## License

By contributing, you agree that your contributions will be licensed under the [Lucent Source Available License 1.0](../LICENSE). Note that this is a source-available license, not an OSI-approved open source license.

## Related Documentation

- [Architecture](architecture.md) — system design and source layout
- [Configuration](configuration.md) — environment variables and settings
- [API Reference](api-reference.md) — REST API documentation
- [Troubleshooting](troubleshooting.md) — common issues and fixes
