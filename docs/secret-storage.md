# Secret Storage Configuration

Lucent provides a pluggable secret storage system that replaces plaintext credentials
in configuration with encrypted, access-controlled secrets. Instead of embedding API
keys and tokens directly in environment variables or MCP server configs, you store
them as named secrets and reference them with the `secret://` protocol.

This guide covers provider setup, the `secret://` reference protocol, API usage, and
migration from plaintext configuration.

---

## Provider Overview

The secret storage backend is selected via the `LUCENT_SECRET_PROVIDER` environment
variable. All providers implement a uniform interface (`get`, `set`, `delete`,
`list_keys`) and integrate with Lucent's ownership model — every secret is scoped
to an organization and owned by a user or group.

| Provider  | Status         | Backend                        |
|-----------|----------------|--------------------------------|
| `builtin` | ✅ Production  | PostgreSQL + Fernet encryption |
| `transit` | ✅ Production  | OpenBao/Vault Transit + PostgreSQL |
| `vault`   | ✅ Production  | OpenBao/Vault KV v2            |
| `aws`     | 🔧 Planned     | AWS Secrets Manager            |
| `azure`   | 🔧 Planned     | Azure Key Vault                |

### Tiered Security Model

| Tier | Provider | Key Location | Use Case |
|------|----------|-------------|----------|
| Dev | `builtin` | Same container (Fernet) | Quick local dev, no extra services |
| Local Secure | `transit` | OpenBao sidecar | Key isolation, zero external dependencies |
| Enterprise | `vault` | External Vault/OpenBao cluster | Full HSM support, centralized key management |

### Auto-Detection

When `LUCENT_SECRET_PROVIDER` is unset (or set to `auto`), Lucent probes
the environment at startup and selects the best available provider:

1. If `VAULT_ADDR` and `VAULT_TOKEN` are set and OpenBao/Vault is healthy →
   check for the Transit encryption key (`lucent-secrets`)
2. If the Transit key exists → use **transit**
3. If Vault is healthy but no Transit key → use **vault** (KV v2)
4. If Vault is unreachable or credentials are missing → fall back to **builtin**

This means new users running the default `docker-compose.yml` get Transit
encryption automatically — no configuration changes required.

---

## Built-in Provider Setup

The built-in provider is the default and the only fully implemented backend. It
encrypts secret values at rest in PostgreSQL using Fernet symmetric encryption
(AES-128 in CBC mode with HMAC-SHA256 authentication).

### Environment Variables

| Variable                | Required | Default   | Description                            |
|-------------------------|----------|-----------|----------------------------------------|
| `LUCENT_SECRET_PROVIDER`| No       | `auto`    | Secret storage backend (`auto`, `builtin`, `transit`, `vault`, `aws`, `azure`) |
| `LUCENT_SECRET_KEY`     | Conditional | —      | Encryption key (required for `builtin` provider) |

### Generate an Encryption Key

`LUCENT_SECRET_KEY` accepts any string, but it should be high-entropy (32+ characters
recommended). The system derives a Fernet key from it using PBKDF2:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set the result in your environment:

```bash
export LUCENT_SECRET_KEY="your-generated-key-here"
```

Or in your `.env` / `docker-compose.yml`:

```yaml
# docker-compose.yml
services:
  lucent:
    environment:
      LUCENT_SECRET_KEY: "your-generated-key-here"
```

> **Important:** If you lose or change `LUCENT_SECRET_KEY`, all existing secrets
> become unreadable. Back up this key securely.

### How Encryption Works

The built-in provider never stores plaintext values. The encryption pipeline:

1. **Key derivation** — `LUCENT_SECRET_KEY` is passed through PBKDF2-HMAC-SHA256
   with 480,000 iterations and a fixed salt (`lucent-secrets-v1`) to derive a
   32-byte key, which is base64-encoded into a Fernet key.

2. **Encryption** — Each secret value is encrypted with the derived Fernet key.
   Fernet provides authenticated encryption (AES-128-CBC + HMAC-SHA256), meaning
   tampered ciphertext is detected and rejected.

3. **Storage** — The encrypted bytes are stored in the `secrets.encrypted_value`
   column (BYTEA) in PostgreSQL.

4. **Decryption** — On read, the ciphertext is decrypted with the same derived
   key. A wrong key or corrupted data raises `SecretKeyError`.

```
LUCENT_SECRET_KEY (env var)
    │
    ▼
PBKDF2-HMAC-SHA256 (480k iterations, salt="lucent-secrets-v1")
    │
    ▼
32-byte derived key → base64 → Fernet key
    │
    ├──encrypt──▶ plaintext → ciphertext (stored in DB)
    └──decrypt──▶ ciphertext → plaintext (returned to caller)
```

### Ownership and Scoping

Every secret is scoped to an organization and owned by either a user or a group:

| Scope Type | Description                                          |
|------------|------------------------------------------------------|
| User-owned | `owner_user_id` set — accessible only to that user   |
| Group-owned| `owner_group_id` set — accessible to all group members |

When resolving a `secret://` reference, the system searches scopes in order:
user-owned secrets first, then group-owned secrets for each group the user belongs to.

---

## The `secret://` Protocol

The `secret://` protocol lets you reference stored secrets by name instead of
embedding plaintext values in configuration.

### Format

```
secret://key-name
```

Where `key-name` is the name you used when storing the secret via the API.

### Where It Can Be Used

| Context                     | Example                                          |
|-----------------------------|--------------------------------------------------|
| MCP server environment vars | `"OPENAI_API_KEY": "secret://openai-api-key"`    |
| Sandbox template env vars   | `"DB_PASSWORD": "secret://prod-db-password"`     |
| Integration configs         | `"api_token": "secret://slack-bot-token"`        |

### Resolution at Runtime

When Lucent encounters a `secret://` reference in an environment variable map,
the `resolve_env_vars()` function processes it:

1. **Detection** — Values starting with `secret://` are identified as references.
   All other values pass through unchanged (backward compatible).

2. **Key extraction** — The key name is parsed from the reference
   (e.g., `secret://my-key` → `my-key`).

3. **Scope search** — The system builds candidate scopes from the authenticated
   user's context:
   - First: user-owned secrets (`owner_user_id = current_user`)
   - Then: group-owned secrets (one scope per group the user belongs to)

4. **Lookup** — Each scope is queried in order. The first match is decrypted
   and returned.

5. **Error** — If no matching secret is found across any scope, a `KeyError`
   is raised with a descriptive message.

### Example: MCP Server Configuration

Store a secret:

```bash
curl -X POST https://lucent.example.com/api/secrets \
  -H "Authorization: Bearer hs_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"key": "openai-api-key", "value": "sk-proj-abc123..."}'
```

Reference it in an MCP server config:

```json
{
  "name": "my-mcp-server",
  "env_vars": {
    "OPENAI_API_KEY": "secret://openai-api-key",
    "LOG_LEVEL": "info"
  }
}
```

At runtime, `OPENAI_API_KEY` is resolved to the decrypted value. `LOG_LEVEL`
passes through as-is.

---

## OpenBao Transit Provider (Recommended)

The Transit provider delegates all encryption and decryption to
[OpenBao](https://openbao.org/)'s Transit secrets engine. Lucent sends
plaintext to OpenBao, which encrypts it with a key that Lucent never sees,
and returns ciphertext that is stored in PostgreSQL.

This is more secure than the builtin provider because the encryption key
is isolated in a separate process. Even if both the Lucent container and the
database are compromised, an attacker cannot decrypt secrets without access to
OpenBao's Transit key.

```
┌──────────────┐   1. plaintext   ┌───────────────┐
│ Lucent Server │ ──────────────► │   OpenBao      │
│ (no key)      │                  │ Transit Engine │
│               │ ◄────────────── │ (holds key)    │
│               │   2. ciphertext  └───────────────┘
│               │
│               │   3. store ciphertext
│               │ ──────────────► ┌───────────────┐
│               │                  │  PostgreSQL    │
└──────────────┘                  │  secrets table │
                                   └───────────────┘
```

On read, the flow reverses: Lucent fetches ciphertext from PostgreSQL, sends
it to OpenBao for decryption, and returns the plaintext to the caller.

### Why Transit over KV?

The `vault` (KV v2) provider stores secret values directly in Vault's
key-value store. This is fine for centralized secret management, but it means
Lucent receives the plaintext value from Vault and must handle it in memory.

With Transit, Lucent **never holds the encryption key**. The key stays inside
OpenBao's process memory and is never exposed via API. This is the
strongest isolation model available without hardware security modules.

### Setup (Docker Compose)

OpenBao is included in the default `docker-compose.yml` and starts
automatically. No additional setup is required.

The included init script (`docker/openbao-init.sh`) configures:
- KV v2 engine at `secret/`
- Transit engine at `transit/`
- A `lucent-secrets` Transit encryption key
- A scoped `lucent-policy` with least-privilege permissions

### Environment Variables

| Variable              | Required | Default | Description                              |
|-----------------------|----------|---------|------------------------------------------|
| `LUCENT_SECRET_PROVIDER` | No    | `auto`  | Set to `transit` to force Transit, or leave as `auto` |
| `VAULT_ADDR`          | Yes      | `http://openbao:8200` (in docker-compose) | OpenBao/Vault API URL |
| `VAULT_TOKEN`         | Yes      | `root` (dev mode)   | Token with Transit encrypt/decrypt permissions |
| `VAULT_TRANSIT_MOUNT` | No       | `transit` | Transit engine mount path                |
| `VAULT_TRANSIT_KEY`   | No       | `lucent-secrets` | Name of the Transit encryption key    |

### Example: Explicit Transit Configuration

If you want to force Transit without auto-detection:

```bash
export LUCENT_SECRET_PROVIDER=transit
export VAULT_ADDR=http://openbao:8200
export VAULT_TOKEN=your-token-here
```

Or in `docker-compose.yml` (already configured by default):

```yaml
services:
  lucent:
    environment:
      LUCENT_SECRET_PROVIDER: transit
      VAULT_ADDR: http://openbao:8200
      VAULT_TOKEN: ${VAULT_TOKEN:-root}
```

---

## HashiCorp Vault / OpenBao KV v2 Provider

The `vault` provider stores secrets directly in Vault's KV v2 secrets engine.
Use this when connecting to an external Vault or OpenBao cluster where you
want centralized secret management rather than local Transit encryption.

> OpenBao is API-compatible with HashiCorp Vault — the same provider works
> with both.

### Environment Variables

| Variable              | Required | Default | Description                              |
|-----------------------|----------|---------|------------------------------------------|
| `LUCENT_SECRET_PROVIDER` | Yes   | —       | Set to `vault`                           |
| `VAULT_ADDR`          | Yes      | —       | Vault/OpenBao API base URL               |
| `VAULT_TOKEN`         | Yes      | —       | Token with read/write access to the KV mount |
| `VAULT_KV_MOUNT`      | No       | `secret` | KV v2 mount path                       |

### Example Setup

```bash
export LUCENT_SECRET_PROVIDER=vault
export VAULT_ADDR=https://vault.example.com
export VAULT_TOKEN=hvs.your-vault-token
```

The provider maps secrets to KV v2 paths:
`{mount}/data/lucent/{org_id}/{user|group}/{owner_id}/{key}`

---

## AWS Secrets Manager Configuration

> **Status: Planned.** The AWS provider validates credentials at startup but
> all operations currently raise `NotImplementedError`.

### Environment Variables

| Variable                    | Required        | Default | Description                        |
|-----------------------------|-----------------|---------|-----------------------------------|
| `LUCENT_SECRET_PROVIDER`    | Yes             | —       | Set to `aws`                      |
| `AWS_REGION`                | Yes (or `AWS_DEFAULT_REGION`) | — | AWS region                |
| `AWS_ACCESS_KEY_ID`         | Conditional†    | —       | AWS access key                    |
| `AWS_SECRET_ACCESS_KEY`     | Conditional†    | —       | AWS secret key                    |
| `AWS_PROFILE`               | Conditional†    | —       | AWS CLI profile name              |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | Conditional† | —       | IRSA token file (for EKS)        |
| `AWS_SESSION_TOKEN`         | No              | —       | Temporary session token (planned) |

†At least one credential method is required: explicit keys (`AWS_ACCESS_KEY_ID` +
`AWS_SECRET_ACCESS_KEY`), a named profile (`AWS_PROFILE`), or web identity
(`AWS_WEB_IDENTITY_TOKEN_FILE` for IAM Roles for Service Accounts on EKS).

### Example Setup

**With explicit credentials:**

```bash
export LUCENT_SECRET_PROVIDER=aws
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

**With IAM role (recommended for EC2/ECS/EKS):**

```bash
export LUCENT_SECRET_PROVIDER=aws
export AWS_REGION=us-east-1
# Credentials are provided by the instance/task/pod role — no keys needed
export AWS_WEB_IDENTITY_TOKEN_FILE=/var/run/secrets/eks.amazonaws.com/serviceaccount/token
```

---

## Azure Key Vault Configuration

> **Status: Planned.** The Azure provider validates credentials at startup but
> all operations currently raise `NotImplementedError`.

### Environment Variables

| Variable                       | Required     | Default | Description                       |
|--------------------------------|-------------|---------|-----------------------------------|
| `LUCENT_SECRET_PROVIDER`       | Yes          | —       | Set to `azure`                   |
| `AZURE_KEY_VAULT_URL`          | Yes          | —       | Key Vault URL (e.g., `https://my-vault.vault.azure.net`) |
| `AZURE_TENANT_ID`             | Conditional† | —       | Azure AD tenant ID                |
| `AZURE_CLIENT_ID`             | Conditional† | —       | Service principal client ID       |
| `AZURE_CLIENT_SECRET`         | Conditional† | —       | Service principal client secret   |
| `AZURE_CLIENT_CERTIFICATE_PATH`| Conditional† | —      | Certificate auth (alternative)    |
| `AZURE_FEDERATED_TOKEN_FILE`  | Conditional† | —       | Workload identity (for AKS)      |

†At least one credential method is required: service principal credentials
(`AZURE_TENANT_ID` + `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET`), certificate-based
auth (`AZURE_CLIENT_CERTIFICATE_PATH`), or workload identity federation
(`AZURE_FEDERATED_TOKEN_FILE`).

### Example Setup

**With service principal:**

```bash
export LUCENT_SECRET_PROVIDER=azure
export AZURE_KEY_VAULT_URL=https://my-vault.vault.azure.net
export AZURE_TENANT_ID=your-tenant-id
export AZURE_CLIENT_ID=your-client-id
export AZURE_CLIENT_SECRET=your-client-secret
```

---

## Migrating from Plaintext Environment Variables

Lucent provides an admin-only API endpoint that automatically migrates plaintext
credentials in MCP server configs, sandbox templates, and integrations to
`secret://` references.

### Automatic Migration

Call the migration endpoint (requires `admin` role):

```bash
curl -X POST https://lucent.example.com/api/secrets/migrate-plaintext-configs \
  -H "Authorization: Bearer hs_your_admin_api_key"
```

The endpoint scans all configurations in your organization and:

1. **Identifies sensitive keys** — Environment variables with names containing
   `token`, `secret`, `password`, `api_key`, `apikey`, `client_secret`,
   `private_key`, `access_key`, `auth`, or `passwd` are flagged as sensitive.

2. **Stores values as secrets** — Each sensitive value is encrypted and stored
   with a deterministic name:
   - MCP configs: `mcp.<config-id>.<key-name>`
   - Sandbox templates: `sandbox.<template-id>.<key-name>`
   - Integrations: `integration.<integration-id>.<key-name>`

3. **Replaces plaintext with references** — The original value is replaced with
   a `secret://<secret-name>` reference in the configuration.

4. **Skips already-migrated values** — Values that already start with `secret://`
   are left untouched.

**Response:**

```json
{
  "migrated_mcp_env_vars": 3,
  "migrated_sandbox_env_vars": 1,
  "migrated_integration_values": 2
}
```

### Manual Migration

For individual secrets or non-sensitive keys not caught by auto-detection:

1. **Store the secret:**

   ```bash
   curl -X POST https://lucent.example.com/api/secrets \
     -H "Authorization: Bearer hs_your_api_key" \
     -H "Content-Type: application/json" \
     -d '{"key": "my-api-key", "value": "the-actual-secret-value"}'
   ```

2. **Update the configuration** to use `secret://my-api-key` in place of the
   plaintext value.

3. **Verify** the secret resolves correctly by retrieving it:

   ```bash
   curl https://lucent.example.com/api/secrets/my-api-key \
     -H "Authorization: Bearer hs_your_api_key"
   ```

4. **Remove the plaintext value** from any `.env` files, CI/CD variables, or
   other locations where it was previously stored.

---

## API Reference

All secret endpoints require authentication. Secret values are **never** included
in list or create responses — only in explicit `GET` requests. Every operation is
recorded in the audit trail.

**Base path:** `/api/secrets`

### Create a Secret

```
POST /api/secrets
```

**Request Body:**

| Field            | Type   | Required | Description                              |
|------------------|--------|----------|------------------------------------------|
| `key`            | string | Yes      | Secret name (1–256 characters)           |
| `value`          | string | Yes      | Secret value (never returned)            |
| `owner_group_id` | string | No       | Group owner (defaults to current user)   |

**Example:**

```bash
curl -X POST https://lucent.example.com/api/secrets \
  -H "Authorization: Bearer hs_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"key": "github-token", "value": "ghp_abc123..."}'
```

**Response (201):**

```json
{
  "key": "github-token",
  "owner_user_id": "550e8400-e29b-41d4-a716-446655440000",
  "owner_group_id": null,
  "created_at": null,
  "updated_at": null
}
```

Calling `POST` again with the same key performs an upsert — the existing value is
replaced.

### List Secrets

```
GET /api/secrets
GET /api/secrets?owner_group_id={group_id}
```

Returns key names only (no values) for the current user or a specified group.

**Example:**

```bash
curl https://lucent.example.com/api/secrets \
  -H "Authorization: Bearer hs_your_api_key"
```

**Response (200):**

```json
{
  "keys": [
    {
      "key": "github-token",
      "owner_user_id": "550e8400-e29b-41d4-a716-446655440000",
      "owner_group_id": null
    },
    {
      "key": "openai-api-key",
      "owner_user_id": "550e8400-e29b-41d4-a716-446655440000",
      "owner_group_id": null
    }
  ]
}
```

### Get a Secret Value

```
GET /api/secrets/{key}
GET /api/secrets/{key}?owner_group_id={group_id}
```

Retrieves the decrypted secret value. Requires explicit access authorization via
the ACL — the operation is audit-logged.

**Example:**

```bash
curl https://lucent.example.com/api/secrets/github-token \
  -H "Authorization: Bearer hs_your_api_key"
```

**Response (200):**

```json
{
  "key": "github-token",
  "value": "ghp_abc123..."
}
```

**Error Responses:**

| Status | Description                              |
|--------|------------------------------------------|
| 403    | Access denied (not owner/group member/admin) |
| 404    | Secret not found                         |

### Delete a Secret

```
DELETE /api/secrets/{key}
DELETE /api/secrets/{key}?owner_group_id={group_id}
```

Deletes a secret. Requires modify access (owner, admin, or org owner).

**Example:**

```bash
curl -X DELETE https://lucent.example.com/api/secrets/github-token \
  -H "Authorization: Bearer hs_your_api_key"
```

**Response (200):**

```json
{
  "deleted": true,
  "key": "github-token"
}
```

### Migrate Plaintext Configs

```
POST /api/secrets/migrate-plaintext-configs
```

Admin-only endpoint. See [Migrating from Plaintext Environment Variables](#migrating-from-plaintext-environment-variables)
for details.

**Response (200):**

```json
{
  "migrated_mcp_env_vars": 3,
  "migrated_sandbox_env_vars": 1,
  "migrated_integration_values": 2
}
```

---

## Migrating from Builtin to Transit

If you have existing secrets encrypted with the builtin (Fernet) provider and
want to switch to Transit, use the included migration script. It re-encrypts
each secret through OpenBao's Transit engine in place.

### Prerequisites

- OpenBao running and initialized (the default `docker-compose.yml` handles this)
- The original `LUCENT_SECRET_KEY` used to encrypt existing secrets
- Database access (`DATABASE_URL`)

### Run the Migration

```bash
python scripts/migrate_secrets_to_transit.py \
  --database-url "$DATABASE_URL" \
  --vault-addr "$VAULT_ADDR" \
  --vault-token "$VAULT_TOKEN" \
  --secret-key "$LUCENT_SECRET_KEY"
```

### Dry Run

Preview what would be migrated without making changes:

```bash
python scripts/migrate_secrets_to_transit.py --dry-run \
  --database-url "$DATABASE_URL" \
  --vault-addr "$VAULT_ADDR" \
  --vault-token "$VAULT_TOKEN" \
  --secret-key "$LUCENT_SECRET_KEY"
```

### How It Works

1. Connects to PostgreSQL and reads all rows from the `secrets` table
2. Skips rows already encrypted with Transit (ciphertext starts with `vault:v1:`)
3. Decrypts each Fernet-encrypted value using `LUCENT_SECRET_KEY`
4. Re-encrypts via OpenBao's Transit engine
5. Updates the `encrypted_value` column with Transit ciphertext
6. Each row is updated in its own transaction for safety

The script is **idempotent** — running it again skips already-migrated secrets.
After migration, set `LUCENT_SECRET_PROVIDER=transit` (or leave it as `auto`)
and you can remove `LUCENT_SECRET_KEY` from your environment.

### CLI Options

| Flag | Env Var | Description |
|------|---------|-------------|
| `--database-url` | `DATABASE_URL` | PostgreSQL connection string |
| `--vault-addr` | `VAULT_ADDR` | OpenBao/Vault API URL |
| `--vault-token` | `VAULT_TOKEN` | OpenBao/Vault token |
| `--secret-key` | `LUCENT_SECRET_KEY` | Fernet key for decrypting existing secrets |
| `--transit-mount` | — | Transit engine mount (default: `transit`) |
| `--transit-key` | — | Transit key name (default: `lucent-secrets`) |
| `--dry-run` | — | Preview only, no database writes |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `SecretKeyError: LUCENT_SECRET_KEY environment variable is not set` | Missing encryption key | Set `LUCENT_SECRET_KEY` in your environment |
| `SecretKeyError: Decryption failed — wrong key or corrupted data` | Key was changed or data corrupted | Restore the original `LUCENT_SECRET_KEY` value |
| `KeyError: Secret not found (reference 'my-key')` | Secret doesn't exist or user lacks access | Verify the secret exists and the user/group ownership matches |
| `Invalid LUCENT_SECRET_PROVIDER` | Typo in provider name | Use one of: `builtin`, `transit`, `vault`, `aws`, `azure` |
| `NotImplementedError` from aws/azure | Provider not yet implemented | Switch to `LUCENT_SECRET_PROVIDER=builtin` or `transit` |
| Transit encrypt/decrypt fails | OpenBao unreachable or wrong token | Check `VAULT_ADDR` connectivity and `VAULT_TOKEN` permissions |
| Auto-detect picks builtin unexpectedly | OpenBao not running or not healthy | Run `docker compose up openbao` and verify health at `VAULT_ADDR/v1/sys/health` |
