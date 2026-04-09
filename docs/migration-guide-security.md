# Security Upgrade Migration Guide

Upgrade guide for existing Lucent deployments to adopt the security features
introduced after migration 036: **groups**, **resource ownership**, **task
user tracking**, and **encrypted secret storage**.

---

## 1. Overview

This upgrade adds four capabilities to Lucent:

| Feature | What It Does |
|---|---|
| **Groups** | User-group directory model for organizing users and controlling access. Available in both personal and team mode. |
| **Resource Ownership** | Every agent definition, skill definition, MCP server config, and sandbox template now tracks an owning user or group. |
| **Task User Tracking** | Each dispatched task records the `requesting_user_id`, enabling audit trails back to the human who initiated work. |
| **Secret Storage** | Encrypted-at-rest secret store with pluggable backends (built-in Postgres/Fernet, HashiCorp Vault, AWS Secrets Manager, Azure Key Vault). Secrets can be referenced in MCP server env vars with the `secret://` prefix. |

### Why Upgrade

- **Audit & compliance** — resource ownership and task attribution give you a
  clear chain of custody.
- **Credential hygiene** — secrets are encrypted at rest instead of stored as
  plaintext env vars.
- **Access scoping** — group-based ownership lets teams share resources without
  exposing them org-wide.

### Backward Compatibility

- **Single-user (personal mode) deployments continue to work with no
  configuration changes.** Groups and ownership are opt-in.
- Existing API keys and sessions remain valid.
- All five migrations use `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` guards
  and are safe to re-run.
- The `LUCENT_SECRET_KEY` env var is only required when you use the built-in
  secret provider. If you never call the secrets API, the server starts
  normally without it.

---

## 2. New Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LUCENT_SECRET_PROVIDER` | No | `builtin` | Secret storage backend. One of: `builtin`, `vault`, `aws`, `azure`. |
| `LUCENT_SECRET_KEY` | When provider = `builtin` | *(none)* | Encryption key for the built-in Fernet-based secret store. Must be a strong, random string. **Keep this safe — losing it means losing access to all stored secrets.** |

### Provider-specific variables

These are only required when you choose a non-default secret provider.

#### HashiCorp Vault (`LUCENT_SECRET_PROVIDER=vault`)

| Variable | Required | Description |
|---|---|---|
| `VAULT_ADDR` | Yes | Vault API base URL (e.g. `https://vault.example.com`) |
| `VAULT_TOKEN` | Yes | Vault token with read/write access to the configured mount |

#### AWS Secrets Manager (`LUCENT_SECRET_PROVIDER=aws`)

| Variable | Required | Description |
|---|---|---|
| `AWS_REGION` or `AWS_DEFAULT_REGION` | Yes | AWS region for Secrets Manager API calls |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | One of these auth methods | Static credentials |
| `AWS_PROFILE` | One of these auth methods | Named profile from `~/.aws/credentials` |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | One of these auth methods | IRSA / EKS pod identity |

#### Azure Key Vault (`LUCENT_SECRET_PROVIDER=azure`)

| Variable | Required | Description |
|---|---|---|
| `AZURE_KEY_VAULT_URL` | Yes | Key Vault URL (e.g. `https://myvault.vault.azure.net`) |
| `AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET` | One of these auth methods | Service principal credentials |
| `AZURE_CLIENT_CERTIFICATE_PATH` | One of these auth methods | Certificate-based auth |
| `AZURE_FEDERATED_TOKEN_FILE` | One of these auth methods | Workload identity federation |

---

## 3. Schema Migration Steps

### Migration Files (037–041)

Lucent applies migrations automatically on server startup. The five new
migration files are:

| File | Description |
|---|---|
| `037_groups.sql` | Creates the `groups` and `user_groups` tables for the group directory model. Groups are scoped to an organization with unique names. Users can be `member` or `admin` of a group. |
| `038_resource_ownership.sql` | Adds `owner_user_id` and `owner_group_id` columns to `agent_definitions`, `skill_definitions`, `mcp_server_configs`, and `sandbox_templates`. Backfills ownership from `created_by` for existing instance-scoped rows. Adds `NOT VALID` CHECK constraints requiring ownership on non-built-in resources. |
| `039_requesting_user_id.sql` | Adds `requesting_user_id` to the `tasks` table and backfills it from `requests.created_by`. Enables audit trails from task execution back to the originating user. |
| `040_secrets.sql` | Creates the `secrets` table for encrypted secret storage with user/group ownership, organization scoping, and composite unique constraints on key + org + owner. |
| `041_secret_audit_types.sql` | Extends the `memory_audit_log` action type CHECK constraint with `secret_create`, `secret_read`, and `secret_delete` event types. |

### Running Migrations

Migrations run **automatically** when the Lucent server starts. The server
uses a `_migrations` tracking table to skip previously-applied files.

To upgrade, simply restart the server:

```bash
# Docker Compose
docker compose down && docker compose up -d

# Or restart just the Lucent service
docker compose restart lucent
```

To verify migrations were applied:

```bash
# Connect to the database
docker compose exec postgres psql -U lucent -d lucent

# Check the migrations table
SELECT name, applied_at FROM _migrations WHERE name LIKE '03%' OR name LIKE '04%' ORDER BY name;
```

Expected output should include all five files (`037_groups.sql` through
`041_secret_audit_types.sql`).

### Rollback Considerations

- Migrations 037–041 are **additive** — they create new tables and add new
  columns. They do not modify or remove existing columns.
- Migration 038 uses `NOT VALID` CHECK constraints, meaning existing rows are
  not validated at migration time. This makes the migration safe and fast even
  on large tables.
- If you need to roll back, drop the new objects in reverse order:

```sql
-- ⚠️  Only run these if you need to fully revert the security upgrade.
-- This will destroy all stored secrets and group memberships.

-- 041: Revert audit types (restore previous constraint)
ALTER TABLE memory_audit_log DROP CONSTRAINT memory_audit_log_action_type_check;
-- Re-add the previous constraint without secret_* types (see migration 029 for original list)

-- 040: Drop secrets table
DROP TABLE IF EXISTS secrets;

-- 039: Drop requesting_user_id
ALTER TABLE tasks DROP COLUMN IF EXISTS requesting_user_id;

-- 038: Drop ownership columns and constraints
ALTER TABLE agent_definitions DROP CONSTRAINT IF EXISTS ck_agent_def_owner_or_builtin;
ALTER TABLE skill_definitions DROP CONSTRAINT IF EXISTS ck_skill_def_owner_or_builtin;
ALTER TABLE mcp_server_configs DROP CONSTRAINT IF EXISTS ck_mcp_cfg_owner_or_builtin;
ALTER TABLE sandbox_templates DROP CONSTRAINT IF EXISTS ck_sandbox_tpl_owner_or_builtin;
ALTER TABLE agent_definitions DROP COLUMN IF EXISTS owner_user_id, DROP COLUMN IF EXISTS owner_group_id;
ALTER TABLE skill_definitions DROP COLUMN IF EXISTS owner_user_id, DROP COLUMN IF EXISTS owner_group_id;
ALTER TABLE mcp_server_configs DROP COLUMN IF EXISTS owner_user_id, DROP COLUMN IF EXISTS owner_group_id;
ALTER TABLE sandbox_templates DROP COLUMN IF EXISTS owner_user_id, DROP COLUMN IF EXISTS owner_group_id, DROP COLUMN IF EXISTS scope;

-- 037: Drop groups
DROP TABLE IF EXISTS user_groups;
DROP TABLE IF EXISTS groups;

-- Remove migration tracking entries
DELETE FROM _migrations WHERE name IN (
    '037_groups.sql', '038_resource_ownership.sql',
    '039_requesting_user_id.sql', '040_secrets.sql',
    '041_secret_audit_types.sql'
);
```

---

## 4. Backward Compatibility

### Single-User (Personal Mode) Deployments

No changes are required. The new tables and columns are created but remain
unused until you explicitly create groups or store secrets. The server starts
and operates identically to before.

### What Happens if `LUCENT_SECRET_KEY` Is Not Set

- The server starts normally.
- The built-in secret provider is **not initialized** until the first secret
  API call.
- If you attempt to store or retrieve a secret without `LUCENT_SECRET_KEY`
  set, you will receive a clear error:

  > `LUCENT_SECRET_KEY environment variable is not set. Secret storage
  > requires an encryption key. Generate one with:
  > python -c "import secrets; print(secrets.token_urlsafe(32))"`

- All non-secret functionality (memories, definitions, sandboxes, daemon)
  continues to work without it.

### Existing API Keys and Sessions

- All existing API keys remain valid with no changes.
- Active browser sessions are not invalidated.
- The new `requesting_user_id` column on tasks is backfilled from
  `requests.created_by` for all existing tasks, so historical audit data is
  preserved.

### Existing `secret://` References

If you have MCP server configs that reference env vars with plaintext values,
those continue to work. The `secret://` prefix is opt-in — only values
starting with `secret://` are resolved through the secret provider. Plaintext
values pass through unchanged.

---

## 5. Assigning Ownership to Existing Resources

### New Resources

All resources created after the upgrade automatically receive ownership:

- **Agent definitions, skill definitions, MCP server configs**: `owner_user_id`
  is set to the creating user's ID.
- **Sandbox templates**: `owner_user_id` is set to the creating user's ID; a
  `scope` column distinguishes `built-in` from `instance` templates.

### Pre-Existing Resources

Migration 038 **automatically backfills** ownership for existing resources:

```sql
-- This runs during migration 038 (you don't need to run it manually):
UPDATE agent_definitions
    SET owner_user_id = created_by
    WHERE (scope = 'instance' OR scope IS NULL)
      AND created_by IS NOT NULL
      AND owner_user_id IS NULL;
-- (Same for skill_definitions, mcp_server_configs, sandbox_templates)
```

This means any resource that has a `created_by` value will automatically get
`owner_user_id` set to match.

### Resources Without Ownership

Resources where `created_by` is `NULL` (e.g. system-generated or imported
resources) will have `NULL` ownership after migration. These are treated as
**built-in** resources.

The `NOT VALID` CHECK constraints enforce ownership only on **new** rows —
existing rows with `NULL` ownership are not rejected. However, if you want to
assign ownership retroactively:

```sql
-- Assign all unowned instance-scoped agent definitions to a specific user
UPDATE agent_definitions
SET owner_user_id = '<user-uuid>'
WHERE owner_user_id IS NULL
  AND owner_group_id IS NULL
  AND scope != 'built-in';

-- Or assign to a group
UPDATE agent_definitions
SET owner_group_id = '<group-uuid>'
WHERE owner_user_id IS NULL
  AND owner_group_id IS NULL
  AND scope != 'built-in';
```

After assigning ownership, you can optionally validate the constraints:

```sql
ALTER TABLE agent_definitions VALIDATE CONSTRAINT ck_agent_def_owner_or_builtin;
ALTER TABLE skill_definitions VALIDATE CONSTRAINT ck_skill_def_owner_or_builtin;
ALTER TABLE mcp_server_configs VALIDATE CONSTRAINT ck_mcp_cfg_owner_or_builtin;
ALTER TABLE sandbox_templates VALIDATE CONSTRAINT ck_sandbox_tpl_owner_or_builtin;
```

---

## 6. Docker / Deployment Changes

### docker-compose.yml

Two new environment variables were added to the `lucent` service:

```yaml
environment:
  # Secret storage (builtin provider requires an encryption key)
  LUCENT_SECRET_PROVIDER: ${LUCENT_SECRET_PROVIDER:-builtin}
  LUCENT_SECRET_KEY: ${LUCENT_SECRET_KEY:-lucent-dev-secret-key-change-in-production}
```

**For production deployments**, override the default dev key:

```bash
# Generate a strong key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Add to your .env file
echo "LUCENT_SECRET_KEY=<generated-key>" >> .env
```

### No New Volumes or Ports

The security upgrade does not require any new Docker volumes, exposed ports,
or additional containers. All data is stored in the existing PostgreSQL
database.

### Dockerfile

No changes to the `Dockerfile`. The `cryptography` library (used for Fernet
encryption) is included as a dependency in `pyproject.toml` and is installed
during the normal build.

### Production `.env` File

Add these lines to your production `.env`:

```bash
# Required for secret storage
LUCENT_SECRET_PROVIDER=builtin
LUCENT_SECRET_KEY=<your-strong-random-key>
```

> **⚠️ Important**: The `LUCENT_SECRET_KEY` is used to derive the Fernet
> encryption key via PBKDF2 (480,000 iterations). If you lose this key, all
> encrypted secrets become unrecoverable. Back it up securely.

---

## 7. Verification Checklist

Run through these steps after upgrading to confirm everything is working.

- [ ] Migrations applied successfully (037–041 in `_migrations` table)
- [ ] New tables exist (`groups`, `user_groups`, `secrets`)
- [ ] Ownership columns present on definition tables
- [ ] Ownership backfill completed for existing resources
- [ ] Task user tracking backfilled
- [ ] Secret storage working — create and retrieve a test secret via `/secrets`
- [ ] Groups page accessible at `/groups`
- [ ] Secrets page accessible at `/secrets`
- [ ] Ownership badges visible on `/definitions` and `/sandboxes`
- [ ] Existing functionality preserved (memories, API keys, sessions)
- [ ] No errors in server logs

### Step 1: Verify Migrations Applied

```bash
docker compose exec postgres psql -U lucent -d lucent -c \
  "SELECT name, applied_at FROM _migrations ORDER BY name;"
```

Confirm `037_groups.sql` through `041_secret_audit_types.sql` appear in the
output.

### Step 2: Verify Tables Exist

```bash
docker compose exec postgres psql -U lucent -d lucent -c \
  "SELECT tablename FROM pg_tables WHERE tablename IN ('groups', 'user_groups', 'secrets') ORDER BY tablename;"
```

Expected: `groups`, `secrets`, `user_groups`.

### Step 3: Verify Ownership Columns

```bash
docker compose exec postgres psql -U lucent -d lucent -c \
  "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_definitions' AND column_name LIKE 'owner%';"
```

Expected: `owner_group_id`, `owner_user_id`.

### Step 4: Verify Ownership Backfill

```bash
docker compose exec postgres psql -U lucent -d lucent -c \
  "SELECT COUNT(*) AS total, COUNT(owner_user_id) AS with_owner FROM agent_definitions WHERE scope != 'built-in';"
```

The `with_owner` count should match `total` (assuming all existing definitions
had `created_by` set).

### Step 5: Verify Task User Tracking

```bash
docker compose exec postgres psql -U lucent -d lucent -c \
  "SELECT COUNT(*) AS total, COUNT(requesting_user_id) AS with_user FROM tasks;"
```

The `with_user` count should be close to `total` (tasks whose parent request
had `created_by` set).

### Step 6: Verify Secret Storage (Optional)

Only if you plan to use secrets:

```bash
# Ensure LUCENT_SECRET_KEY is set
docker compose exec lucent env | grep LUCENT_SECRET

# Test via the Web UI — navigate to the Secrets page
# http://localhost:8766/secrets
# Create a test secret, verify it appears in the list, then delete it
```

### Step 7: Verify Groups UI

Navigate to the Lucent Web UI and confirm the **Groups** page is accessible:

```
http://localhost:8766/groups
```

You should see the groups list page (empty if no groups have been created yet).
Groups are available in all modes — no team mode requirement.

### Step 8: Verify Ownership Badges

Navigate to the Definitions page:

```
http://localhost:8766/definitions
```

Each agent definition, skill, and MCP server config should display an
**"Owner: username"** or **"Owner: group-name"** badge for non-built-in
resources. Check the Sandboxes page (`/sandboxes`) for the same badges on
sandbox templates.

### Step 9: Check Server Logs

```bash
docker compose logs lucent --tail=50 | grep -i -E 'migration|secret|error'
```

Confirm there are no errors related to migrations or secret provider
initialization.

---

## Troubleshooting

### "LUCENT_SECRET_KEY environment variable is not set"

This error appears only when you call the secrets API without setting the key.
Add `LUCENT_SECRET_KEY` to your environment or `.env` file.

### "Decryption failed — wrong key or corrupted data"

You changed `LUCENT_SECRET_KEY` after storing secrets. Secrets encrypted with
the old key cannot be decrypted with a new key. Restore the original key or
re-create the affected secrets.

### Constraint violation on new definitions

If you see a CHECK constraint error when creating a definition:

```
ck_agent_def_owner_or_builtin
```

This means the definition has `scope != 'built-in'` but neither
`owner_user_id` nor `owner_group_id` is set. Ensure the creating code passes
ownership information. This is handled automatically by the API and Web UI.

### Groups page returns 404

Verify the server has fully restarted and migrations have been applied. The
groups routes are registered unconditionally (they do not require team mode).
