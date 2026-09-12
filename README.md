# Lucent

Lucent is a self-hostable workspace where people and AI agents can take useful work from a loose idea to a finished result—together.

Bring the models, tools, and working style that make sense for you. Lucent gives them a shared home for requests, tasks, handoffs, reviews, schedules, and the context worth carrying forward. Use it as a personal command center, a project hub for a small team, or the foundation for a fleet of specialized agents.

> **Project status:** Active development (v0.4.0). Core features are stable; APIs may evolve before 1.0.

## Why Lucent exists

Most AI tools are excellent at a single conversation. Real work is messier: it moves between people and agents, takes more than one sitting, needs feedback, and rarely fits one model or one vendor.

Lucent is built for that reality. It is deliberately flexible rather than prescriptive:

- **Make it yours** — run it locally or self-host it; use hosted models, local models, or both; connect the tools and services your work actually needs.
- **Work in the open** — turn an idea into a request, break it into tasks, follow the timeline, and keep the useful artifacts close to the work.
- **Collaborate across the human–agent boundary** — people can assign, clarify, review, redirect, and approve; agents can plan, execute, hand work off, and ask for help.
- **Build on shared context** — notes, decisions, procedures, and prior outcomes can be available when they help, without making every interaction start from scratch.
- **Choose the right amount of autonomy** — start with a chat-connected assistant, add background work or schedules when you want them, and keep review points where they matter.
- **Stay portable** — Lucent speaks MCP and REST, supports several model providers, and does not require you to commit to one client or one model stack.

The result is less like a chatbot with a long memory and more like a workshop: a place where humans and agents can pick up a piece of work, make progress, and leave it in better shape for the next collaborator.

## What you can do with it

| Build or use | Lucent provides |
|---|---|
| A personal AI workbench | MCP tools, a web dashboard, durable requests, and optional background help. |
| A shared project room | Task ownership, handoffs, review queues, timelines, and space for the decisions behind the work. |
| A team of specialists | Agent definitions, skills, managed tools, schedules, and parallel workers. |
| A private or hybrid AI stack | Local deployment, self-hosted model paths, and hosted providers through GitHub Copilot, OpenAI, Anthropic, Google, Ollama, and LangChain-backed providers. |
| Careful automation | Sandboxed execution, secret storage, approval gates, and audit trails when an agent needs to take action. |

## A workspace, not a workflow you have to adopt

Lucent can be as lightweight or as structured as you need it to be. A solo developer might connect it to an MCP client and keep project notes and recurring maintenance in one place. An open-source group might use requests and handoffs to let maintainers and agents collaborate on issues, docs, and release work. A larger team can add roles, integrations, separate workers, and stronger controls without changing the basic way work moves through the system.

Memory is part of that story, but it is not the center of it. Save context when it will help the next person or agent; skip it when a task should stay ephemeral. Lucent is there to support your practice, not impose one.

## How it works

Lucent runs as an application server plus a long-running agent process. The default Docker Compose stack starts one agent process; extra workers are opt-in.

```text
┌─────────────────────────────────────────────────────┐
│                   Lucent Server                      │
│                                                     │
│  MCP /mcp  ·  REST API /api/*  ·  Web Dashboard      │
│                                                     │
│  Requests · Tasks · Handoffs · Reviews · Schedules   │
│  Context · Definitions · Tools · Sandboxes · Secrets │
│                                                     │
│  PostgreSQL keeps the shared workspace durable       │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│                Lucent Agent Process                  │
│                                                     │
│  Planning · Task dispatch · Scheduling · Learning    │
│  Sandboxed execution · Human handoff                 │
└─────────────────────────────────────────────────────┘
```

The server keeps the workspace state and exposes the dashboard, API, and MCP interface. The agent process can plan work, dispatch tasks, run schedules, validate outputs, and add useful context back to Lucent. New requests can wake it immediately for focused task decomposition, while periodic cycles handle broader planning and maintenance. When there is nothing to do, maintenance cycles record a lightweight no-op rather than manufacturing work.

## Working together

1. **Start with a request.** A person or an agent creates a piece of work with the goal, context, and desired outcome.
2. **Let the right collaborator pick it up.** Lucent can track ownership and status, split eligible work into tasks, or wait for a human to decide what comes next.
3. **Keep the conversation attached to the work.** Handoffs make room for questions, clarifications, decisions, and deliverables instead of hiding them in a one-off chat.
4. **Review and iterate.** Route outputs through review when appropriate, ask for rework, and retain the history that explains how a result came to be.

This supports high-autonomy workflows, but it also works beautifully as a coordination layer around thoughtful human judgment.

## Quick start

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) v2+.

```bash
# Clone and start Lucent with one daemon worker
git clone https://github.com/kahinton/lucent.git
cd lucent
docker compose up -d
```

Open http://localhost:8766, create an account, choose at least one discovered model, and copy the API key shown during setup. Then add Lucent to your MCP client:

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

For a guided setup and configurations for other clients, see [Getting Started](docs/getting-started.md).

## Core pieces

- **MCP server** — brings Lucent's requests, tasks, handoffs, reviews, schedules, context, definitions, and tools into compatible AI clients.
- **REST API** — lets you build your own interfaces and integrations around the same workspace.
- **Web dashboard** — a home for work in progress, reviews, agent and workflow definitions, sandboxes, settings, and activity history.
- **Autonomous daemon** — optional background planning, dispatch, scheduling, and maintenance.
- **PostgreSQL persistence** — durable shared state, versions, access controls, and task history.
- **Sandbox manager** — isolated Docker environments for code and tool work, with optional reuse across sequential tasks in a request.
- **LLM engine layer** — provider flexibility through the GitHub Copilot SDK and LangChain-backed providers.

## Ideas to try

- Give your coding agent a shared place to track a feature from research through review.
- Coordinate issue triage, documentation cleanup, or release preparation with a mix of maintainers and agents.
- Build specialized agents for research, testing, code review, design notes, or any workflow your community needs.
- Schedule recurring chores—dependency checks, docs reviews, or project health checks—and review the results on your terms.
- Run a local-first setup with Ollama, or combine private infrastructure with the hosted models you prefer.
- Connect tools and services gradually, keeping credentials and execution boundaries under your control.

## Documentation

| Guide | Description |
|-------|-------------|
| **[Getting Started](docs/getting-started.md)** | Start locally, connect an MCP client, and try your first shared workflow |
| **[Architecture](docs/architecture.md)** | How the server, daemon, MCP tools, and source tree fit together |
| **[Configuration](docs/configuration.md)** | Environment variables, Docker Compose options, and feature flags |
| **[Development](docs/development.md)** | Local setup, tests, and contributing |
| [Agent Integration](docs/agent-integration.md) | Bring another agent into a Lucent workflow |
| [API Reference](docs/api-reference.md) | REST API endpoints and parameters |
| [Connections](docs/connections.md) | Connect services and manage credentials for local, team, or managed setups |
| [Deployment Guide](docs/deployment-guide.md) | Production deployment with Docker Compose |
| [Security Model](docs/security-model.md) | Authentication, authorization, tenancy, and auditability |
| [Secret Storage](docs/secret-storage.md) | Encryption providers and secret references |
| [Sandboxes](docs/sandboxes.md) | Docker sandbox configuration and lifecycle |
| [Observability](docs/observability.md) | OpenTelemetry, Prometheus, Jaeger, and Grafana |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and fixes |
| [Kubernetes](docs/kubernetes-deployment.md) | Helm chart and operator deployment |

## Contributing

Lucent gets more interesting when people bring their own workflows, integrations, agent ideas, and sharp edges. Bug reports, documentation improvements, experiments, and focused pull requests are all welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the [Development Guide](docs/development.md).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

Lucent Source Available License 1.0 — free for non-commercial use. Commercial use requires a separate license. Converts to Apache 2.0 after 2 years. See [LICENSE](LICENSE) for full terms.
