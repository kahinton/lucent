# Tools and Tool Builder

Tools are persistent, reviewable capabilities that agents can use when a task needs a concrete external action, such as calling an API, transforming data, or wrapping a specialized workflow.

In the Agent Composer UI, all agent-callable capabilities live on one **Tools**
page:

- **Managed tools** are Lucent-executed tools. A definition picks one of four
  executor kinds (see below), carries JSON input/output schemas, optional scoped
  credentials, and a network policy.
- **External tool providers** are MCP server connections. They bring in tools from
  another service; after granting a provider to an agent, admins choose which
  discovered tools that agent may call.

The Agent Wizard can propose a tool when a user describes a new capability, an
admin reviews and approves it, and the tool is granted to specific agents.

## Executor kinds

Every managed tool has exactly one `runtime_type`. This is the one field that
decides *who executes the tool* and *how much trust it requires* — the security
envelope around it (owner, agent grant, per-user consent, schemas, credential
references, network policy, resource limits, run ledger) is identical for all four.

| Kind | Who executes | Needs a sandbox | Source code | Credentials |
|---|---|---|---|---|
| `python` | Lucent, inside a sandbox container | **yes** | yes | env refs |
| `http` | Lucent server, as one outbound HTTPS request | no | no | `{{env:NAME}}` |
| `openapi` | Lucent server, one operation from a stored spec | no | no | `{{env:NAME}}` |
| `builtin` | Lucent server, a reviewed native handler | no | no | none (handler-owned) |

Only `python` requires `features.sandboxes_enabled`. A deployment that runs no
containers can still offer declarative and native tools — which is the point of the
other three kinds.

### `python` — sandboxed source

Lucent runs your Python entrypoint inside an isolated container. Use it for
computation, parsing, or logic that genuinely needs code.

```python
def handler(args):
    customer_id = args["customer_id"]
    return {"customer_id": customer_id, "status": "ok"}
```

The function receives one dictionary and must return JSON-serializable data.
`requirements` installs pip packages inside the container (never on the host).

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

### `http` — declarative request

One HTTPS call, described instead of coded. Nothing is executed as code.

```json
{
  "url": "https://api.example.com/v1/weather/{city}",
  "method": "GET",
  "query": {"units": "{units}", "appid": "{{env:api_key}}"},
  "headers": {"Accept": "application/json"},
  "response_path": "data.forecast",
  "response_format": "json",
  "allowed_status": [200],
  "timeout_seconds": 30
}
```

- `{name}` interpolates a **model-supplied argument**. Substituted path values are
  percent-encoded (including dots, so `..` cannot walk up the path). Other values
  are inserted literally and never re-expanded, so an argument cannot reach a
  credential by echoing `{{env:...}}`.
- `{{env:NAME}}` interpolates a resolved **credential** from `env_vars`. Credentials
  must be declared as `secret://` or `credential://` references; a literal secret in
  `env_vars` is rejected at write time.
- `body` may be a JSON object (templated recursively) or a raw string for
  XML/SOAP/GraphQL. `form` sends `application/x-www-form-urlencoded`. Setting both
  is an error.
- `response_path` selects a subtree of the JSON response with a dotted path
  (`data.items[0].price`).
- `allowed_status` lists the codes treated as success. Anything else is a failed run
  with the status reported, not a crash.

### `openapi` — one operation from a document

Point at an OpenAPI 3.x or Swagger 2.0 document, name one operation, and Lucent
derives the rest.

```json
{
  "spec": "{\"openapi\":\"3.0.0\",\"paths\":{...}}",
  "operation_id": "getWeather",
  "response_path": "data",
  "headers": {"Authorization": "Bearer {{env:api_key}}"}
}
```

The **document is stored on the definition**, not fetched at call time. That is
deliberate: a fetch-at-call-time spec would let an approved tool change behaviour
underneath the model, and would turn every invocation into an outbound request to an
endpoint the approver never saw. Refreshing a spec is an explicit edit that re-enters
review.

The `input_schema` is derived from the operation: path and query parameters and a
JSON body's object properties are flattened into one object. Supported: local `$ref`
only (remote refs are refused), path/query/header parameters, JSON and form-encoded
request bodies. Refused rather than half-supported: remote refs, cookie parameters,
non-JSON media types, and operations whose parameter names collide.

### `builtin` — reviewed native handler

A handler from Lucent's own catalog. No code to maintain, no container to start, and
no way for a definition to widen the handler's contract: the model-facing schema comes
from the catalog, and the runtime re-validates arguments against the catalog on every
call, not against the stored row.

Handlers ship in two tiers, and the bar for entry is deliberately high: a handler
earns a slot only if the model **structurally cannot** do the thing, or **must not**.
Anything a model can do in two lines of sandboxed code is not a tool.

**Core** — standard library plus `httpx`, always available:

| Handler | What it does | Why it is not just code |
|---|---|---|
| `web_search` | Keyword web search (no API key required) | Needs egress the model never has |
| `web_fetch` | Fetch a public page and return readable text | Needs egress, plus SSRF protection |
| `weather_forecast` | Open-Meteo current conditions and forecast | Curated no-key API; low value but genuinely needs the network |

**Optional** — needs a third-party package installed at image build time:

| Handler | Package | Build flag |
|---|---|---|
| `pdf_extract_text` | `pypdf` | `LUCENT_TOOLS_ENABLE_DOCUMENTS` |
| `docx_extract_text` | `python-docx` | `LUCENT_TOOLS_ENABLE_DOCUMENTS` |
| `xlsx_read` | `openpyxl` | `LUCENT_TOOLS_ENABLE_DOCUMENTS` |
| `image_info` | `Pillow` | `LUCENT_TOOLS_ENABLE_IMAGES` |

Enable them without editing the compose file:

```bash
LUCENT_TOOLS_ENABLE_DOCUMENTS=1 docker compose up -d --build
```

An optional handler whose package is absent still appears in the catalog, marked
**unavailable** with the flag needed to enable it. It fails with an actionable error,
never a traceback.

Ask the server what this build supports:

```
list_tool_runtime_kinds()
```

Returns every kind (with `available` and `requires_sandbox`), every handler (with its
`input_schema` and availability), and whether sandboxes are enabled.

## Operator-provisioned tools (instance catalog)

An instance operator (hyperadmin) can push a definition into a specific
organization. This is how you host a tool and offer it to tenants without asking
each one to author it — and it is the foundation a marketplace would sit on.

**It is a separate API surface**, deliberately:

```
PUT    /api/organizations/{org_id}/catalog/mcp-servers        # create or replace
GET    /api/organizations/{org_id}/catalog/{definition_type}  # what is provisioned here
DELETE /api/organizations/{org_id}/catalog/mcp-servers/{name} # revoke from one tenant
GET    /api/organizations/catalog/{definition_type}/organizations  # fleet view
```

`/api/definitions` answers *"what may I use in my organization?"* and derives the
tenant from the caller's own membership. These routes answer *"what am I pushing
into someone else's organization?"* Every one of them writes into another tenant's
rows, so mixing them into the tenant-authoring module is how cross-tenant mistakes
happen. They require `org.manage.instance`; a tenant owner is **not** enough (tested).

### One row per organization, on purpose

A provisioned definition is one row **in each target organization** — not a single
shared `NULL`-org row. That is less efficient and strictly safer:

- every row names its tenant, so no authorization predicate ever has to special-case
  `NULL`;
- no cross-tenant read is possible by construction;
- revoking from one tenant is a plain `DELETE`, not a visibility flag that some other
  query might miss.

### It behaves exactly like a shipped built-in

The row is `scope='built-in'`:

- consent-exempt, readable org-wide, no per-user owner;
- protected from daemon mutation (`BuiltInProtectionError`);
- tenants cannot edit or delete it, the same way they cannot edit a shipped skill.

**And it carries `origin='operator'`**, which is what keeps it safe. Without that
second fact the disk sync would reconcile it: an operator row sharing a name with a
file on disk would be **overwritten on the next restart**, and a name the disk
catalogue does not carry would be **archived as "retired"**. Both failures are
silent, and both are covered by tests.

| `origin` | Who owns the row | Reconciled by the disk sync |
|---|---|---|
| `disk` | `.github/` in this repo | yes — updated, pruned |
| `operator` | the instance operator | **never** |

### Provisioning grants nothing

Pushing a connector makes it *available* in that tenant. A tenant admin still decides
which of their agents may use it, the requesting user still consents, and each user
supplies their own credential through the declared `credential_slots`. The operator
controls availability, not reach.

### What a marketplace would still need

The expensive parts already exist — proposed → approved lifecycle, ownership and
clearances, per-user capability consent, credential slots, and a per-run ledger for
metering. Missing: publisher identity and attribution, a per-tenant
enable/disable toggle (as distinct from provision/revoke), listing visibility rules,
and a pricing hook on the ledger.

## Built-in connectors (no admin required)

Some vendors publish a **hosted** MCP server at a stable URL. Those are shipped as
built-in connectors: seeded into every organization as `scope='built-in'`, already
active, and granted to the default agent. No administrator authors anything.

The important design point is that **the endpoint is shared, and the credential is
not**. A shipped connector carries no token. It declares a credential slot and stores
only a `secret://` reference:

```json
"headers": {"Authorization": "secret://github.mcp.token"},
"credential_slots": [
  {"name": "GITHUB_TOKEN", "header": "Authorization", "format": "Bearer {value}",
   "label": "GitHub personal access token", "secret_key": "github.mcp.token",
   "create_url": "https://github.com/settings/tokens"}
]
```

Resolution runs through the clearance-driven secret store **for the calling user** —
their own secret first, then a group share, then an explicit grant. That gives three
useful properties:

| Situation | Result |
|---|---|
| The user added `github.mcp.token` | Works for them immediately |
| An admin added it organization-wide | Works for everyone cleared for it — one token for the team |
| Nobody added it | Connector reported as **needs setup**, not as an error |

### Shipped today

| Connector | Endpoint | Credential |
|---|---|---|
| GitHub | `api.githubcopilot.com/mcp/readonly` | `github.mcp.token` (PAT) |
| Sentry | `mcp.sentry.dev/mcp` | `sentry.mcp.token` (auth token) |

GitHub ships the **read-only** endpoint by default. A built-in that can push to every
repository a token reaches is a poor first-run default; an owner can point the URL at
`/mcp/` to enable writes.

### What the user sees

The connector's detail page has a **"Your access to this connector"** panel computed
from the same resolution the runtime uses, so it cannot claim readiness when a call
would fail:

- green **Connected** when the slot resolves;
- red **Needs your token** with the exact secret key and a link to the vendor's token
  page when it does not.

### When a credential is missing at call time

The connector is omitted from that user's tool surface, and the model is told why:

> ## Connectors that need this user's credentials
> These connectors are available to this agent but the user has not added the
> credential they require, so they are NOT connected right now. Do not claim you
> cannot access the service at all: tell the user to add the secret in Settings →
> Secrets, then retry.

This matters more than it looks. Before it, a missing `secret://` raised a bare
`KeyError` during config assembly and took down the **entire turn** — one unconfigured
connector cost the user every other tool, and the failure said nothing about the fix.
The daemon path had the same defect, plus it routed `sse` connectors into the stdio
branch and tried to spawn a command that does not exist. Both are fixed.

### Adding a shipped connector

Append a `BuiltinConnector` to `src/lucent/services/builtin_connectors.py`, then
declare its name under `mcp_server_names` in the default agent's `AGENT.md`. The tests
enforce: absolute `https://`, at least one credential slot, a dotted stable
`secret_key`, and that **no header or env value is ever a literal** — references only.

## Connecting real services

The **+ Connect Tool Provider** modal includes a curated catalog of remote MCP
servers published by the vendors themselves. Selecting one fills in the endpoint; it
grants nothing, and the normal proposed → approved lifecycle still applies.

The entries are ordered by **onboarding cost**, because that is what decides whether
a connection is a two-second click or a half-hour of admin work:

| Badge | Meaning |
|---|---|
| **Sign in** | The vendor supports OAuth with dynamic client registration (DCR) or client ID metadata documents (CIMD). Click, sign in, done. |
| **API token** | The user pastes a token from the vendor's settings page. Easy, but a long-lived credential — store it as a `secret://` reference, never inline. |
| **Admin setup** | The vendor requires a **pre-registered OAuth application**. Someone must create an app in that vendor's console and share a client ID/secret first. Badged so nobody starts a flow they cannot finish. |
| **No sign-in** | Public, read-only, unauthenticated. |

Sign-in entries always sort first within a category.

### Coverage, honestly

Every endpoint in the catalog was verified against the vendor's own documentation.
Roughly 60% of the consumer services we looked for have **no official remote MCP
server** and are deliberately absent rather than guessed — a plausible URL that 404s
fails *after* the user has committed.

Notably absent, and why:

| Missing | Status |
|---|---|
| Microsoft 365 (Outlook, OneDrive, Teams) | Official path is stdio-only (`npx @microsoft/workiq`), not a remote connector |
| Google Workspace (Gmail, Drive, Calendar) | Remote, but gated behind a Developer Preview plus a heavy GCP OAuth setup |
| Salesforce, Zendesk, Intercom, Pipedrive | No vendor-operated server found |
| Dropbox, Trello, Discord, Spotify, YouTube | No official server found |
| QuickBooks, Xero, Plaid | Could not verify |
| Datadog | Docs publish a per-site placeholder, no concrete URL |
| Philips Hue, SmartThings, Uber, DoorDash, Yelp | Could not verify |

For anything on that list, **Zapier** is the honest interim answer: one OAuth
connection covers 9,000+ apps, at 2 tasks per tool call.

### Adding an entry

Append a `ConnectorPreset` to `src/lucent/services/connector_catalog.py`. The tests
enforce the discipline that matters: absolute `https://`, no documentation
placeholders, a unique slug, a name within the 64-character column, and a
`setup_note` on every `admin_app` entry. Reject guesses — an unverified endpoint
belongs in the docs' "missing" table, not in the catalog.

## Lifecycle

1. **Propose** — create a definition: input schema (derived for `openapi`, fixed for
   `builtin`), optional output schema, the kind-specific config, environment or
   credential references, network policy, resource limits.
2. **Review** — the tool stays `proposed` until an admin/owner approves it.
3. **Grant** — admins grant active tools to specific agent definitions.
4. **Invoke** — agents call `run_managed_tool` by tool name. Lucent verifies the
   caller, agent grant, per-user consent, schemas, and runtime policy before
   execution.
5. **Audit** — each execution creates a `managed_tool_runs` record with status,
   timing, sandbox id (if any), input/output payloads, and errors.

## Default security model

All kinds fail closed by default:

- **Agent grant required** — when invoked from an agent session, the trusted
  `X-Lucent-Agent-Definition-Id` context must match an `agent_managed_tools` grant.
- **Per-user consent required** — the requesting user must have approved the
  capability for that agent, unless exempt (built-in scope, the user who attached it,
  or its owner). See `docs/security-model.md`.
- **User ACL required** — the caller must be able to read the definition by ownership,
  group, organization-shared scope, or built-in scope.
- **Schema-gated input/output** — input is validated before execution; output is
  validated afterwards when an output schema is provided.
- **No network by default** — `network_policy.network_mode` defaults to `none`.
  - `allowlist` permits only `allowed_hosts`. A `host` entry without a port covers
    only 80/443; use `host:port` for anything else.
  - `bridge` permits any **public** host on 80/443.
  - Loopback, RFC 1918, link-local, CGNAT, IPv6 ULA, multicast, and cloud-metadata
    addresses are **always** blocked, on every redirect hop, for every kind.
- **Credential isolation** — credentials are resolved just in time for the
  authenticated user only, are never stored as literals, never appear in the run
  ledger or model context (URLs are redacted to scheme/host/path), and are dropped
  from a redirect that leaves the origin they were configured for.
- **Resource bounded** — memory, CPU, disk, and timeout are part of the definition.
  Server-executed kinds are capped at 120s because there is no container to reap.

## Adding a network tool

A tool that needs the internet must say so. For a built-in handler the seeded default
already names the handler's own hosts. For `http` and `openapi`:

```json
"network_policy": {"network_mode": "allowlist", "allowed_hosts": ["api.example.com"]}
```

Deployment-level allowlisting of a *private* host (`LUCENT_MCP_URL_ALLOWLIST`) does
**not** let a tool reach it: the tool's own allowlist is an additional restriction,
never a bypass.

## API and MCP surfaces

REST endpoints for managed tools are available under `/api/definitions/tools` and
`/api/definitions/agents/{agent_id}/tools`. `GET /api/definitions/tools/runtime-kinds`
returns the kind and handler catalog. External tool providers use the MCP server
definition endpoints, which accept `http` (Streamable HTTP), `sse` (legacy HTTP+SSE),
and `stdio` transports.

MCP tools include:

- `list_tool_runtime_kinds`
- `list_tool_definitions`
- `get_tool_definition`
- `create_tool_definition`
- `grant_tool_to_agent`
- `revoke_tool_from_agent`
- `run_managed_tool`

Approval and rejection are intentionally not exposed as MCP tools. The Agent Wizard
can propose tools but does not approve, grant, or run them from chat. Human review
remains the activation boundary through the web UI or authenticated REST endpoints.

## Adding a new runtime kind

The runtime layer is a small, deliberately boring extension point. To add one:

1. Implement `ToolRuntime` in `src/lucent/services/tool_runtimes/<kind>_runtime.py`:
   a `runtime_type`, a `requires_sandbox` answer, and
   `async execute(context) -> RuntimeExecutionResult`.
2. Register it in `RUNTIME_CATALOG` and `_load_runtimes()` in
   `tool_runtimes/registry.py`.
3. Add its required fields to `_validate_tool_shape` in `db/definitions.py` so a
   malformed definition fails at write time.
4. Widen the `runtime_type` CHECK constraint in a migration.

Do **not** re-implement authorization. Every kind is authorized by
`ManagedToolExecutor` before `execute()` is called; a runtime receives an
already-authorized request and must never widen its own permissions.
