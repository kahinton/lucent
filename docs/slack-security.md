# Slack Integration — Security Considerations

This document covers the security architecture of the Slack integration, including credential handling, webhook verification, identity resolution, and audit logging.

---

## Credential Encryption

### At-Rest Encryption

All platform credentials (bot tokens, signing secrets) are encrypted with **Fernet (AES-128-CBC + HMAC-SHA256)** before storage. The `encrypted_config` column stores opaque ciphertext bytes — plaintext credentials never exist in the database.

The encryption key is read from the `LUCENT_CREDENTIAL_KEY` environment variable (with `LUCENT_ENCRYPTION_KEY` as a legacy fallback).

### Key Generation

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

This produces a 32-byte URL-safe base64 key suitable for Fernet.

### Key Rotation

The `FernetEncryptor` supports seamless key rotation via `MultiFernet`:

1. Generate a new key
2. Set the new key as `LUCENT_CREDENTIAL_KEY`
3. Keep the old key available during the transition
4. Re-encrypt existing integrations by updating their config (triggers re-encryption with the new key)

During rotation, decryption tries the new key first, then falls back to the old key.

### What's Encrypted

| Data | Storage | Encryption |
|------|---------|------------|
| Slack bot token (`xoxb-...`) | `integrations.encrypted_config` | Fernet (BYTEA) |
| Slack signing secret | `integrations.encrypted_config` | Fernet (BYTEA) |
| Pairing codes | `pairing_challenges.code_hash` | bcrypt (not Fernet) |

### What's NOT Encrypted

- Channel IDs (stored as JSONB in `allowed_channels`)
- External user IDs and workspace IDs (needed for lookup queries)
- Integration status and metadata

---

## Webhook Signature Verification

### How It Works

Slack signs every webhook request with HMAC-SHA256 using your app's signing secret. Lucent verifies this signature **before** parsing any payload data.

The verification flow:

1. `WebhookSignatureMiddleware` intercepts requests to `/integrations/webhook/slack`
2. Raw body is buffered **before** any JSON parsing
3. The `SlackAdapter.verify_signature()` method checks:
   - `X-Slack-Request-Timestamp` is within 5 minutes (replay attack protection)
   - `X-Slack-Signature` matches HMAC-SHA256 of `v0:{timestamp}:{body}` using the signing secret
   - Comparison uses `hmac.compare_digest()` (constant-time) to prevent timing attacks

### Why Pre-Parse Verification Matters

The middleware reads the raw request body **before** any framework-level parsing. This prevents:

- **Deserialization attacks**: Malformed JSON payloads are rejected before parsing
- **Body stream consumption**: The raw body is cached and replayed for downstream handlers
- **ContextVar pollution**: Uses pure ASGI (not BaseHTTPMiddleware) to preserve async context

### Failed Verification

Invalid signatures return `401` with a generic error message. The following details are logged but **not** returned to the caller:

- Client IP address
- Request path
- Timestamp
- Platform identifier

A `signature_verification_failed` audit event is recorded.

---

## Identity Resolution

### The Security Model

External platform users are **untrusted by default**. A Slack user ID (`U0123...`) has no privileges in Lucent until explicitly linked to a Lucent account through one of:

1. **Pairing code flow** — User proves ownership of both accounts
2. **Admin linking** — Admin explicitly maps external → Lucent identity
3. **OAuth** (future) — Platform-provided identity verification

### Pairing Code Security

| Property | Implementation |
|----------|---------------|
| Code entropy | 128 bits (`secrets.token_urlsafe(16)`) |
| Storage | bcrypt hash only — plaintext is shown once and never stored |
| Expiry | 10-minute TTL enforced at verification time |
| Attempt limit | 5 per code, then auto-exhausted |
| Rate limit | 10 codes per user per hour |
| Lookup | bcrypt prevents direct hash lookup — verification scans pending challenges |

### Why bcrypt?

Pairing codes are short-lived secrets. bcrypt hashing prevents offline brute-force if the database is compromised. Since codes expire in 10 minutes, the slow hashing is actually a feature — it rate-limits brute-force attempts naturally.

### Link Lifecycle

```
pending → active → revoked
                 → superseded (when re-linked)
                 → orphaned (when integration disabled)
                 → disabled
```

Only `active` links are used for identity resolution. Superseded and revoked links are preserved for audit purposes.

### One Active Link Per Identity

The system enforces that each external identity tuple (provider + external_user_id + workspace) maps to at most one active link. Re-linking automatically supersedes the old link.

---

## Pipeline Security

When a message arrives from Slack, it passes through a 9-step security pipeline:

| Step | Check | Failure Response |
|------|-------|-----------------|
| 0 | Webhook signature verification (middleware) | `401 Invalid signature` |
| 1 | Channel allowlist | Ephemeral: "This channel isn't configured for Lucent commands" |
| 2 | Identity resolution | Ephemeral: "Your account isn't linked..." |
| 3 | RBAC permission check | Ephemeral: "You don't have permission..." |
| 4 | Rate limit (unified per-user) | Ephemeral: "You're sending requests too quickly..." |
| 5 | Input sanitization | (Empty input filtered) |
| 6 | `set_current_user` ContextVar | — |
| 7 | MCP tool dispatch | — |
| 8 | Response formatting | — |
| 9 | Send via adapter | — |

### Error Message Philosophy

User-facing error messages are intentionally vague. They never reveal:

- Whether a specific user exists
- Why identity resolution failed
- Internal error details or stack traces
- Rate limit thresholds or remaining quota

Detailed information is available only in server logs and audit events.

---

## Input Sanitization

All user input from integration channels is sanitized before processing:

- **Truncated** to 4000 characters
- **Control characters stripped** (null bytes, non-printable chars) while preserving newlines and tabs
- **Whitespace trimmed**

This prevents oversized payloads, null-byte injection, and control character attacks.

---

## Rate Limiting

Integration requests share the unified per-user rate limit (default: 100 requests/minute). The rate limit key is the **Lucent user ID** (not the external platform user ID), so a user's rate limit is shared across API, MCP, and integration channels.

Rate limit headers are not returned on integration responses (Slack doesn't support them). Instead, the bot sends an ephemeral "too many requests" message and logs an `integration_rate_limited` audit event.

---

## RBAC

Integration access requires the `MEMORY_CREATE` permission (granted to all roles: member, admin, owner). The `MANAGE_INTEGRATIONS` permission (admin + owner only) is required for:

- Creating, updating, and deleting integrations
- Managing user links (list, create, revoke)
- Viewing integration audit events

Regular users can only:
- Generate pairing codes for themselves
- Verify pairing codes
- Use the integration in allowed channels

---

## Audit Trail

Every integration operation generates audit events stored in the `audit_logs` table. Events include:

- **Who**: Lucent user ID (when resolved) or external user ID (when unresolved)
- **What**: Event type and contextual details
- **When**: Timestamp
- **Where**: Platform, channel, integration ID

Security events use dedicated `action_type` values for easier filtering. Operational events use `action_type = 'integration_event'` with the specific event type in the JSONB `context` field.

Audit logs are accessible to admins via:

```
GET /api/audit/organization
GET /api/audit/recent
```

### Events to Monitor

For security monitoring, pay attention to:

| Event | Concern |
|-------|---------|
| `signature_verification_failed` (repeated) | Possible webhook endpoint probing |
| `challenge_failed` (repeated for same user) | Possible pairing code brute-force |
| `resolution_failed` (high volume) | Unauthorized users trying to use the bot |
| `integration_rate_limited` (frequent) | Possible abuse via integration channel |
| `channel_not_allowed` (repeated) | Users attempting to use bot in unauthorized channels |

---

## Network Security

### Webhook Endpoint Exposure

The webhook endpoint (`/integrations/webhook/slack`) must be publicly reachable from Slack's servers. Recommendations:

- Use HTTPS (TLS termination at reverse proxy)
- Rely on signature verification rather than IP allowlisting (Slack's IP ranges change)
- Set `LUCENT_CORS_ORIGINS` appropriately (webhooks don't use CORS, but the admin API does)

### Internal Communication

The integration service dispatches to the MCP tool pipeline over the internal loopback (`LUCENT_MCP_URL`, default `http://localhost:8766/mcp`). This traffic stays on the server and does not traverse the network.

---

## Encryption Key Management Checklist

- [ ] `LUCENT_CREDENTIAL_KEY` is set and backed up securely
- [ ] Key is not committed to source control or Docker images
- [ ] Key is rotated periodically (recommended: quarterly)
- [ ] Old keys are preserved during rotation for re-encryption
- [ ] Backup encryption keys are stored separately from database backups

---

## Related Docs

- [Admin Setup Guide](slack-admin-setup.md) — Integration configuration
- [User Linking Guide](slack-user-linking.md) — Pairing code flow
- [Integrations API Reference](integrations-api-reference.md) — Endpoint documentation
- [Deployment Guide](deployment-guide.md) — Server configuration
