# Tools and Tool Builder

Tools are persistent, reviewable capabilities that agents can use when a task needs a concrete external action, such as calling an API, transforming data, or wrapping a specialized workflow.

In the Agent Composer UI, all agent-callable capabilities live on one **Tools** page:

- **Custom tools** are Lucent-hosted tools. Lucent runs their Python source in a sandbox with JSON schemas, scoped credentials, resource limits, and network policy.
- **External tool providers** are MCP server connections. They bring in tools from another service; after granting a provider to an agent, admins choose which discovered tools that agent may call.

The Agent Wizard can propose a custom tool when a user describes a new capability, an admin reviews and approves it, and the tool is granted to specific agents.

## Lifecycle

1. **Propose** — A user or Agent Wizard creates a managed tool definition with:
   - JSON input schema
   - optional JSON output schema
   - Python source code and entrypoint
   - runtime image/configuration
   - environment variables or credential references
   - network policy
   - resource limits
2. **Review** — The tool remains `proposed` until an admin/owner approves it.
3. **Grant** — Admins grant active tools to specific agent definitions.
4. **Invoke** — Agents call `run_managed_tool` by tool name. Lucent verifies the caller, agent grant, schema, and runtime policy before execution.
5. **Audit** — Each execution creates a `managed_tool_runs` record with status, timing, sandbox id, input/output payloads, and errors.

## Default security model

Custom tools fail closed by default:

- **No host execution** — tool source code runs inside a Lucent sandbox container.
- **No network by default** — `network_policy.network_mode` defaults to `none`; API tools should use `allowlist` with explicit hosts.
- **Agent grant required** — when invoked from an agent session, the trusted `X-Lucent-Agent-Definition-Id` context must match an `agent_managed_tools` grant.
- **User ACL required** — the caller must be able to access the tool definition by ownership, group, org-shared scope, built-in scope, or admin/owner override.
- **Schema-gated input/output** — input is validated before sandbox execution; output is validated after execution when an output schema is provided.
- **Scoped credentials** — environment values can use `secret://...` or `credential://...` references resolved for the authenticated user immediately before sandbox launch.
- **Resource bounded** — memory, CPU, disk, and timeout settings are part of the definition.

## Python runtime contract

The first supported runtime is Python. The tool source must define an entrypoint function, usually `handler`, that accepts one dictionary and returns JSON-serializable data.

Example:

```python
def handler(args):
    customer_id = args["customer_id"]
    return {"customer_id": customer_id, "status": "ok"}
```

Recommended input schema:

```json
{
  "type": "object",
  "properties": {
    "customer_id": {"type": "string"}
  },
  "required": ["customer_id"],
  "additionalProperties": false
}
```

## API and MCP surfaces

REST endpoints for custom tools are available under `/api/definitions/tools` and `/api/definitions/agents/{agent_id}/tools`. External tool providers continue to use the MCP server definition endpoints.

MCP tools include:

- `list_tool_definitions`
- `get_tool_definition`
- `create_tool_definition`
- `grant_tool_to_agent`
- `revoke_tool_from_agent`
- `run_managed_tool`

Approval and rejection are intentionally not exposed as MCP tools. The Agent Wizard can propose tools but does not approve, grant, or run them from chat. Human review remains the activation boundary through the web UI or authenticated REST endpoints.
