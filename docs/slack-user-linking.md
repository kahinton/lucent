# Slack Integration — User Linking Guide

This guide explains how Lucent users link their Slack identity to their Lucent account using the pairing code flow.

## Overview

Before Lucent can respond to your messages in Slack, it needs to know who you are. The pairing code flow creates a secure link between your Slack user and your Lucent account without exposing any credentials.

**How it works:**

1. You generate a one-time pairing code in Lucent
2. You send that code to the Lucent bot in Slack as a DM
3. Lucent verifies the code and links your accounts
4. All future Slack messages from you are recognized as your Lucent identity

---

## Prerequisites

- A Lucent account (any role)
- The Slack integration is active for your organization (set up by your admin)
- The Lucent bot is installed in your Slack workspace

---

## Step 1: Generate a Pairing Code

### Via the Web UI

1. Log in to Lucent at `https://your-lucent-host`
2. Navigate to **Settings > Integrations > Link Account**
3. Select the Slack integration
4. Click **Generate Pairing Code**
5. Copy the displayed code

### Via the API

```bash
curl -X POST https://your-lucent-host/api/v1/integrations/link \
  -H "Authorization: Bearer hs_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "integration_id": "your-integration-uuid"
  }'
```

**Response:**

```json
{
  "id": "challenge-uuid",
  "integration_id": "...",
  "user_id": "your-user-uuid",
  "code": "Abc1Def2Ghi3Jkl4Mnop5Q",
  "expires_at": "2026-03-18T19:40:00Z",
  "status": "pending",
  "created_at": "2026-03-18T19:30:00Z"
}
```

> **Important:** The `code` field is only shown in this response. It is never stored in plaintext — only a bcrypt hash is kept in the database.

---

## Step 2: Send the Code in Slack

Open a **direct message** with the Lucent bot in Slack and send:

```
/lucent link Abc1Def2Ghi3Jkl4Mnop5Q
```

Or simply DM the code to the bot:

```
Abc1Def2Ghi3Jkl4Mnop5Q
```

The bot will respond with a confirmation if the code is valid.

---

## Step 3: Confirm the Link

Once verified, you'll see a confirmation message in the Slack DM. Your Slack identity is now linked to your Lucent account.

You can verify the link status via the API:

```bash
curl https://your-lucent-host/api/v1/integrations/links \
  -H "Authorization: Bearer hs_your_api_key"
```

Your link will show `status: "active"`.

---

## Using Lucent in Slack

Once linked, you can interact with Lucent in any allowed channel:

- **@mention**: `@Lucent search for authentication patterns`
- **DM**: Send any message directly to the Lucent bot
- **Slash commands**: `/lucent <query>` (if configured by your admin)

Lucent processes your request through the same memory and tool pipeline as the web interface, with your full identity and permissions.

---

## Code Constraints

| Property | Value |
|----------|-------|
| Code length | 22 characters (128-bit, URL-safe base64) |
| Expiry | 10 minutes from generation |
| Max attempts | 5 per code (then auto-exhausted) |
| Rate limit | 10 codes per user per hour |
| Storage | bcrypt hash only (plaintext never stored) |

If your code expires or is exhausted, generate a new one.

---

## Re-linking

If you need to link a different Slack account (or the same one to a different Lucent account):

1. Generate a new pairing code
2. Send it from the new Slack account

The old link is automatically **superseded** — only one active link per external identity is allowed. The superseded link is preserved in the audit trail.

---

## Unlinking

Ask your Lucent admin to revoke the link, or use the API if you have admin privileges:

```bash
curl -X DELETE https://your-lucent-host/api/v1/integrations/links/{link_id} \
  -H "Authorization: Bearer hs_your_admin_key"
```

After unlinking, Slack messages from you will no longer be recognized by Lucent until you re-link.

---

## Troubleshooting

### "Your account isn't linked to Lucent yet"

This message appears when Lucent receives a Slack message from an unlinked user. Follow the pairing code flow above to link your account.

### Code expired or invalid

Pairing codes expire after 10 minutes. Generate a new one and try again.

### "Too many pairing codes"

You've hit the rate limit of 10 codes per hour. Wait and try again later.

### Code won't verify after multiple attempts

Each code allows 5 verification attempts. After 5 failures, the code is exhausted. Generate a fresh code.

### Bot doesn't respond in a channel

- The channel may not be in the integration's allowlist. Ask your admin to add it.
- The bot may not be invited to the channel. Use `/invite @Lucent`.

---

## Related Docs

- [Admin Setup Guide](slack-admin-setup.md) — Integration creation and channel management
- [Security Considerations](slack-security.md) — How the pairing flow is secured
- [Troubleshooting](troubleshooting.md#slack-integration) — More debugging tips
