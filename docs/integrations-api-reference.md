# Integrations API Reference

REST API endpoints for managing platform integrations, user identity links, and pairing challenges.

Admin endpoints require the `MANAGE_INTEGRATIONS` permission (admin or owner role). Pairing endpoints are available to any authenticated user.

Interactive API docs are also available at `/api/docs` (Swagger UI) when the server is running.

---

## Webhooks

### Receive Webhook

```
POST /integrations/webhook/{provider}
```

Inbound webhook from a platform (Slack, Discord). **No API key authentication** — webhook signature verification is the authentication mechanism.

Returns `200` immediately and processes the event asynchronously.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `provider` | string | Platform identifier: `slack` or `discord` |

**Slack URL Verification:** When Slack sends a `url_verification` challenge, the endpoint responds synchronously with `{"challenge": "..."}`.

**Normal Response:** `200`
```json
{"status": "accepted"}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `401` | Invalid webhook signature |
| `404` | Unknown provider or no adapter configured |

---

## Integrations (Admin)

Base path: `/api/v1/integrations`

All endpoints require admin or owner role.

### Create Integration

```
POST /api/v1/integrations
```

Register a new platform integration for your organization. Only one active integration per type + workspace is allowed.

**Request Body:**

```json
{
  "type": "slack",
  "external_workspace_id": "T0123ABCDEF",
  "config": {
    "bot_token": "xoxb-...",
    "signing_secret": "..."
  },
  "allowed_channels": ["C0123ABCDEF"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `"slack"` or `"discord"` |
| `external_workspace_id` | string | no | Platform workspace/guild ID |
| `config` | object | yes | Platform credentials — encrypted at rest, never returned by the API |
| `allowed_channels` | string[] | no | Channel IDs to restrict usage. Empty = all channels. |

**Slack config fields:**

| Key | Description |
|-----|-------------|
| `bot_token` | Bot User OAuth Token (`xoxb-...`) |
| `signing_secret` | App signing secret for webhook verification |

**Response:** `201`

```json
{
  "id": "uuid",
  "organization_id": "uuid",
  "type": "slack",
  "status": "active",
  "external_workspace_id": "T0123ABCDEF",
  "allowed_channels": ["C0123ABCDEF"],
  "config_version": 1,
  "created_by": "uuid",
  "updated_by": null,
  "created_at": "2026-03-18T19:30:00Z",
  "updated_at": "2026-03-18T19:30:00Z",
  "disabled_at": null,
  "revoked_at": null
}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `400` | User has no organization |
| `409` | Active integration of same type+workspace already exists |
| `500` | Credential encryption not configured (`LUCENT_CREDENTIAL_KEY` missing) |

---

### List Integrations

```
GET /api/v1/integrations
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `integration_status` | string | all | Filter by status: `active`, `disabled`, `revoked`, `deleted` |
| `limit` | int | 50 | Max results (capped at 100) |

**Response:** `200`

```json
{
  "integrations": [
    { "id": "...", "type": "slack", "status": "active", "..." }
  ],
  "total_count": 1
}
```

---

### Get Integration

```
GET /api/v1/integrations/{integration_id}
```

Returns a single integration. Scoped to the caller's organization.

**Response:** `200` — `IntegrationResponse` object (see Create response).

---

### Update Integration

```
PATCH /api/v1/integrations/{integration_id}
```

Update credentials, channels, and/or status. All fields are optional.

**Request Body:**

```json
{
  "status": "disabled",
  "allowed_channels": ["C0123ABCDEF"],
  "config": {
    "bot_token": "xoxb-new-token",
    "signing_secret": "new-secret"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Transition to: `active`, `disabled`, `revoked`, `deleted` |
| `allowed_channels` | string[] | Replaces existing allowlist entirely |
| `config` | object | New credentials (re-encrypted at rest) |

**Status Transitions:**

```
active → disabled, revoked, deleted
disabled → active, deleted
revoked → deleted
```

Invalid transitions return `409`.

> **Side effect:** Disabling or revoking an integration orphans all active user links.

**Response:** `200` — Updated `IntegrationResponse`.

---

### Delete Integration

```
DELETE /api/v1/integrations/{integration_id}
```

Soft-delete the integration. Orphans all active user links.

**Response:** `200`

```json
{"id": "uuid", "status": "deleted"}
```

---

## User Links (Admin)

### List User Links

```
GET /api/v1/integrations/links
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `integration_id` | UUID | Filter by integration |
| `link_status` | string | Filter by status: `pending`, `active`, `revoked`, `superseded`, `orphaned`, `disabled` |
| `limit` | int | Max results (default 50, max 100) |

**Response:** `200`

```json
{
  "links": [
    {
      "id": "uuid",
      "organization_id": "uuid",
      "integration_id": "uuid",
      "user_id": "uuid",
      "provider": "slack",
      "external_user_id": "U0123SLACK",
      "external_workspace_id": "T0123ABCDEF",
      "status": "active",
      "verification_method": "pairing_code",
      "linked_at": "2026-03-18T19:35:00Z",
      "created_at": "2026-03-18T19:30:00Z",
      "updated_at": "2026-03-18T19:35:00Z"
    }
  ],
  "total_count": 1
}
```

---

### Create User Link (Admin)

```
POST /api/v1/integrations/links
```

Directly link a Lucent user to a platform identity, bypassing the pairing code flow. The link is automatically activated.

**Request Body:**

```json
{
  "integration_id": "uuid",
  "user_id": "lucent-user-uuid",
  "external_user_id": "U0123SLACK",
  "external_workspace_id": "T0123ABCDEF"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `integration_id` | UUID | yes | Target integration |
| `user_id` | UUID | yes | Lucent user to link |
| `external_user_id` | string | yes | Platform user ID |
| `external_workspace_id` | string | no | Platform workspace/guild ID |

**Response:** `201` — `UserLinkResponse` with `status: "active"` and `verification_method: "admin"`.

---

### Revoke User Link

```
DELETE /api/v1/integrations/links/{link_id}
```

Revokes an active user link. The link record is preserved for audit purposes.

**Response:** `200`

```json
{"id": "uuid", "status": "revoked"}
```

---

## Pairing (Authenticated Users)

These endpoints are available to any authenticated user.

### Generate Pairing Code

```
POST /api/v1/integrations/link
```

Generate a one-time pairing code for the current user.

**Request Body:**

```json
{
  "integration_id": "uuid"
}
```

**Response:** `201`

```json
{
  "id": "challenge-uuid",
  "integration_id": "uuid",
  "user_id": "your-uuid",
  "code": "Abc1Def2Ghi3Jkl4Mnop5Q",
  "expires_at": "2026-03-18T19:40:00Z",
  "status": "pending",
  "created_at": "2026-03-18T19:30:00Z"
}
```

> The `code` field is only present in this response. It is never stored in plaintext.

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `404` | Integration not found in user's organization |
| `409` | Integration is not active |
| `429` | Rate limit exceeded (10 codes per user per hour) |

---

### Verify Pairing Code

```
POST /api/v1/integrations/verify
```

Validate a pairing code and activate the identity link.

**Request Body:**

```json
{
  "code": "Abc1Def2Ghi3Jkl4Mnop5Q",
  "integration_id": "uuid",
  "external_user_id": "U0123SLACK",
  "provider": "slack"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | yes | The plaintext pairing code |
| `integration_id` | string | yes | Integration the code was generated for |
| `external_user_id` | string | no | Platform user ID (defaults to current user ID) |
| `provider` | string | no | Defaults to the integration's platform type |

**Response:** `200`

```json
{"linked": true, "provider": "slack"}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `400` | Invalid or expired pairing code |
| `404` | Integration not found |
| `422` | Missing `code` or `integration_id` |

---

## Response Models

### IntegrationResponse

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Integration ID |
| `organization_id` | UUID | Owning organization |
| `type` | string | `slack` or `discord` |
| `status` | string | `active`, `disabled`, `revoked`, `deleted` |
| `external_workspace_id` | string? | Platform workspace/guild ID |
| `allowed_channels` | string[] | Channel allowlist (empty = all) |
| `config_version` | int | Increments on config update |
| `created_by` | UUID | User who created the integration |
| `updated_by` | UUID? | User who last updated |
| `created_at` | datetime | Creation timestamp |
| `updated_at` | datetime | Last update timestamp |
| `disabled_at` | datetime? | When disabled (if applicable) |
| `revoked_at` | datetime? | When revoked (if applicable) |

### UserLinkResponse

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Link ID |
| `organization_id` | UUID | Organization |
| `integration_id` | UUID | Parent integration |
| `user_id` | UUID | Lucent user |
| `provider` | string | Platform type |
| `external_user_id` | string | Platform user ID |
| `external_workspace_id` | string? | Platform workspace ID |
| `status` | string | `pending`, `active`, `revoked`, `superseded`, `orphaned`, `disabled` |
| `verification_method` | string | `pairing_code`, `admin`, `oauth` |
| `linked_at` | datetime? | When the link was activated |
| `created_at` | datetime | Creation timestamp |
| `updated_at` | datetime | Last update timestamp |

### PairingChallengeResponse

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Challenge ID |
| `integration_id` | UUID | Target integration |
| `user_id` | UUID | Lucent user |
| `code` | string? | Plaintext code (only in create response) |
| `expires_at` | datetime | When the code expires |
| `status` | string | `pending`, `used`, `expired`, `exhausted` |
| `created_at` | datetime | Creation timestamp |

---

## Audit Events

All integration operations generate audit events. Admins can view them via the existing audit log endpoints.

### Security Events (dedicated action types)

| Event | Trigger |
|-------|---------|
| `signature_verification_failed` | Webhook with invalid HMAC signature |
| `channel_not_allowed` | Message from a non-allowlisted channel |
| `challenge_failed` | Pairing code verification failure |
| `resolution_failed` | Unlinked user attempted to use integration |
| `integration_rate_limited` | User exceeded rate limit via integration |
| `integration_revoked` | Integration credentials revoked |
| `link_revoked` | User link revoked by admin |

### Operational Events (action type: `integration_event`)

| Event | Trigger |
|-------|---------|
| `integration_created` | New integration registered |
| `integration_updated` | Config or channels modified |
| `integration_disabled` | Integration disabled |
| `challenge_issued` | Pairing code generated |
| `challenge_succeeded` | Pairing code verified |
| `link_activated` | User link activated |
| `link_superseded` | Old link replaced by new link |
| `link_orphaned` | Link orphaned due to integration disable |
| `link_disabled` | Link manually disabled |
| `identity_resolved` | External user resolved to Lucent user |
| `integration_command` | Command processed through integration |

---

## Related Docs

- [Admin Setup Guide](slack-admin-setup.md) — Creating and configuring integrations
- [User Linking Guide](slack-user-linking.md) — Pairing code flow for end users
- [Security Considerations](slack-security.md) — Encryption, signatures, audit
