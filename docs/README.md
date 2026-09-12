# Lucent Documentation

Lucent is a flexible, self-hostable workspace for people and AI agents to collaborate on real work. These guides help you start small, shape Lucent around your workflow, and grow only into the features you need.

## Start here

- **[Getting Started](getting-started.md)** — Run Lucent locally, create an account, connect an MCP client, and try a first workflow.
- **[Architecture](architecture.md)** — See how the server, daemon, MCP tools, and source layout fit together.
- **[Configuration](configuration.md)** — Explore environment variables and settings when you are ready to customize your setup.

## Build and collaborate

- **[Agent Integration](agent-integration.md)** — Bring an external agent into a Lucent workflow.
- **[Tools](managed-tools.md)** — Create and manage tools agents can use.
- **[Handoffs](api-reference.md#handoffs)** — Let people and agents exchange questions, decisions, outputs, and clarifications around a task.
- **[Memory Lifecycle](memory-lifecycle-design.md)** — Understand the optional lifecycle tools for context that should persist.
- **[API Reference](api-reference.md)** — Use the REST API to build your own integrations or interface.

## Run your way

- **[Connections](connections.md)** — Connect services and manage personal, shared, or managed credentials.
- **[Sandboxes](sandboxes.md)** — Configure isolated execution for code and tool work.
- **[Deployment Guide](deployment-guide.md)** — Deploy with Docker Compose.
- **[Kubernetes Deployment](kubernetes-deployment.md)** — Use the Helm chart and operator deployment.
- **[Operator Guide](operator-guide.md)** — Administration and maintenance.
- **[Observability](observability.md)** — OpenTelemetry, Prometheus, Jaeger, and Grafana.
- **[Troubleshooting](troubleshooting.md)** — Common issues and fixes.

## Integrations

- **[Integrations API Reference](integrations-api-reference.md)** — Integrate external services through the REST API.
- **[Slack Admin Setup](slack-admin-setup.md)** — Configure the Slack app for a workspace.
- **[Slack User Linking](slack-user-linking.md)** — Pair individual Slack users with Lucent accounts.
- **[Slack Security](slack-security.md)** — Review the Slack integration security model.

## Develop and secure

- **[Development Guide](development.md)** — Set up locally, test changes, and contribute.
- **[Security Model](security-model.md)** — Authentication, authorization, multi-tenancy, and auditability.
- **[Secret Storage](secret-storage.md)** — Configure pluggable encryption providers (OpenBao, Fernet, Vault).
- **[Migration Guide: Security](migration-guide-security.md)** — Move to the current security features.
