# Security Model

Lucent implements a multi-tenant, role-based security architecture with fine-grained
resource ownership. Every request is authenticated, every resource is scoped to an
organization, and access decisions follow a deterministic resolution chain that
combines ownership, group membership, and role-based overrides.

This document covers the security primitives, how they compose, and how they are
enforced throughout the system.

---

## Authentication

Lucent supports three authentication mechanisms: session-based authentication
for the web UI, API key authentication for programmatic access, and SCIM bearer
tokens for directory synchronization. All three resolve to the same internal
`CurrentUser` context, ensuring uniform authorization downstream.

### Session-Based Authentication (Web UI)

Sessions are managed via secure HTTP cookies. A session can be created by four
paths, and every one of them ends in the same `create_session()` call so the
cookie contract is identical regardless of how the person proved who they are:

| Path | How the identity is established |
|------|---------------------------------|
| Password login (`POST /login`) | bcrypt comparison against `users.password_hash` |
| Invitation acceptance (`POST /invite/{token}`) | single-use token, bound to one mailbox |
| Self-serve signup (`POST /signup`) | the person creates the account and its tenant |
| SSO (`/sso/{slug}/start` → callback) | OIDC `id_token` or SAML assertion, signature verified |

The token flow:

1. The identity check succeeds and yields a database user row
2. Server generates a 48-byte URL-safe random token (`secrets.token_urlsafe(48)`)
3. The SHA-256 hash of the token is stored in `user_sessions`, one row per device
4. The plaintext token is set as the `lucent_session` cookie

**Session Validation:**

On each request, the middleware hashes the cookie value and looks it up in
`user_sessions`. The session is valid only if:

- A row exists with that `token_hash`
- `expires_at > NOW()` (TTL not exceeded)
- `users.is_active = true` (account not deactivated)
- The organization's `status` is `active` (not suspended)

**Cookie Configuration:**

| Parameter    | Value        | Purpose                          |
|-------------|-------------|----------------------------------|
| `httponly`  | `true`      | Prevents JavaScript access       |
| `samesite`  | `lax`       | CSRF protection                  |
| `secure`    | Configurable | HTTPS-only when enabled          |
| `path`      | `/`         | Sent on all paths                |

**Environment Variables:**

| Variable                    | Default | Description                          |
|----------------------------|---------|--------------------------------------|
| `LUCENT_SESSION_TTL_HOURS` | `24`    | Session lifetime in hours            |
| `LUCENT_SECURE_COOKIES`    | `true`  | Require HTTPS for cookies            |
| `LUCENT_SELF_SIGNUP_ENABLED` | `false` | Offer the public `/signup` form    |
| `LUCENT_PUBLIC_URL`        | unset   | Externally visible base URL, used when a server (not a browser) needs to build a link |

Setting `LUCENT_SECURE_COOKIES=false` logs a warning — it should only be used for
local HTTP development.

**Session Lifecycle:**

- Login creates a new session for that device and leaves other devices signed in
- Logout destroys only the presented token
- A password change rotates this device's token and revokes every other session
- Deactivation, an admin password reset, and SCIM deactivation all destroy every
  session for the user immediately

### Password Policy and Credential Handling

Passwords are hashed with **bcrypt** and compared with `bcrypt.checkpw`. A
complexity policy (minimum 8 characters, at least one uppercase, one lowercase,
one digit) is enforced server-side on every path that accepts a password.

A user may hold no password at all. This is a supported state, not an accident:
a directory-provisioned account (SCIM), or an account created through an
invitation whose invitee intends to sign in through SSO, has no local
credential and can only be accessed through its identity provider.

**Temporary passwords** are generated only for admin-created accounts that the
administrator did not give a password for. They are returned exactly once, and
the account is flagged `force_password_change`, which redirects every web page to
`/force-password-change` until the person sets their own password. Two newer
flows deliberately avoid this handoff entirely — invitation links and self-serve
signup both let the person choose their own password before the account is ever
usable.

### Single Sign-On (OIDC, GitHub OAuth, SAML 2.0)

SSO is configured per tenant in **Settings → Identity & access** and stored in
`sso_connections`. An enabled connection's button appears automatically on the
sign-in page. The rules below are enforced in code, not convention:

**Trust anchors**

- An **OIDC** connection names an `issuer`. Discovery is fetched from
  `{issuer}/.well-known/openid-configuration` over TLS and the document's own
  `issuer` field must match. The `id_token` is verified against the JWKS from
  that document with `aud` = our client id, `iss` = the configured issuer, and a
  required `nonce`.
- `alg` is restricted to asymmetric algorithms. Accepting `HS256` would let
  anyone who knows the public client id mint tokens.
- A **SAML** connection pins one or more IdP certificates. Assertion signatures
  are verified against those certificates and nothing else — no key is ever
  fetched from the IdP at login time.
- **GitHub** is plain OAuth 2.0 with no `id_token`, so identity comes from
  GitHub's REST API. It is modelled as its own provider type rather than being
  disguised as OIDC, because there is nothing to signature-check.

**SAML assertion checks** (all mandatory)

1. A signature exists on the assertion, the response, or both — an entirely
   unsigned assertion is always rejected, whatever the connection's flags say.
2. The signature validates against a pinned certificate.
3. `InResponseTo` matches a request Lucent actually issued; the request id comes
   from the database row keyed by `RelayState`, not from the POST body.
4. `Audience` is this service provider; a `Destination`/`Recipient` present in
   the document must be our ACS URL.
5. `NotBefore`/`NotOnOrAfter` are satisfied, with 120 seconds of clock skew.
6. The assertion `ID` is unspent. Spent ids are recorded in
   `saml_assertion_replays` for the assertion's lifetime.

XML is parsed with entity expansion, DTDs, and network access disabled, and no
`<!DOCTYPE>` is accepted.

**Replay protection.** OIDC state and PKCE verifier, the OIDC nonce, and SAML
`RelayState`/request id are stored in `sso_login_states` keyed by the SHA-256 of
the state value. Consuming a row is a single atomic
`UPDATE ... WHERE consumed_at IS NULL RETURNING`, so a second callback with the
same state finds nothing. Rows expire after 10 minutes.

**Account resolution** (one implementation, shared by every federation path)

1. Match on `(provider, external_id)` — the IdP's stable subject. This wins over
   email, because a mailbox can change but a subject usually cannot.
2. Fall back to email **within the connection's organization only**, and only
   when the IdP asserts `email_verified`, the existing account has
   `email_verified = true`, and the connection's `allow_email_linking` is on. An
   unproven address must never be able to adopt an existing identity.
3. Otherwise create the account, if `auto_create_users` is enabled.

**Hard limits**

- A federated identity can never become `hyperadmin`. That role is granted only
  by the out-of-band CLI.
- An identity already bound in one tenant is refused by another tenant's
  connection rather than being moved between them.
- Daemon service identities can never be adopted by a login flow.
- `allowed_email_domains` restricts which mailboxes a shared IdP may vouch for.
  An empty list means unrestricted, which is why it is strongly recommended for
  Google and Microsoft.
- Role changes are **not** re-derived from group claims on each login. Role is
  refreshed only at JIT-creation time, so an IdP cannot silently promote or
  demote a Lucent administrator on every sign-in.

### SCIM 2.0 Directory Provisioning

Enterprise identity providers keep the directory in sync through `/scim/v2`, the
standard SCIM root. It is authenticated by its own bearer-token family, `sc_...`,
stored in `scim_tokens` (prefix for lookup, bcrypt hash of the whole value).

| Property | Behaviour |
|----------|-----------|
| Token scope | One organization. The tenant comes from the token, never from the payload. |
| Assignable roles | `member`, `admin`. Never `owner` or `hyperadmin`. |
| Created accounts | No password. They sign in through SSO or an admin reset. |
| Deactivation (`active: false`) | Sets `is_active = false` **and** destroys all sessions. |
| `DELETE` on a used account | Deactivates rather than deletes, so history stays attributable. |
| `DELETE` on an unused account | Deletes outright, so a mistaken provisioning is undoable. |
| Daemon identities | Invisible to and untouchable by SCIM. |
| Audit | Every mutation is logged with the token as actor, not a person. |

SCIM tokens are separate from user API keys on purpose: a directory token can
create and remove people, which no user's API key should imply, and rotating the
directory credential must not disturb anyone's API access.

### Invitation Links

An admin (or the hyperadmin API) can mint a single-use invitation for one email
address, valid for a configurable window (24 hours by default).

- Only the SHA-256 of the token is stored, so a database read yields no usable
  credential.
- The invitation's email is authoritative: redemption creates an account for the
  address the link was sent to. A forwarded link therefore cannot become an
  account-creation primitive for a different mailbox.
- Accepted, revoked, expired, and unknown tokens all produce the same message, so
  the page cannot be used to probe which addresses were invited.
- Acceptance is an atomic claim (`UPDATE ... WHERE accepted_at IS NULL`); a lost
  race rolls the newly created account back.
- An admin cannot invite a role above their own, and `hyperadmin` is never
  invitable.

### Email Uniqueness

`idx_users_org_email_unique` makes `(organization_id, lower(email))` unique for
human members. Without it, two members of one tenant could share an address and
SSO or SCIM identity matching by email would become ambiguous — an
account-takeover shape rather than a nuisance. The constraint is scoped per
tenant because one person may legitimately hold accounts in two tenants, and it
excludes `daemon` identities because they share a synthetic address by
construction.

### CSRF Protection

Web forms use a double-submit cookie pattern:

1. A `lucent_csrf` cookie is set with a random token
2. Forms include the same token as a hidden field (`csrf_token`)
3. On POST, the server verifies the cookie value matches the form field

This requires no server-side session store for CSRF tokens. The `samesite=lax`
cookie policy provides additional defense against cross-origin requests.

### API Key Authentication (Programmatic Access)

API keys provide stateless authentication for the REST API, CLI tools, and daemon
processes.

**Key Format:**

```
hs_<32 bytes of URL-safe random data>
```

The `hs_` prefix allows quick identification. The first 11 characters (prefix) are
stored in plaintext for efficient lookup; the full key is stored as a bcrypt hash.

**Usage:**

```
Authorization: Bearer hs_abc123...
```

**Key Properties:**

| Field        | Description                                         |
|-------------|-----------------------------------------------------|
| `key_prefix` | First 11 characters — used for DB lookup            |
| `key_hash`   | bcrypt hash of the full key                         |
| `scopes`     | JSON array (e.g., `["read", "write"]`, `["daemon-tasks"]`) |
| `expires_at` | Optional expiration timestamp                       |
| `is_active`  | Active flag (can be revoked)                        |
| `revoked_at` | Timestamp when revoked (null if active)             |

**Verification Flow:**

1. Extract `Bearer` token from `Authorization` header
2. Query `api_keys` by `key_prefix` (first 11 chars), joined with `users` to
   verify the user is active and the key is not revoked
3. Compare the full key against bcrypt hash(es) — prefix collisions are handled
   by checking all matching rows
4. On success, update `last_used_at` and `use_count` for telemetry

```sql
SELECT ak.id, ak.user_id, ak.key_hash, ak.scopes, ...
FROM api_keys ak
JOIN users u ON ak.user_id = u.id
WHERE ak.key_prefix = $1
  AND ak.is_active = true
  AND ak.revoked_at IS NULL
  AND u.is_active = true
```

### Authentication Resolution

Both mechanisms produce a `CurrentUser` object that carries the authenticated
identity through the request:

```python
class CurrentUser:
    id: UUID                         # User ID (from database)
    organization_id: UUID | None     # Organization boundary
    role: Role                       # member, admin, or owner
    auth_method: str                 # "session", "api_key", or "oauth"
    api_key_id: UUID | None          # Present if API key auth
    api_key_scopes: list[str]        # Scopes from the API key
    impersonator_id: UUID | None     # Set during impersonation
```

Key design principle: **the user identity always comes from the database, never
from request parameters.** There is no way for a caller to inject or override the
authenticated user ID.

---

## RBAC (Role-Based Access Control)

### Roles

Lucent defines three roles in a strict hierarchy:

| Role     | Level | Description                                    |
|----------|-------|------------------------------------------------|
| `member` | 0     | Standard user — manages own resources          |
| `admin`  | 1     | Organization administrator — manages users and org-wide resources |
| `owner`  | 2     | Organization owner — full control including destructive operations |

Roles are comparable: `Role.ADMIN >= Role.MEMBER` evaluates to `true`. This allows
concise guard expressions like `if user.role >= Role.ADMIN`.

Each organization has exactly one `owner` (enforced by a unique index). The
`member` role is the default for new users.

### Permission Matrix

**Member Permissions:**

| Category | Permissions |
|----------|------------|
| Memory   | `create`, `read.own`, `read.shared`, `update.own`, `delete.own`, `share` |
| Audit    | `view.own` |
| Access   | `view.own` |
| Users    | `view` |
| Org      | `view` |

**Admin Permissions** (all member permissions plus):

| Category     | Additional Permissions |
|-------------|----------------------|
| Memory      | `read.all` (org-wide) |
| Audit       | `view.org` |
| Access      | `view.org` |
| Users       | `invite`, `manage` |
| Integrations | `manage` |

**Owner Permissions** (all admin permissions plus):

| Category | Additional Permissions |
|----------|----------------------|
| Memory   | `delete.any` |
| Org      | `update`, `delete`, `transfer` |

### Permission Checking

Permissions are enforced via two complementary patterns:

**1. Instance method on CurrentUser:**

```python
user.require_permission(Permission.USERS_MANAGE)
# Raises HTTPException(403) if the user's role lacks the permission
```

**2. Dependency injection for route-level guards:**

```python
@router.get("/admin")
async def admin_route(user: CurrentUser = Depends(require_role(Role.ADMIN))):
    ...
```

### Role Management Rules

| Action | Owner | Admin | Member |
|--------|-------|-------|--------|
| Manage any user | ✓ | Members only | ✗ |
| Assign any role | ✓ | `member` only | ✗ |
| Delete users    | ✓ | Members only | ✗ |

---

## Groups and Membership

Groups are a core security primitive that enables shared resource ownership and
collaborative access control. They are available in all deployment modes.

### What Groups Are

A group is a named collection of users within an organization. Groups serve two
primary purposes:

1. **Resource ownership** — Resources can be owned by a group, granting access to
   all group members
2. **Access scoping** — Secrets and definitions scoped to a group are visible to
   all members of that group

### Group Schema

| Field             | Type          | Description               |
|-------------------|---------------|---------------------------|
| `id`              | UUID          | Primary key               |
| `name`            | VARCHAR(128)  | Unique within organization |
| `description`     | TEXT          | Optional description      |
| `organization_id` | UUID          | Parent organization       |
| `created_by`      | UUID          | User who created the group |
| `created_at`      | TIMESTAMPTZ   | Creation timestamp        |

### Membership

The `user_groups` junction table tracks membership:

| Field      | Type         | Description                        |
|-----------|-------------|-------------------------------------|
| `user_id`  | UUID        | Member user                         |
| `group_id` | UUID        | Group                               |
| `role`     | VARCHAR(16) | `member` or `admin` (within group)  |

Group membership cascades on delete — removing a user or group automatically
cleans up the membership records.

### API Endpoints

| Method   | Endpoint                         | Permission Required    |
|----------|----------------------------------|----------------------|
| `POST`   | `/api/groups`                    | `users.manage`       |
| `GET`    | `/api/groups`                    | Authenticated        |
| `GET`    | `/api/groups/{id}`               | Authenticated (org member) |
| `PUT`    | `/api/groups/{id}`               | `users.manage`       |
| `DELETE` | `/api/groups/{id}`               | `admin` role or above |
| `POST`   | `/api/groups/{id}/members`       | `users.manage`       |
| `DELETE` | `/api/groups/{id}/members/{uid}` | `users.manage`       |
| `GET`    | `/api/groups/{id}/members`       | Authenticated        |

### Groups and the ACL

When a resource has `owner_group_id` set, any member of that group can access the
resource (read). This is resolved via a subquery in the access control SQL — see
[Access Control Resolution Chain](#access-control-resolution-chain).

**Performance:** Group membership lookups use a 5-second TTL in-memory cache to
reduce database load on repeated access checks within the same request cycle.

---

## Resource Ownership Model

### Resources with Ownership

The following resource types support ownership tracking:

| Resource              | Table                  | Has `scope` | Has `owner_user_id` | Has `owner_group_id` |
|-----------------------|-----------------------|:-----------:|:-------------------:|:-------------------:|
| Agent Definitions     | `agent_definitions`   | ✓           | ✓                   | ✓                   |
| Skill Definitions     | `skill_definitions`   | ✓           | ✓                   | ✓                   |
| MCP Server Configs    | `mcp_server_configs`  | ✓           | ✓                   | ✓                   |
| Hook Definitions      | `hook_definitions`    | ✓           | ✓                   | ✓                   |
| Sandbox Templates     | `sandbox_templates`   | ✓           | ✓                   | ✓                   |
| Models                | `models`              | —           | ✓                   | ✓                   |
| Secrets               | `secrets`             | —           | ✓                   | ✓                   |

### Ownership Types

**Built-in Scope** (`scope = 'built-in'`):
Resources shipped with Lucent. They have no owner (both `owner_user_id` and
`owner_group_id` are NULL) and are read-accessible to all organization members.
Built-in resources cannot be modified or deleted by users.

**User Ownership** (`owner_user_id`):
The resource belongs to a specific user. The owner has full read/write/delete
access. It is not visible or manageable by unrelated organization administrators
or owners. Set automatically during resource creation to the authenticated user's ID.

**Group Ownership** (`owner_group_id`):
The resource belongs to a group. All members of the group can access (read) the
resource. Group administrators can modify resources owned by their group.

**Organization-shared definitions** (`scope = 'instance'` with both owner columns NULL):
Instance-scoped definitions with neither `owner_user_id` nor `owner_group_id` set
are shared with the entire organization. All organization members can read/use
them, but only organization `admin`/`owner` users can modify them. Daemon-created
instance definitions default to this state so the daemon is recorded as the actor
(`created_by`) without becoming the human-facing owner.

**Model audiences** (both owner columns NULL, or one owner set):
Models use the same user/group ownership checks without a `scope` column. An
unowned model is available to the whole organization, a user-owned model is
available only to that user, and a group-owned model is available to group
members. Admins and owners can manage all models in their organization. Global
unowned catalog rows (`organization_id IS NULL`) remain visible across
organizations for backward compatibility; assigning an audience binds the model
to the administrator's organization.

### Database Constraints

Definition-style resources enforce single-owner semantics: a row may be
user-owned, group-owned, or organization-shared, but not both user- and
group-owned:

```sql
CHECK (NOT (owner_user_id IS NOT NULL AND owner_group_id IS NOT NULL))
```

For secrets, which don't use the `scope` field, at least one owner is always required:

```sql
CHECK (owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
```

Secrets have composite unique constraints to prevent key collisions within an
ownership scope:

```sql
UNIQUE (key, organization_id, owner_user_id)
UNIQUE (key, organization_id, owner_group_id)
```

### How Ownership Is Set

During resource creation, the system determines ownership from the request context:

1. If the user selects organization sharing, both owner columns are left NULL on
   the instance-scoped row
2. If the user specifies a `owner_group_id`, the system validates that the user is
   a member of that group before assigning group ownership
3. If no scope is specified, `owner_user_id` is set to the authenticated user's ID
4. If an unscoped daemon creates an instance definition, the definition defaults
   to organization-shared instead of daemon-owned
5. Built-in scope is reserved for system-shipped resources and cannot be set via
   the API

### Ownership Transfer

Resource ownership can be changed by users with modification access (the current
owner, or an admin/owner role). The update operation replaces the `owner_user_id`
or `owner_group_id` fields, with the same membership validation applied to group
transfers.

---

## Access Control Resolution Chain

Every resource access decision follows a deterministic resolution chain, implemented
as a SQL WHERE clause that evaluates conditions in priority order.

### Resolution Order

```
1. Built-in scope  →  ALLOW (read-only, all org members)
2. Direct owner    →  ALLOW (owner_user_id = requesting user)
3. Group member    →  ALLOW (owner_group_id in user's groups)
4. Org-shared      →  ALLOW (scope='instance' and both owner columns are NULL)
5. Unowned pending definition review → ALLOW to organization `admin`/`owner`
6. Default         →  DENY
```

### SQL Implementation

The canonical ACL pattern, used consistently across all resource-type queries:

```sql
AND (
    a.scope = 'built-in'
    OR a.owner_user_id = $3
    OR a.owner_group_id IN (
        SELECT group_id FROM user_groups WHERE user_id = $3
    )
   OR (a.scope = 'instance' AND a.owner_user_id IS NULL AND a.owner_group_id IS NULL)
)
```

Where `$3` is the requesting user's ID and `$4` is their role.

This pattern appears in:
- `db/definitions.py` — Agent, skill, and MCP server config queries
- `db/sandbox_template.py` — Sandbox template queries
- `secrets/builtin.py` — Secret scope resolution

### Read vs. Modify Access

The resolution chain above governs **read access**. Modification access is more
restrictive:

| Role   | Can Modify                                           |
|--------|------------------------------------------------------|
| Owner  | Directly owned resources, group-admin resources, and organization-shared resources |
| Admin  | Directly owned resources, group-admin resources, and organization-shared resources |
| Member | Directly owned resources; group-owned resources only when they are a group admin |

Organization-shared definitions are readable by all organization members but are
admin/owner-only for modification.

### Organization Boundary

All queries include an `organization_id` filter. This is the outermost security
boundary — users cannot access resources from other organizations under any
circumstances, regardless of role.

```sql
WHERE a.organization_id = $1
  AND (/* ACL resolution chain */)
```

---

## Request-Scoped Resource Tracing

### User Context Propagation

Every request carries an authenticated user context established at the entry point
and propagated via Python `ContextVar` objects:

```python
_current_user: ContextVar[dict]          # Authenticated user record
_current_api_key_id: ContextVar[UUID]    # API key ID (if key-authenticated)
_impersonating_user: ContextVar[dict]    # Original user (during impersonation)
```

**Web routes** establish context via `get_user_context()`, which validates the
session cookie and loads the user record from the database.

**API routes** establish context via the `get_current_user` dependency, which
validates the `Authorization: Bearer` header and loads the user record.

**MCP routes** establish context via `MCPAuthMiddleware`, which accepts session
tokens in the Bearer header.

### `requesting_user_id` Pattern

Throughout the codebase, the requesting user's ID is passed explicitly to
repository methods and audit logging:

```python
requesting_user_id = str(user.id)  # Always from authenticated context
```

This value is:
- Used for ownership checks in ACL queries
- Logged in audit trail entries
- Passed to the access control service for resource-level checks

### Anti-Spoofing Design

The system prevents user impersonation through several layered defenses:

**1. Server-side identity resolution:**
The user ID is **always** extracted from the authenticated session or API key
record in the database. It is never read from request parameters, headers (other
than the auth header), or request body fields. There is no mechanism for a caller
to specify "act as user X" through normal API parameters.

**2. Impersonation controls:**
For legitimate administrative impersonation (e.g., debugging user issues):

| Impersonator Role | Can Impersonate           |
|-------------------|---------------------------|
| Owner             | Anyone except other owners |
| Admin             | Members only               |
| Member            | No one                     |

Impersonation uses a signed cookie (`lucent_impersonate`) with HMAC-SHA256,
bound to the session hash to prevent cookie theft. The original user is preserved
in the `_impersonating_user` context variable and included in all audit entries
via `impersonator_id`.

**3. Session binding:**
Session tokens are single-use per user (logging in invalidates the previous
session). The session is bound to the user record — changing the session cookie
to another user's token fails because the SHA-256 hash won't match.

**4. Organization scoping:**
Even if a user could somehow forge an identity, all queries are scoped to the
user's `organization_id`. Cross-organization access is structurally impossible
at the query level.

### Audit Context

The `CurrentUser` object provides `get_audit_context()` for comprehensive logging:

```python
{
    "auth_method": "session" | "api_key",
    "api_key_id": "uuid",           # If API key auth
    "api_key_scopes": ["read", "write"],
    "impersonator_id": "uuid",      # If impersonated
    "impersonator_display_name": "...",
    "is_impersonated": true | false
}
```

---

## Outbound Network Policy (Egress)

Tools and MCP servers are the two places where Lucent makes requests to hosts a
tenant chose. Both are confined, but by different mechanisms because they run in
different places.

### Sandboxed tools (`runtime_type='python'`)

The sandbox backend enforces `network_policy` at the container level:
`none` blocks all egress, `allowlist` permits only `allowed_hosts`, and `bridge`
permits any reachable address. Because the guest cannot escape the container's
network namespace, this is the strongest of the three controls.

### Server-executed tools (`http`, `openapi`, `builtin`)

These run **in the server process**, so the server itself is the boundary. Every
request — including each redirect hop — passes through
`lucent.services.tool_runtimes.http_policy`, which enforces:

1. **The tool's own policy.** `network_mode` must be `allowlist` or `bridge`; a
   `host` entry without a port covers only 80/443, and `bridge` is likewise limited
   to 80/443 so "any public host" cannot become "any public port".
2. **SSRF protection**, independently of the tool's policy. Loopback, RFC 1918,
   link-local, carrier-grade NAT, IPv6 unique-local, IPv4-mapped loopback,
   multicast, reserved, and the cloud metadata endpoint are blocked. NAT64
   (`64:ff9b::/96`), which embeds an IPv4 address, is blocked too.
3. **Transport integrity.** A redirect may not downgrade HTTPS to HTTP.
4. **Credential scoping.** Headers carrying a credential are dropped when a
   redirect leaves the origin that credential was configured for. httpx's cookie
   jar is disabled, because a `Set-Cookie` from hop 1 would otherwise be re-attached
   to hop 2 and bypass that drop.
5. **Bounded bodies.** Request and response sizes are capped, and the response is
   read as a stream so a hostile endpoint cannot exhaust server memory.

The deployment-wide `LUCENT_MCP_URL_ALLOWLIST` relaxes (2) for named private hosts.
It does **not** relax (1): a tool's own `allowed_hosts` is an additional
restriction, never a bypass. Both must agree.

### MCP servers

Remote MCP endpoints are validated with the same SSRF rules before a session is
opened, and that validation happens in the **server**, before the config reaches an
engine SDK — not only inside the LangChain bridge. The Copilot, Claude Code, and
Codex engines receive an MCP config dict and open the connection themselves, so a
check that lived only in the bridge would leave those engines unvalidated.

A single unreachable or blocked MCP server is skipped with a warning rather than
aborting the session: one misconfigured provider must not cost the agent every other
tool.

### Prompt-injection scanning

Declarative tool configuration is model-visible. `runtime_config` (URL templates,
header values, builtin options) is scanned for injection alongside `source_code` and
the description, because an instruction hidden in a header value is read by the same
context that reads the description.

## Secret Storage Architecture

Lucent provides encrypted secret storage with a pluggable provider architecture.
Secrets are scoped to organizations and owned by users or groups, following the
same ownership model as other resources.

> **For full configuration details**, including setup instructions for each
> provider, see [Secret Storage Configuration](secret-storage.md).

### Provider Pattern

The secret provider is selected via the `LUCENT_SECRET_PROVIDER` environment
variable (default: `builtin`):

| Provider  | Status | Backend                          |
|-----------|--------|----------------------------------|
| `builtin` | ✅ Production | PostgreSQL + Fernet encryption  |
| `vault`   | 🔧 Planned   | HashiCorp Vault KV v2           |
| `aws`     | 🔧 Planned   | AWS Secrets Manager             |
| `azure`   | 🔧 Planned   | Azure Key Vault                 |

All providers implement the `SecretProvider` abstract base class with a uniform
interface: `get()`, `set()`, `delete()`, and `list()`.

### Built-in Provider Encryption

The built-in provider encrypts secret values at rest in PostgreSQL using Fernet
symmetric encryption (AES-128 in CBC mode with HMAC-SHA256 authentication).

**Key Derivation:**

```python
dk = hashlib.pbkdf2_hmac(
    "sha256",
    secret_key.encode("utf-8"),       # From LUCENT_SECRET_KEY env var
    salt=b"lucent-secrets-v1",        # Fixed, deterministic salt
    iterations=480_000,               # NIST-recommended iteration count
    dklen=32,
)
fernet_key = base64.urlsafe_b64encode(dk)
```

The `LUCENT_SECRET_KEY` environment variable is **required** when using the
built-in provider. It should be a high-entropy string (minimum 32 characters
recommended).

### Secret Protocol: `secret://`

Resources that accept sensitive values (e.g., MCP server environment variables,
managed-tool environment references) can reference secrets by name using the
`secret://` protocol:

```
secret://my_api_key
```

**Resolution Flow:**

1. Parse the key name from the `secret://` reference
2. Query the secrets table, searching scopes in order:
   - User-owned secrets (`owner_user_id = current_user`)
   - Group-owned secrets (for each group the user belongs to)
3. Decrypt and return the first match
4. Raise `KeyError` if no matching secret is found

This allows environment variables to reference encrypted secrets without exposing
plaintext values in configuration.

### Secrets Table Schema

| Column            | Type         | Description                    |
|-------------------|-------------|--------------------------------|
| `id`              | UUID        | Primary key                    |
| `key`             | VARCHAR(256)| Secret name                    |
| `encrypted_value` | BYTEA       | Fernet-encrypted value         |
| `owner_user_id`   | UUID        | User owner (nullable)          |
| `owner_group_id`  | UUID        | Group owner (nullable)         |
| `organization_id` | UUID        | Organization boundary          |
| `created_at`      | TIMESTAMPTZ | Creation timestamp             |
| `updated_at`      | TIMESTAMPTZ | Last update timestamp          |

---

## Audit Trail

Lucent maintains two complementary audit logs for accountability and forensics.

### Audit Log (`memory_audit_log`)

Records all state-changing operations on memories and system resources.

**Tracked Action Types:**

| Category    | Actions                                                           |
|-------------|------------------------------------------------------------------|
| Memory      | `create`, `update`, `delete`, `restore`, `hard_delete`           |
| Sharing     | `share`, `unshare`                                                |
| Definitions | `definition_create`, `definition_update`, `definition_approve`, `definition_reject`, `definition_delete`, `definition_grant`, `definition_revoke` |
| Secrets     | Secret CRUD events (added in migration 041)                      |
| Integrations| `signature_verification_failed`, `channel_not_allowed`, `integration_event`, and others |

**Entry Fields:**

| Field            | Description                                     |
|------------------|-------------------------------------------------|
| `memory_id`      | Affected resource (may reference deleted items) |
| `user_id`        | User who performed the action                   |
| `organization_id`| Organization context                            |
| `action_type`    | One of the tracked action types                 |
| `changed_fields` | Array of field names modified (updates only)    |
| `old_values`     | Previous field values (JSONB)                   |
| `new_values`     | New field values (JSONB)                        |
| `context`        | Request metadata (IP, user agent, API version)  |
| `version`        | Version number for point-in-time restore        |
| `snapshot`       | Full resource state at this version (JSONB)     |

### Access Log (`memory_access_log`)

Records read operations on memories for usage analytics and access auditing.

**Access Types:**

| Type            | Description                                |
|-----------------|--------------------------------------------|
| `view`          | Direct memory retrieval                    |
| `search_result` | Memory appeared in search results          |

**Entry Fields:**

| Field             | Description                          |
|-------------------|--------------------------------------|
| `memory_id`       | Accessed memory                      |
| `user_id`         | User who accessed                    |
| `organization_id` | Organization context                 |
| `access_type`     | `view` or `search_result`            |
| `context`         | Search query, filters, referrer      |

### Access Log API Endpoints

| Endpoint                          | Description                    | Permission       |
|-----------------------------------|-------------------------------|-------------------|
| `GET /api/access/memory/{id}`     | Access history for a memory   | Own or admin      |
| `GET /api/access/user/{id}`       | User's access activity        | Own or admin      |
| `GET /api/access/most-accessed`   | Most-accessed memories        | Authenticated     |

Admins can view org-wide access history. Members can only view their own access
records and access history for memories they own.

---

## Environment Variables Reference

| Variable                  | Default    | Description                              |
|---------------------------|-----------|------------------------------------------|
| `LUCENT_SESSION_TTL_HOURS`| `24`      | Session cookie lifetime                  |
| `LUCENT_SECURE_COOKIES`   | `true`    | Require HTTPS for cookies                |
| `LUCENT_SIGNING_SECRET`   | Random    | HMAC key for signed cookies (persist across restarts) |
| `LUCENT_SECRET_PROVIDER`  | `builtin` | Secret storage backend                   |
| `LUCENT_SECRET_KEY`       | —         | Encryption key for built-in secret provider (required) |

---

## Security Design Principles

1. **Defense in depth** — Authentication, authorization, organization scoping,
   and resource ownership each provide independent security boundaries
2. **Least privilege** — The `member` role has minimal permissions; elevation
   requires explicit role assignment by an admin or owner
3. **Fail closed** — The ACL resolution chain defaults to deny; resources are
   inaccessible unless an explicit allow condition is met
4. **No trust in client input** — User identity is always derived from
   server-validated credentials, never from request parameters
5. **Auditability** — All state changes and access events are logged with full
   user context, including impersonation tracking
6. **Encryption at rest** — Secrets are encrypted using Fernet before storage;
   plaintext values never persist in the database
