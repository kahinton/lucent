# Lucent

An MCP (Model Context Protocol) server that gives AI assistants persistent memory, autonomous task execution, and the ability to work between conversations.

More than a memory store — Lucent is the infrastructure for AI teammates that learn, plan, and act independently. It provides five memory types with fuzzy search, an autonomous daemon with four independent loops (cognitive reasoning, task dispatch, scheduling, and background learning), sandboxed Docker execution, and a full web dashboard — all behind a single `docker compose up -d`.

> **Project Status:** Active development (v0.2.0). Core features are stable. APIs may change before 1.0.

## Quick Start

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) v2+.

```bash
# 1. Clone and start
git clone https://github.com/kahinton/lucent.git
cd lucent
docker compose up -d

# 2. Open http://localhost:8766 — create your account and copy the API key

# 3. Add to your MCP client (VS Code .vscode/mcp.json shown):
```

```json
{
  "servers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "type": "http",
      "headers": {
        "Authorization": "Bearer hs_your_api_key_here"
      }
    }
  }
}
```

For other clients (Claude Desktop, GitHub Copilot CLI), see the [Getting Started](docs/getting-started.md) guide.

## Features

- **Persistent Memory** — five memory types (experience, technical, procedural, goal, individual) with fuzzy search, versioning, and rollback
- **Autonomous Daemon** — cognitive reasoning, event-driven task dispatch, cron scheduling, and background learning loops
- **Sandboxed Execution** — Docker-based isolated environments with resource limits, network policies, and auto-cleanup
- **Agent Definitions** — approval-gated registry for agents and skills, human-vetted before daemon use
- **Multi-Model Support** — per-task model selection from 20+ LLMs (OpenAI, Anthropic, Google) via GitHub Copilot SDK or LangChain engine
- **Web Dashboard** — manage memories, agents, schedules, sandboxes, activity tracking, and review queues
- **Pluggable Auth** — basic auth, API key auth, session management, RBAC
- **Secret Storage** — [OpenBao Transit](docs/secret-storage.md), builtin Fernet, or external Vault

## Documentation

| Guide | Description |
|-------|-------------|
| **[Getting Started](docs/getting-started.md)** | Full setup walkthrough, MCP client configuration, authentication |
| **[Architecture](docs/architecture.md)** | System design, components, daemon, MCP tools, source layout |
| **[Configuration](docs/configuration.md)** | All environment variables, Docker Compose options, settings |
| **[Development](docs/development.md)** | Contributing guide, local dev setup, testing, CI/CD |
| [API Reference](docs/api-reference.md) | REST API endpoints and parameters |
| [Deployment Guide](docs/deployment-guide.md) | Production deployment with Docker Compose |
| [Security Model](docs/security-model.md) | Authentication, authorization, multi-tenancy |
| [Secret Storage](docs/secret-storage.md) | Pluggable encryption providers |
| [Sandboxes](docs/sandboxes.md) | Docker sandbox configuration and lifecycle |
| [Observability](docs/observability.md) | OpenTelemetry, Prometheus, Jaeger, Grafana |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and fixes |
| [Kubernetes](docs/kubernetes-deployment.md) | Helm chart and operator deployment |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Development Guide](docs/development.md).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

Lucent Source Available License 1.0 — free for non-commercial use. Commercial use requires a separate license. Converts to Apache 2.0 after 2 years. See [LICENSE](LICENSE) for full terms.
