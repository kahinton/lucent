# Security Model

Lucent implements a multi-tenant, role-based security architecture with fine-grained
resource ownership. Every request is authenticated, every resource is scoped to an
organization, and access decisions follow a deterministic resolution chain that
combines ownership, group membership, and role-based overrides.

This document covers the security primitives, how they compose, and how they are
enforced throughout the system.

---

## Authentication

Lucent supports two authentication mechanisms: session-based authentication for
the web UI and API key authentication for programmatic access. Both resolve to the
same internal `CurrentUser` context, ensuring uniform authorization downstream.

### Session-Based Authentication (Web UI)

Sessions are managed via secure HTTP cookies. The flow:

1. User authenticates via an auth provider (Google, GitHub, SAML, or local credentials)
2. Server generates a 48-byte URL-safe random token (`secrets.token_urlsafe(48)`)
3. The SHA-256 hash of the token is stored in the `users.session_token` column
4. The plaintext token is set as the `lucent_session` cookie

**Session Validation:**

On each request, the middleware hashes the cookie value and compares it against the
stored hash. The session is valid only if:

- The hash matches `users.session_token`
- `session_expires_at > NOW()` (TTL not exceeded)
- `users.is_active = true` (account not deactivated)

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

Setting `LUCENT_SECURE_COOKIES=false` logs a warning — it should only be used for
local HTTP development.

**Session Lifecycle:**

- Login creates a new session, invalidating any previous token for that user
- Logout calls `destroy_session()`, clearing the token from the database
- Only one active session per user at a time

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
| Sandbox Templates     | `sandbox_templates`   | ✓           | ✓                   | ✓                   |
| Secrets               | `secrets`             | —           | ✓                   | ✓                   |

### Ownership Types

**Built-in Scope** (`scope = 'built-in'`):
Resources shipped with Lucent. They have no owner (both `owner_user_id` and
`owner_group_id` are NULL) and are read-accessible to all organization members.
Built-in resources cannot be modified or deleted by users.

**User Ownership** (`owner_user_id`):
The resource belongs to a specific user. The owner has full read/write/delete
access. Set automatically during resource creation to the authenticated user's ID.

**Group Ownership** (`owner_group_id`):
The resource belongs to a group. All members of the group can access (read) the
resource. Only users with `admin` or `owner` role, or the direct user owner, can
modify group-owned resources.

### Database Constraints

Every non-built-in resource must have an owner:

```sql
CHECK (scope = 'built-in' OR owner_user_id IS NOT NULL OR owner_group_id IS NOT NULL)
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

1. If the user specifies a `owner_group_id`, the system validates that the user is
   a member of that group before assigning group ownership
2. If no group is specified, `owner_user_id` is set to the authenticated user's ID
3. Built-in scope is reserved for system-shipped resources and cannot be set via
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
4. Role override   →  ALLOW (user role is admin or owner)
5. Default         →  DENY
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
    OR $4 IN ('admin', 'owner')
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
| Owner  | Any resource in the organization                     |
| Admin  | Any resource in the organization                     |
| Member | Only resources where `owner_user_id` matches their ID |

Group members can **read** group-owned resources but cannot **modify** them unless
they are also an admin/owner or the direct user owner.

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

Resources that accept sensitive values (e.g., MCP server environment variables)
can reference secrets by name using the `secret://` protocol:

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
