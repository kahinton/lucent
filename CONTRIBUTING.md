# Contributing to Lucent

Thanks for considering a contribution to Lucent. We want Lucent to be useful to the people who build with it, so bug reports, docs fixes, small focused improvements, new integrations, and experiments with human–agent collaboration are all valuable contributions. This guide will help you get set up.

## Prerequisites

- Python 3.12+
- Docker and Docker Compose (v2+)
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

3. **Create a virtual environment and install dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```

4. **Set up environment:**
   ```bash
   cp .env.example .env
   # The DATABASE_URL below connects to the Docker Compose postgres service:
   export DATABASE_URL="postgresql://lucent:change-me-insecure-dev-password@localhost:5433/lucent"
   ```

5. **Run the server:**
   ```bash
   lucent
   ```

   Or use Docker Compose for the full stack:
   ```bash
   docker compose up
   ```

See the [Development Guide](docs/development.md) for more options including the full Docker development setup.

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
4. Open a pull request that explains what you changed, why it helps, and any tradeoffs or follow-up ideas worth discussing.

## Reporting Issues

Use [GitHub Issues](https://github.com/kahinton/lucent/issues) to report bugs, share an idea, or request a feature. Include:
- Steps to reproduce (for bugs)
- Expected vs actual behavior
- Python version and OS

## Security Issues

**Do not open public issues for security vulnerabilities.** See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.

## License

By contributing, you agree that your contributions will be licensed under the [Lucent Source Available License 1.0](LICENSE). Note that this is a source-available license, not an OSI-approved open source license.
