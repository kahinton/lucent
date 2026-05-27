# Sandboxes

Sandboxes are isolated execution environments for running code, installing dependencies, and performing tasks without affecting the host system. They are Docker containers managed by Lucent's sandbox API.

## Overview

When Lucent needs to execute code — whether dispatched by a daemon task, a schedule, or a user request — it creates a sandbox. Each sandbox is:

- **Isolated**: Runs in its own Docker container with configurable resource limits
- **Ephemeral**: Created on demand and destroyed after use
- **Configurable**: Custom images, environment variables, network access, and setup commands
- **Devcontainer-aware**: Automatically detects and applies `.devcontainer/devcontainer.json` configs from cloned repositories

## Creating a Sandbox

### Via API

```bash
curl -X POST http://localhost:8766/api/sandboxes \
  -H "Authorization: Bearer hs_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "python:3.12-slim",
    "repo_url": "https://github.com/your-org/your-repo",
    "branch": "main",
    "setup_commands": ["pip install -r requirements.txt"],
    "memory_limit": "2g",
    "cpu_limit": 2.0,
    "timeout_seconds": 1800
  }'
```

### Via Templates

Templates are reusable sandbox configurations. Create a template once, then launch instances from it:

```bash
# Create a template
curl -X POST http://localhost:8766/api/sandboxes/templates \
  -H "Authorization: Bearer hs_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "python-dev",
    "image": "python:3.12-slim",
    "repo_url": "https://github.com/your-org/your-repo",
    "setup_commands": ["pip install -r requirements.txt"],
    "memory_limit": "4g"
  }'

# Launch from template
curl -X POST http://localhost:8766/api/sandboxes/templates/{template_id}/launch \
  -H "Authorization: Bearer hs_your_key"
```

Templates can be referenced by tasks and schedules via the `sandbox_template_id` field.

---

## Devcontainer Support

Lucent automatically detects and applies [devcontainer](https://containers.dev/) configurations when a sandbox clones a repository. This means repos that already have a `.devcontainer/devcontainer.json` will have their development environment set up automatically — no extra configuration needed.

### How Detection Works

After a repository is cloned into the sandbox, Lucent checks for a devcontainer config in this order:

1. `.devcontainer/devcontainer.json` (standard path)
2. `.devcontainer.json` (root-level alternative)

The first file found is parsed and applied. If neither exists, the sandbox continues with just the user-specified setup commands.

### What Happens When a Devcontainer Is Detected

The sandbox creation flow with devcontainer support:

```
1. Create container with base image
2. Clone repository
3. Detect devcontainer.json
4. If devcontainer specifies a different image → rebuild container with that image
5. If devcontainer specifies a Dockerfile → build image and rebuild container
6. Run lifecycle commands (onCreateCommand → updateContentCommand → postCreateCommand)
7. Apply environment variables from containerEnv and remoteEnv
8. Run user-specified setup_commands (from API request)
9. Mark sandbox as READY
10. Run postStartCommand (after READY status)
```

### Supported Fields

| Field | Type | Supported | Notes |
|-------|------|-----------|-------|
| `image` | string | ✅ | Triggers container rebuild with the specified image |
| `build.dockerfile` | string | ✅ | Builds the Dockerfile inside the sandbox |
| `build.context` | string | ✅ | Build context directory (default: `.devcontainer`) |
| `build.args` | object | ✅ | Passed as `--build-arg` to `docker build` |
| `dockerFile` | string | ✅ | Legacy field, same as `build.dockerfile` |
| `onCreateCommand` | string/list/object | ✅ | Runs first during setup |
| `updateContentCommand` | string/list/object | ✅ | Runs after onCreateCommand |
| `postCreateCommand` | string/list/object | ✅ | Runs after updateContentCommand |
| `postStartCommand` | string/list/object | ✅ | Runs after sandbox is marked READY |
| `postAttachCommand` | string/list/object | ⚠️ | Parsed but not executed (no attach concept) |
| `containerEnv` | object | ✅ | Environment variables set in the container |
| `remoteEnv` | object | ✅ | Overlaid on top of containerEnv |
| `forwardPorts` | int[] | ✅ | Stored for reference; port forwarding depends on network mode |
| `remoteUser` | string | ✅ | Stored for reference |
| `features` | object | ⚠️ | Parsed and stored but **not installed** (see Limitations) |

### Command Formats

Lifecycle commands (`onCreateCommand`, `postCreateCommand`, etc.) accept three formats per the devcontainer spec:

**String** — runs as a single shell command:
```json
"onCreateCommand": "npm install"
```

**Array** — joined into a single command:
```json
"onCreateCommand": ["npm", "install"]
```

**Object** — each value runs as a separate command, in insertion order:
```json
"postCreateCommand": {
  "install": "npm install",
  "db": "npm run db:setup",
  "seed": "npm run db:seed"
}
```

### Lifecycle Command Execution Order

Commands execute in the order defined by the devcontainer spec:

```
┌─────────────────────┐
│ 1. onCreateCommand  │  First-time container setup
├─────────────────────┤
│ 2. updateContent    │  Content/dependency updates
│    Command          │
├─────────────────────┤
│ 3. postCreate       │  Post-setup tasks
│    Command          │
├─────────────────────┤
│ 4. (user setup_     │  Your API-specified commands
│    commands)        │
├─────────────────────┤
│  ── SANDBOX READY ──│
├─────────────────────┤
│ 5. postStartCommand │  Runs after READY status
└─────────────────────┘
```

Environment variables from `containerEnv` and `remoteEnv` are merged (remoteEnv wins on conflicts) and passed to every lifecycle command as an `env` prefix.

### Limitations and Unsupported Features

| Feature | Status | Details |
|---------|--------|---------|
| **Dev container features** | Not supported | `features` are parsed but not installed. Devcontainer features require the `devcontainer` CLI, which is not bundled. Use setup commands as a workaround. |
| **Docker Compose** | Not supported | `dockerComposeFile` and multi-container setups are not handled. |
| **postAttachCommand** | Not executed | Sandboxes have no attach/detach lifecycle. The field is parsed for reference only. |
| **Mounts / volumes** | Not supported | `mounts` and `workspaceMount` fields are ignored for security. |
| **GPU / privileged mode** | Not supported | `--privileged`, `--gpus`, and `capAdd` are ignored. Sandboxes run unprivileged. |
| **VS Code extensions** | Not applicable | `customizations.vscode.extensions` is ignored. |
| **Port forwarding** | Partial | `forwardPorts` are stored but actual port mapping depends on the sandbox network mode (`none`, `bridge`, or `allowlist`). |
| **Dockerfile build failures** | Non-fatal | If a devcontainer Dockerfile fails to build, the sandbox continues with the base image and logs a warning. |

### Workarounds for Unsupported Features

**Features**: Install what you need via `onCreateCommand`:
```json
{
  "image": "ubuntu:22.04",
  "onCreateCommand": "apt-get update && apt-get install -y git python3 python3-pip"
}
```

**Docker Compose**: Use a single-container config with setup commands to initialize services:
```json
{
  "image": "python:3.12",
  "postCreateCommand": "pip install -r requirements.txt"
}
```

### Example Devcontainer Configs

#### Python Project

```json
{
  "image": "python:3.12-slim",
  "onCreateCommand": "pip install --upgrade pip",
  "postCreateCommand": "pip install -e '.[dev]'",
  "containerEnv": {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1"
  }
}
```

#### Node.js Project

```json
{
  "image": "node:20",
  "onCreateCommand": "npm ci",
  "postCreateCommand": "npm run build",
  "postStartCommand": "npm run dev",
  "containerEnv": {
    "NODE_ENV": "development"
  },
  "forwardPorts": [3000]
}
```

#### Custom Dockerfile

```json
{
  "build": {
    "dockerfile": "Dockerfile.dev",
    "context": ".",
    "args": {
      "PYTHON_VERSION": "3.12"
    }
  },
  "postCreateCommand": {
    "deps": "pip install -r requirements.txt",
    "db": "python manage.py migrate"
  },
  "containerEnv": {
    "DATABASE_URL": "sqlite:///db.sqlite3"
  },
  "remoteUser": "appuser"
}
```

#### Multi-Step Setup

```json
{
  "image": "ubuntu:22.04",
  "onCreateCommand": {
    "system": "apt-get update && apt-get install -y python3 python3-pip nodejs npm",
    "python": "pip3 install poetry",
    "node": "npm install -g pnpm"
  },
  "updateContentCommand": "poetry install && pnpm install",
  "postCreateCommand": {
    "build": "pnpm run build",
    "test": "poetry run pytest --co -q"
  },
  "containerEnv": {
    "POETRY_VIRTUALENVS_IN_PROJECT": "true"
  }
}
```

### Inspecting Devcontainer Results

After sandbox creation, the response includes a `devcontainer` field (if one was detected) with a summary:

```json
{
  "id": "abc123",
  "name": "my-sandbox",
  "status": "ready",
  "devcontainer": {
    "image": "node:20",
    "build_dockerfile": null,
    "remote_user": "node",
    "forward_ports": [3000],
    "features": [],
    "lifecycle_commands": {
      "onCreateCommand": ["npm ci"],
      "postCreateCommand": ["npm run build"],
      "updateContentCommand": [],
      "postStartCommand": ["npm run dev"]
    },
    "env_var_count": 1
  }
}
```

> **Note**: The `devcontainer` summary is available on the `SandboxInfo` object returned internally. The REST API response (`SandboxResponse`) includes `id`, `name`, `status`, `container_id`, timestamps, and `error`. Use the `/exec` endpoint to inspect the sandbox environment directly if needed.

---

## Resource Limits

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `memory_limit` | `2g` | 1m–99999t | Container memory limit |
| `cpu_limit` | `2.0` | 0.1–16 | CPU core limit |
| `disk_limit` | `10g` | 1m–99999t | Disk space limit |
| `timeout_seconds` | `1800` | 60–86400 | Auto-destroy timeout |

## Network Modes

| Mode | Description |
|------|-------------|
| `none` | No network access (most secure, default) |
| `bridge` | Full network access via Docker bridge |
| `allowlist` | Network access restricted to `allowed_hosts` only |

## Lifecycle

1. **CREATING** — Container is being set up
2. **READY** — Container is running and setup complete
3. **STOPPED** — Container stopped but state preserved
4. **FAILED** — Creation or setup failed (check `error` field)
5. **DESTROYED** — Container permanently removed
