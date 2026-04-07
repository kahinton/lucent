# Getting Started

This guide walks you through installing Lucent, creating your first account, and connecting an MCP client.

## Prerequisites

- **Docker** and **Docker Compose** (v2+)
- An MCP-compatible client (VS Code, Claude Desktop, or GitHub Copilot CLI)
- Python 3.12+ (only if running outside Docker)

## 1. Clone and Start

```bash
git clone https://github.com/kahinton/lucent.git
cd lucent
docker compose up -d
```

This starts PostgreSQL, OpenBao (secret storage), and the Lucent server. All services run behind a single port (default `8766`).

## 2. Create Your Account

Open **http://localhost:8766** in your browser. On first run you'll see a setup page where you:

1. Create your user account (username, password)
2. Receive your MCP API key (shown once — **copy it!**)

You can generate additional API keys later at http://localhost:8766/settings.

## 3. Connect Your MCP Client

### VS Code (MCP Extension)

Add to `.vscode/mcp.json` in your project:

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

### GitHub Copilot CLI

Add to `.mcp.json` in your project root:

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

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lucent": {
      "url": "http://localhost:8766/mcp",
      "headers": {
        "Authorization": "Bearer hs_your_api_key_here"
      }
    }
  }
}
```

Replace `hs_your_api_key_here` with the API key from the setup page in all examples above.

## 4. Verify It Works

Ask your AI assistant something like:

> "Create a memory about this project"

If the assistant can create and retrieve memories, you're set.

## Authentication

Lucent uses a pluggable authentication system configured via `LUCENT_AUTH_PROVIDER`.

### Basic Auth (default)

Username/password authentication with bcrypt hashing. Configured automatically during first-run setup.

- **Web UI**: Session cookie (24-hour TTL, configurable via `LUCENT_SESSION_TTL_HOURS`)
- **MCP/API**: API key (`Authorization: Bearer hs_...`)

### API Key Auth

For simpler setups, authenticate the web UI with an API key instead of username/password:

```bash
export LUCENT_AUTH_PROVIDER=api_key
```

### Future Providers

- **OAuth**: GitHub/Google authentication
- **SAML/SCIM**: Enterprise SSO (team mode)

## Web Dashboard

Once running, the web UI at http://localhost:8766 provides:

| Page | Purpose |
|------|---------|
| `/` | Dashboard overview |
| `/memories` | Memory management UI |
| `/activity` | Request/task tracking and event timeline |
| `/definitions` | Agent, skill, and MCP server management |
| `/schedules` | Schedule creation and monitoring |
| `/sandboxes` | Sandbox template and instance management |
| `/daemon/review` | Review queue for daemon-generated content |
| `/audit` | Audit log viewer |
| `/users` | User management (admin) |
| `/settings` | API keys, password, profile |

## 5. Run the Daemon (Optional)

The Lucent server provides persistent memory and the web dashboard. The **daemon** adds autonomous capabilities — cognitive reasoning, task dispatch, scheduled work, and background learning.

The daemon connects to the server over MCP and requires a GitHub token for LLM access via the Copilot SDK:

```bash
# Required: GitHub personal access token with "copilot" scope
export GITHUB_TOKEN=your_github_token_here

# Required: MCP connection details
export LUCENT_MCP_URL=http://localhost:8766/mcp
export LUCENT_MCP_API_KEY=hs_your_api_key_here  # Same key from step 2

# Run the daemon
python -m daemon.daemon
```

Or use the multi-daemon Docker profile:

```bash
# Set your API key in .env first: LUCENT_MCP_API_KEY=hs_...
docker compose --profile multi-daemon up -d
```

See [Architecture — Autonomous Daemon](architecture.md#autonomous-daemon) for details on the four daemon loops.

## Next Steps

- [Architecture](architecture.md) — how Lucent's components fit together
- [Configuration](configuration.md) — all environment variables and settings
- [API Reference](api-reference.md) — REST API documentation
- [Deployment Guide](deployment-guide.md) — production deployment
- [Troubleshooting](troubleshooting.md) — common issues and fixes
