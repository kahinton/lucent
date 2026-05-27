# Slack Integration — Admin Setup Guide

This guide walks administrators through creating a Slack app, configuring it in Lucent, and managing channel access.

## Prerequisites

- Lucent running in **team mode** (`LUCENT_MODE=team`)
- An **admin** or **owner** role in your Lucent organization
- A Slack workspace where you can create apps
- The `LUCENT_CREDENTIAL_KEY` environment variable set on the Lucent server (see [Credential Encryption](#credential-encryption))

---

## Step 1: Create a Slack App

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**.
3. Name the app (e.g., "Lucent") and select your workspace.
4. Click **Create App**.

### Configure Bot Token Scopes

1. Navigate to **OAuth & Permissions** in the left sidebar.
2. Under **Bot Token Scopes**, add these scopes:

| Scope | Purpose |
|-------|---------|
| `chat:write` | Send messages and responses |
| `chat:write.public` | Send messages to channels the bot hasn't joined |
| `app_mentions:read` | Respond when @mentioned |
| `commands` | Handle slash commands |
| `im:read` | Read DMs (for pairing code flow) |
| `im:write` | Send DMs (for pairing code flow) |
| `im:history` | Read DM history (for pairing code flow) |

### Enable Event Subscriptions

1. Navigate to **Event Subscriptions** and toggle **Enable Events** on.
2. Set the **Request URL** to:

```
https://your-lucent-host/integrations/webhook/slack
```

Slack will send a verification challenge to this URL. Lucent handles the `url_verification` event automatically — you should see a green checkmark.

3. Under **Subscribe to bot events**, add:
   - `message.im` — DM messages (for pairing codes)
   - `app_mention` — @mentions in channels

### Install the App

1. Navigate to **Install App** in the left sidebar.
2. Click **Install to Workspace** and authorize.
3. Copy the **Bot User OAuth Token** (starts with `xoxb-`).
4. Go back to **Basic Information** and copy the **Signing Secret**.

> **Keep these values safe.** You'll need them for the next step. Never commit them to source control.

---

## Step 2: Configure the Integration in Lucent

### Set Up Credential Encryption

Before storing any Slack credentials, ensure the `LUCENT_CREDENTIAL_KEY` environment variable is set. Generate a key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add it to your environment:

```bash
export LUCENT_CREDENTIAL_KEY=your-generated-key-here
```

Or in your Docker Compose `.env` file:

```
LUCENT_CREDENTIAL_KEY=your-generated-key-here
```

> **Important:** Back up this key. If lost, you'll need to re-create all integrations. See [Security Considerations](slack-security.md#encryption-key-management) for key rotation.

### Register the Integration via API

Use the Lucent REST API to create the integration. You'll need an API key with admin privileges.

```bash
curl -X POST https://your-lucent-host/api/v1/integrations \
  -H "Authorization: Bearer hs_your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "slack",
    "external_workspace_id": "T0123ABCDEF",
    "config": {
      "bot_token": "xoxb-your-bot-token",
      "signing_secret": "your-signing-secret"
    },
    "allowed_channels": []
  }'
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Must be `"slack"` |
| `external_workspace_id` | recommended | Your Slack workspace ID (found in **Settings > Workspace settings > Workspace ID**, or from the URL) |
| `config` | yes | Contains `bot_token` and `signing_secret`. Encrypted at rest via Fernet. |
| `allowed_channels` | no | Channel IDs to restrict usage. Empty list = all channels allowed. |

The config object is encrypted before storage — it is **never** returned by the API.

**Response:**

```json
{
  "id": "a1b2c3d4-...",
  "organization_id": "...",
  "type": "slack",
  "status": "active",
  "external_workspace_id": "T0123ABCDEF",
  "allowed_channels": [],
  "config_version": 1,
  "created_by": "...",
  "updated_by": null,
  "created_at": "2026-03-18T...",
  "updated_at": "2026-03-18T...",
  "disabled_at": null,
  "revoked_at": null
}
```

### Via the Web UI

Navigate to **Settings > Integrations** in the Lucent dashboard. Click **Add Integration**, select Slack, and fill in the same fields.

---

## Step 3: Channel Allowlisting

By default, an integration with an empty `allowed_channels` list accepts messages from **all channels** in the workspace. To restrict which channels can use Lucent:

### Find Channel IDs

In Slack, right-click a channel name → **View channel details** → scroll to the bottom to find the **Channel ID** (e.g., `C0123ABCDEF`).

### Set the Allowlist

```bash
curl -X PATCH https://your-lucent-host/api/v1/integrations/{integration_id} \
  -H "Authorization: Bearer hs_your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{
    "allowed_channels": ["C0123ABCDEF", "C0456GHIJKL"]
  }'
```

Messages from channels **not** in this list will receive an ephemeral "This channel isn't configured for Lucent commands" response. The attempt is logged as a `channel_not_allowed` audit event.

### Recommendations

- **Start restrictive**: Begin with a single test channel and expand after validating.
- **Include DM channels**: If users will use pairing codes via DM, the bot's DM channel is always allowed (pairing code verification uses a separate code path).
- **Update atomically**: The `allowed_channels` field is replaced entirely on update — always include the full list.

---

## Step 4: Verify the Setup

1. **Invite the bot** to an allowed channel: `/invite @Lucent`
2. **@mention the bot**: `@Lucent hello`
3. If no users are linked yet, you'll see: *"Your account isn't linked to Lucent yet..."*

This confirms the webhook pipeline is working. Next, [link user accounts](slack-user-linking.md).

---

## Managing the Integration

### Update Credentials

Rotate the bot token or signing secret:

```bash
curl -X PATCH https://your-lucent-host/api/v1/integrations/{integration_id} \
  -H "Authorization: Bearer hs_your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "bot_token": "xoxb-new-token",
      "signing_secret": "new-signing-secret"
    }
  }'
```

The `config_version` increments on each update for audit traceability.

### Disable / Re-enable

```bash
# Disable — all active user links become orphaned
curl -X PATCH .../api/v1/integrations/{id} \
  -H "Authorization: Bearer hs_..." \
  -d '{"status": "disabled"}'

# Re-enable
curl -X PATCH .../api/v1/integrations/{id} \
  -H "Authorization: Bearer hs_..." \
  -d '{"status": "active"}'
```

> **Warning:** Disabling an integration orphans all active user links. Users will need to re-link after re-enabling.

### Delete

```bash
curl -X DELETE https://your-lucent-host/api/v1/integrations/{integration_id} \
  -H "Authorization: Bearer hs_your_admin_key"
```

This is a soft delete — the record is marked `deleted` and all user links are orphaned.

### Admin-Create User Links

Admins can bypass the pairing code flow and directly link a Lucent user to a Slack identity:

```bash
curl -X POST https://your-lucent-host/api/v1/integrations/links \
  -H "Authorization: Bearer hs_your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{
    "integration_id": "a1b2c3d4-...",
    "user_id": "lucent-user-uuid",
    "external_user_id": "U0123SLACK",
    "external_workspace_id": "T0123ABCDEF"
  }'
```

Admin-created links are automatically activated (no pairing code needed) and recorded in the audit log as `verification_method: "admin"`.

---

## Related Docs

- [User Linking Guide](slack-user-linking.md) — Pairing code flow for end users
- [Integration API Reference](integrations-api-reference.md) — Full endpoint documentation
- [Security Considerations](slack-security.md) — Encryption, signatures, audit
- [Troubleshooting](troubleshooting.md#slack-integration) — Common issues
