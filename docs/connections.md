# Connections

Lucent's **Settings → Connections** page manages two distinct kinds of credentials:

1. **Workspace connections** — apps/bots installed for an *organization* (GitHub App, Slack bot, Discord bot, future Jira/Linear apps). Owned by the org, managed by admins.
2. **Your connected accounts** — *your* personal credentials linking your Lucent identity to an external account (GitHub OAuth/PAT, Slack identity). Owned and managed by you.

Both kinds are surfaced under a single sidebar item (`Settings → Connections`) but are presented in two clearly separated sections.

---

## Two-Tier Model

| Tier | Stored in | Owns | Manages | Examples |
|------|-----------|------|---------|----------|
| **Workspace connections** | `integrations` table | Organization | Admin / Owner (`MANAGE_INTEGRATIONS`) | GitHub App install, Slack bot install, Discord bot install |
| **Your connected accounts** | `enterprise_credentials` table (scope = `user`) | Individual user | The user themself | GitHub PAT, GitHub OAuth, Slack OAuth, env-token claim |

```
┌─────────────────────────────────────────────────────────────────┐
│  Settings → Connections                                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌── Workspace connections ─────────────────────────────────┐   │
│  │  Org-owned app installs.   Admin/Owner only.            │   │
│  │  • GitHub App     [active]   Disable | Revoke           │   │
│  │  • Slack bot      [active]   Disable | Revoke           │   │
│  │  Members see this list as read-only.                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌── Your connected accounts ──────────────────────────────┐   │
│  │  Personal credentials. Visible only to you.             │   │
│  │  • GitHub (OAuth)  [active]                Revoke       │   │
│  │  • Slack (OAuth)   [active]                Revoke       │   │
│  │  • Add a personal access token (PAT) ▸                  │   │
│  │  • Claim env token (GITHUB_TOKEN detected) ▸            │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Why the split

- **Repo ACL must come from the user, not the app.** A GitHub App being installed on an org gives Lucent a way to *see* repos at the app level — but it tells us nothing about whether *you* personally have access. User authorization decisions consult only your personal GitHub credential.
- **Admin-managed installs vs. self-service identity.** Adding a Slack bot to your org is a privileged, irreversible-feeling action. Connecting your own GitHub account is not. They deserve different UI affordances and different permission gates.
- **Open-source friendliness.** A solo developer running Lucent locally should still be able to paste a PAT and get going, with no "workspace integrations" ceremony required. That path is preserved as a first-class, feature-flagged mode.

---

## Feature Flags

All connection features are gated by a small set of environment variables, read through one helper module (`src/lucent/integrations/connection_flags.py`). Defaults are tuned for a friendly local/open-source experience.

| Flag | Default | Controls |
|------|---------|----------|
| `LUCENT_CONNECTIONS_PAT_ENABLED` | `true` | PAT entry form (UI) and `POST /settings/connections/pat` (backend). When `false`, the form is hidden and any PAT save attempt is rejected with `403 feature_disabled`. |
| `LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED` | `true` | Detection card for tokens like `GITHUB_TOKEN` (UI) and `POST /settings/connections/env/claim` (backend). Also gates the `GITHUB_TOKEN` fallback in `_get_any_github_token` (existence-check only, never user ACL). |
| `LUCENT_CONNECTIONS_OAUTH_ENABLED` | `true` | OAuth "Connect with …" buttons. Per-provider visibility additionally requires `LUCENT_OAUTH_<PROVIDER>_CLIENT_ID` to be configured. |
| `LUCENT_WORKSPACE_INTEGRATIONS_ENABLED` | `true` | The entire **Workspace connections** section. When `false`, the section is hidden and the read-model skips loading workspace rows entirely. |
| `LUCENT_GITHUB_APP_ENABLED` | `false` | GitHub App setup, install-id-aware queries, and `app_installation_can_see_repo()`. When `false`, the method returns `None` ("unknown") regardless of any rows in the `integrations` table. |
| `LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL` | `false` | Strict repo ACL mode. When `true`, users without a personal GitHub credential are denied repo access (with reason `user_github_credential_required` and a "Connect your GitHub…" hint). When `false`, no-credential users are allowed for back-compat with existing single-user deployments. |

### Security implications

- **`PAT_ENABLED=false`** removes a category of long-lived secret entirely. Recommended for any environment where OAuth or a GitHub App can do the job.
- **`ENV_TOKEN_CLAIM_ENABLED=false`** prevents process-level env vars from being silently promoted into per-user credentials. Recommended for any shared/multi-tenant deployment.
- **`OAUTH_ENABLED=true` without `<PROVIDER>_CLIENT_ID`** is a no-op for that provider; the button is suppressed. There is no insecure half-state.
- **`WORKSPACE_INTEGRATIONS_ENABLED=false`** hides the section *and* short-circuits the read model so non-admins never trigger workspace queries.
- **`GITHUB_APP_ENABLED=true` does NOT change user ACL.** It only allows app-installation visibility checks via the separate `app_installation_can_see_repo()` API. App visibility is never substituted for user authorization.
- **`REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=true`** is the strict mode that removes the single-user back-compat allowance. Always pair this with at least one of `PAT_ENABLED=true` or `OAUTH_ENABLED=true` (with GitHub OAuth configured) so users have a way to actually connect.

All four mutation endpoints under `/settings/connections/*` (PAT save, env-token claim, OAuth start, revoke) require a valid CSRF token (`X-CSRF-Token` header, or the form field for the revoke flow).

---

## Setup Profiles

Three reference profiles. Pick the one closest to your deployment, then adjust.

### Profile 1 — Simple local / open-source (default)

Solo developer running Lucent locally. PAT-driven GitHub access, no workspace apps required.

```bash
# Connections — defaults are already correct, listed here for clarity
LUCENT_CONNECTIONS_PAT_ENABLED=true
LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED=true
LUCENT_CONNECTIONS_OAUTH_ENABLED=true        # no-op unless OAuth client IDs are set
LUCENT_WORKSPACE_INTEGRATIONS_ENABLED=false  # hide workspace section entirely
LUCENT_GITHUB_APP_ENABLED=false
LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=false

# Existing env token will be auto-detected by the env-claim card
GITHUB_TOKEN=ghp_yourpersonalaccesstoken
```

Behavior:
- `Settings → Connections` shows only **Your connected accounts**.
- The PAT form is visible. Pasting a PAT stores it encrypted at rest.
- The env-claim card appears because `GITHUB_TOKEN` is set.
- No GitHub App or workspace bots needed.

### Profile 2 — Team

Shared Lucent for a team. OAuth for first-class user identity, workspace integrations for org-wide bots, PAT kept as an opt-in fallback.

```bash
LUCENT_CONNECTIONS_PAT_ENABLED=true             # fallback for users without OAuth
LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED=false
LUCENT_CONNECTIONS_OAUTH_ENABLED=true
LUCENT_WORKSPACE_INTEGRATIONS_ENABLED=true
LUCENT_GITHUB_APP_ENABLED=false                 # enable when you finish App setup
LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=false

# OAuth client IDs/secrets — set per provider you want to enable
LUCENT_OAUTH_GITHUB_CLIENT_ID=Iv1.xxxxxxxxxxxxxxxx
LUCENT_OAUTH_GITHUB_CLIENT_SECRET=...
```

Behavior:
- Both sections visible. Members can self-connect via OAuth or PAT.
- Admins can install Slack/Discord bots from **Workspace connections**.
- Env-token auto-claim is off — credentials must be intentional.

### Profile 3 — Enterprise

Hardened multi-tenant deployment. No PATs, no env-token shortcuts, mandatory user GitHub credential, GitHub App installed for repo visibility.

```bash
LUCENT_CONNECTIONS_PAT_ENABLED=false
LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED=false
LUCENT_CONNECTIONS_OAUTH_ENABLED=true
LUCENT_WORKSPACE_INTEGRATIONS_ENABLED=true
LUCENT_GITHUB_APP_ENABLED=true
LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=true

LUCENT_OAUTH_GITHUB_CLIENT_ID=Iv1.xxxxxxxxxxxxxxxx
LUCENT_OAUTH_GITHUB_CLIENT_SECRET=...
```

Behavior:
- PAT form is hidden. Server rejects any direct PAT save with `403 feature_disabled`.
- Env-claim card is hidden. `GITHUB_TOKEN` in the process env is **not** promoted to a user credential.
- Users without a connected GitHub account cannot access repo-scoped functionality (denied with `user_github_credential_required`). They are pointed at the OAuth flow.
- Admins manage the GitHub App install in **Workspace connections**. Repo visibility checks via `app_installation_can_see_repo()` are available, but **never substitute** for user ACL.

---

## Roles and UI Visibility

| UI control | Member sees | Admin / Owner sees |
|------------|-------------|---------------------|
| Workspace connections section | Read-only list with "View only" badge | Full list with **Disable** and **Revoke** actions |
| Add workspace integration (Slack / Discord / GitHub App) | hidden | visible |
| Your connected accounts (own credentials) | visible — full self-service | visible — full self-service |
| Other users' connected accounts | never visible | never visible (per-user isolation) |
| PAT form | gated by `LUCENT_CONNECTIONS_PAT_ENABLED` | same |
| Env-token claim card | gated by `LUCENT_CONNECTIONS_ENV_TOKEN_CLAIM_ENABLED` and presence of the env var | same |

**Backend enforcement.** Workspace-integration mutations (`POST/PATCH/DELETE /api/v1/integrations/...`) require the `MANAGE_INTEGRATIONS` permission, enforced both by the `AdminUser` dependency and by an explicit `user.require_permission(...)` check inside each handler. The web routes mirror this for the `/settings/connections/...` admin actions.

---

## GitHub Repo ACL Rule

> **App installation visibility is not a substitute for user access.**

User authorization decisions in `GitHubRepoAccessService.check_access()` consult **only** the user's personal GitHub credential (`enterprise_credentials`, scope `user`). The result is a `RepoAccessDecision(allowed, reason, hint)`.

- A GitHub App being installed on the org tells us the *app* can see the repo. It tells us nothing about whether the *user* can.
- The separate method `app_installation_can_see_repo(organization_id, repo)` returns `True | False | None` — and `None` ("unknown") is the typical result today (see follow-up below). Callers must treat `None` as unknown, not as deny, and must never read it as a user-ACL signal.
- A regression test (`test_app_installed_does_not_silently_grant_user_access`) enforces this separation: with `LUCENT_REQUIRE_USER_GITHUB_CONNECTION_FOR_REPO_ACL=true` and `LUCENT_GITHUB_APP_ENABLED=true`, a user with no personal credential is still denied — and no app-side HTTP call is made.

---

## GitHub App Status (Current State)

What's implemented in this release:

- `integrations.type` accepts `github_app` (migration 069), with `install_id`, `health_status`, `health_detail`, `health_checked_at` columns and a partial unique index on `(org, type, install_id)` for active rows.
- `LUCENT_GITHUB_APP_ENABLED` flag.
- `app_installation_can_see_repo()` API method, gated by the flag, with strict separation from user ACL.
- Workspace-connection admin CRUD for `github_app` rows.

**Deferred to a follow-up task:**

- **App JWT signing + installation-token minting.** Until this lands, `app_installation_can_see_repo()` returns `None` ("unknown") for all inputs even when an install row exists. Consumers must handle `None` as "no signal."
- **Webhook receiver for GitHub App events.** The schema is ready (install row, health columns), but the HTTP endpoint, `X-Hub-Signature-256` HMAC verification, and `X-GitHub-Delivery` dedupe are not yet implemented. Follow the existing Slack/Discord pattern in [Integrations API Reference — Webhooks](integrations-api-reference.md#webhooks) when this is built.

Tracking these as follow-up work — do not enable `LUCENT_GITHUB_APP_ENABLED=true` in production with the expectation of working webhook-driven behavior yet.

---

## Related Documentation

- [Configuration](configuration.md#connections--feature-flags) — flag reference table
- [Getting Started](getting-started.md) — first-run setup including the simple PAT path
- [Integrations API Reference](integrations-api-reference.md) — REST endpoints for workspace integrations and webhooks
- [Security Model](security-model.md) — authentication, RBAC, and permission gates
- [Secret Storage](secret-storage.md) — how PATs and OAuth tokens are encrypted at rest
