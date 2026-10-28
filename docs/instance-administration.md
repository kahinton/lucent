# Instance administration: hyperadmin and the ops console

Lucent can host more than one organization. This document covers the two pieces
that make it a *service* you can run for other people rather than a tool you use
yourself:

1. **Hyperadmin** — an instance-level role that can create and manage every
   tenant. This is the API an external signup site calls.
2. **The ops console** — a small, local-only web UI for administering a handful
   of organizations by hand, and a test bed for that external site.

If you only run Lucent for yourself, you need neither of these.

---

## 1. Prerequisites

### No mode configuration needed

The organization APIs (`/api/organizations/**`) are **always registered**. Access
is enforced by role and permission, so a hyperadmin key works and any other key
gets `403` — never a `404`.

`LUCENT_MODE` is reporting only. It sets a label in the UI and a log line, and it
does not mount, unmount, enable, or disable anything. Set it to whatever describes
your deployment; leaving it at the default `personal` on a multi-tenant instance
changes nothing about what the API will do.

### A hyperadmin account

A hyperadmin is a member of no customer tenant. It lives in the internal system
organization (`__lucent_system__`) so it never appears in a customer's member
list.

```bash
# Promote an existing user
docker exec lucent-server python -m lucent.hyperadmin grant --email ops@example.com

# Or create the operator account from scratch
docker exec lucent-server python -m lucent.hyperadmin grant \
    --email ops@example.com --create --display-name "Instance Ops"

# Mint the API key the console (or your signup site) will use
docker exec lucent-server python -m lucent.hyperadmin mint-key --email ops@example.com

# See what the instance looks like
docker exec lucent-server python -m lucent.hyperadmin status
```

`grant`, `revoke`, and `mint-key` are the only ways to create or destroy
instance-level access. The tenant-facing user endpoints deliberately refuse to
assign `hyperadmin` (see `can_assign_role` in `src/lucent/rbac.py`), so an
organization owner can never escalate to instance operator.

`status` reports the configured mode for context. It is a label, not a
capability switch.

---

## 2. Hyperadmin API

All routes are under `/api/organizations`. Every route listed here requires
`role=hyperadmin`; a tenant owner gets `403`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/provision` | **Create a complete tenant** (org + first admin + key) |
| `GET` | `?search=&status=&include_stats=` | List tenants with member/key counts |
| `GET` | `/{id}?include_stats=true` | One tenant |
| `PATCH` | `/{id}` | Rename |
| `POST` | `/{id}/suspend` | Block sign-in and keys, keep all data |
| `POST` | `/{id}/resume` | Reactivate |
| `DELETE` | `/{id}?confirm_name=<name>` | Permanently delete |
| `GET` | `/{id}/users` | Members (daemon identity hidden by default) |
| `POST` | `/{id}/users?mint_api_key=` | Add a member |
| `PATCH` | `/{id}/users/{uid}/role` | Change a member's role |
| `POST` | `/{id}/users/{uid}/reset-password` | Temporary password, returned once |
| `POST` | `/{id}/users/{uid}/api-keys` | Mint a key for that member |
| `DELETE` | `/{id}/users/{uid}` | Remove a member |
| `GET` | `/{id}/settings` | Editable runtime settings for that tenant |
| `PUT` | `/{id}/settings/{key}` | Set (`{"value": ...}`) or reset (`{"reset": true}`) |
| `GET` | `/{id}/setting-locks` | Settings this tenant cannot change |
| `PUT` | `/{id}/setting-locks/{key}` | Pin a setting (`{"reason": "..."}`) |
| `DELETE` | `/{id}/setting-locks/{key}` | Release a pin back to the tenant |
| `PUT` | `/{id}/setting-locks` | **Reconcile the whole lock set** (tier apply) |
| `POST` | `/{id}/setting-locks/release` | Release a named set of locks |
| `GET` | `/instance/setting-locks` | Every lock across every tenant |
| `GET` | `/instance/settings-schema` | Key/type/default/bounds for every editable setting |
| `GET` | `/instance/runtime` | Configured LLM engine and the providers it can run |
| `GET` | `/instance/providers` | Provider catalogue, with the engines using each |
| `GET` | `/instance/engines` | Engines, accepted providers, config fields |
| `GET` | `/{id}/credentials` | Which providers that tenant has a key for |
| `PUT` | `/{id}/credentials/{provider}` | Store a provider API key (`{"value": ...}`) |
| `DELETE` | `/{id}/credentials/{provider}` | Remove it, falling back to the instance key |
| `POST` | `/{id}/providers/discover` | Register the models those keys expose |
| `GET` | `/{id}/models` | Models visible to the tenant (owned and shared) |
| `POST` | `/{id}/models` | Define a model for that tenant and grant it |
| `POST` | `/{id}/models/{model_id}/grant` | Grant a model, adopting it if instance-shared |
| `PATCH` | `/{id}/models/{model_id}` | Enable/disable an **org-owned** model |
| `GET` | `/{id}/audit` | That tenant's admin audit log |
| `GET` | `/instance/audit` | Operator trail across all tenants |
| `GET` | `/instance/usage/resources` | **Seats + resource usage for every tenant** |
| `GET` | `/{id}/usage/resources` | Seats + resource usage for one tenant |
| `GET` | `/{id}/usage/ledger` | Raw usage intervals behind the totals |
| `GET` | `/instance/usage/backfill-preview` | What history a backfill would recover |
| `POST` | `/instance/usage/backfill` | Reconstruct usage from pre-ledger sandboxes |

### Usage data

Two different things are reported, with different semantics:

- **Seats** are a *live count* — "who is on the account right now". `metered`
  excludes service identities (`daemon`) and operator staff (`hyperadmin`) and,
  by default, inactive members. Counting either category would inflate every
  invoice and make the seat count depend on how Lucent happens to be deployed.
- **Resource usage** is *history* — sandbox CPU and memory reservations over time,
  read from the append-only ledger (`resource_usage_events`, migration 154).

```http
GET /api/organizations/instance/usage/resources?starts_at=2026-10-01T00:00:00Z
```

```json
{
  "instance_totals": {"seats": 14, "intervals": 312, "cpu_seconds": 918400.0,
                      "cpu_hours": 255.1, "memory_mb_seconds": 1884160000},
  "tenants": [
    {
      "organization_id": "...",
      "organization_name": "Acme Robotics",
      "status": "active",
      "seats": {"total": 6, "active": 5, "metered": 5, "by_role": {"owner": 1, "member": 4}},
      "resources": {"intervals": 41, "duration_seconds": 147600,
                    "cpu_seconds": 295200.0, "cpu_hours": 82.0,
                    "memory_mb_seconds": 604569600}
    }
  ]
}
```

**Lucent records quantities, not prices.** There is deliberately no rate, plan,
charge, or tier in this API: the application that sells the product decides what
a CPU-hour costs, and a stored rate would go stale the moment a price changed.

Four properties are worth knowing:

- **`NULL` is not zero.** A usage row whose limits could not be parsed contributes
  nothing to a sum but stays visible in `/usage-events`, because an unknown
  reservation must not be invoiced as free. The same applies to seats: a `null`
  count means "not applicable", not "none".
- **A stopped-then-destroyed sandbox meters to the stop.** The first close wins, so
  the later teardown updates the outcome without extending the interval. Taking
  the later end would meter idle hours.
- **Usage survives tenant deletion.** `organization_id` is `ON DELETE SET NULL`
  and the tenant name is snapshotted, so an offboarded tenant still appears in the
  report with `status="deleted"` and `seats=null` — its final period is still
  chargeable. This is why the ledger is a separate table rather than columns on
  `sandboxes`, which is deleted with the tenant.
- **`cpu_seconds` and `memory_mb_seconds` are derived server-side** as
  `limit × duration_seconds`, so every consumer agrees on rounding instead of
  recomputing it.

`resources_by_source` splits consumption into `task`, `managed_tool`, and
`interactive`, so a provider that prices custom tooling differently can rate them
separately.

#### Tool invocations

A managed-tool execution produces **two** ledger entries, and they measure
different things:

| Row | Counts as | Carries |
|-----|-----------|---------|
| `resource_type = "sandbox"` | compute | `cpu_limit`, `memory_limit_mb`, duration |
| `resource_type = "managed_tool_run"` | invocations | duration, outcome, `tool_id` |

This split exists because a tool run *creates* the sandbox it runs in — the same
resources, described twice. **Tool-run rows carry no CPU or memory on purpose**, and
the aggregation counts compute only from `sandbox` rows. A database CHECK enforces
it, so a future caller cannot write a reservation onto an invocation row and
double-bill every custom-tool call.

The payoff is that per-call pricing and per-CPU-second pricing can coexist on the
same ledger without either polluting the other:

```json
{
  "resources": {"intervals": 41, "cpu_hours": 82.0, "tool_invocations": 312},
  "tool_usage": [
    {"tool_id": "...", "invocations": 300, "succeeded": 297, "duration_seconds": 4210}
  ]
}
```

`tool_usage` is grouped per tool definition so one expensive tool can be priced
differently from a cheap, frequently-called one. `succeeded` counts runs whose
outcome is `completed`; `failed` and `blocked` runs still count as
invocations, because the compute was spent either way.

Tool-run rows survive tenant deletion for the same reason sandbox rows do — that
is the gap `managed_tool_runs` could not fill, since it cascades with the
organization. Outcomes use the tool vocabulary (`completed`, `failed`, `blocked`),
not the sandbox one; they are answers to different questions.

#### Historical usage

The ledger only starts recording from migration 154. Existing finished sandboxes
can be reconstructed from their stored config and timestamps:

```bash
# see what would be recovered
curl ".../api/organizations/instance/usage/backfill-preview?organization_id=$ORG"
# commit it (idempotent)
curl -X POST ".../api/organizations/instance/usage/backfill?organization_id=$ORG"
```

Backfill only considers *finished* sandboxes: a live one has no end yet and would
produce an interval that grows on every run. Re-running is safe — the ledger's
unique key plus first-close-wins means nothing is double-billed.

### Locking settings for a tenant

A lock is a **write barrier, not a value**. The effective value keeps living in
`runtime_settings` (or its environment/built-in fallback); a lock only removes
the tenant's ability to change it. Two consequences worth knowing before using
them:

* **Set, then lock.** The two are deliberately separate calls, so you can pin a
  setting without also pinning a value — the tenant is then held to whatever the
  instance currently resolves to.
* **Reset is blocked too.** Without that, a tenant could delete the DB row and
  fall back to an instance default the operator was overriding, undoing the pin
  in one click.

Operator writes are *not* blocked: the same API owns the barriers. That is how a
plan change re-pins a tenant to a new value without an unlock in between.

Unlocking changes only *who may write* — the stored value is untouched, so
releasing a barrier never silently changes what the customer experiences.

```bash
# pin sandboxes off for one tenant
curl -X PUT .../api/organizations/$ORG/settings/features.sandboxes_enabled \
     -d '{"value": false}'
curl -X PUT .../api/organizations/$ORG/setting-locks/features.sandboxes_enabled \
     -d '{"reason": "Not included in your plan."}'

# hand it back (value stays false, tenant may now change it)
curl -X DELETE .../api/organizations/$ORG/setting-locks/features.sandboxes_enabled
```

Every lock and unlock is written to the operator's own audit trail **and** to the
tenant's, as `org.setting_lock` / `org.setting_unlock`. A customer can always see
that access to a setting was removed, and by whom.

The tenant's Settings page renders a locked setting as read-only, with the
operator's `reason` in place of the editing controls.

### Applying a tier

Tier definitions belong to the application that sells them; this API supplies
only the primitives that application needs. One call sets a tenant's complete
lock set:

```http
PUT /api/organizations/{id}/setting-locks
Authorization: Bearer <hyperadmin key>

{
  "locks": [
    {"key": "features.sandboxes_enabled", "reason": "Starter plan", "value": false},
    {"key": "models.chat_model", "reason": "Starter plan", "value": "<model id>"},
    {"key": "memory.search_vitality_boost_alpha", "reason": "Starter plan"}
  ],
  "release_unlisted": true,
  "dry_run": false
}
```

The response is a diff, not a status:

```json
{
  "organization_id": "...",
  "dry_run": false,
  "locked": ["features.sandboxes_enabled"],
  "updated": ["models.chat_model"],
  "values_written": ["memory.search_vitality_boost_alpha"],
  "released": ["some.other.setting"],
  "unchanged": ["another.setting"],
  "not_locked": [],
  "failed": [{"key": "retired.setting", "error": "Unknown or read-only setting: ..."}]
}
```

`updated` (the **lock** changed, i.e. its reason) and `values_written` (the
**value** changed) are deliberately separate: a plan can change a limit without
changing which settings it pins, and a caller reconciling a tier needs to tell
those apart. A setting whose value write fails is **not** locked — otherwise the
tenant would be pinned to exactly the stale value the caller was replacing.

Four properties are deliberate, because a webhook that fires twice, or a plan
change during a catalog migration, is the normal case rather than the exception:

* **Declare the target state, don't send a delta.** `release_unlisted: true`
  (the default) releases any lock not named in `locks`, so re-sending the same
  body is a no-op and the call converges instead of accumulating. Use
  `release_unlisted: false` when you want to add to a tenant's set without
  undoing pins an operator placed by hand.
* **`value` is written before the lock.** A tier that says "pin this to X" can
  never end up with a lock on a value it never wrote. Omit `value` to freeze the
  setting at whatever the instance currently resolves to — no DB row is created.
  A value that differs from what is stored is written even when the `reason` is
  unchanged, so "same tier, new limit" is applied rather than skipped.
* **Per-setting failure, never all-or-nothing.** A tier naming one retired
  setting still applies the other twenty and reports the bad one in `failed`. A
  catalog change cannot brick the caller's whole flow.
* **`dry_run` returns the same diff without writing**, for pre-flighting a plan
  change. It writes no audit row either.

One audit row is written per batch — a tier apply is one decision — with the
affected keys in `changed_fields` and the reasons under `new_values.reasons`. A
batch that changed nothing writes nothing.

To undo a tier, release exactly the keys it pinned:

```http
POST /api/organizations/{id}/setting-locks/release
{"keys": ["features.sandboxes_enabled", "models.chat_model"]}
```

This is **not** a reconcile: anything an operator pinned by hand survives. Keys
that were not locked come back in `not_locked` rather than erroring, so releasing
the same set twice is safe.

### Monitoring locks across tenants

```http
GET /api/organizations/instance/setting-locks?key=features.sandboxes_enabled
```

Returns `{total, locks, by_setting, by_organization}` — the same rows grouped
both ways so a dashboard does not have to reshape them. Each row carries
`organization_name`, `reason`, `locked_by_name`, and `db_value`.

`db_value` is the tenant's **stored override** and is `null` when the tenant
inherits the environment or built-in default. That is the frozen-fallback case,
and a monitoring caller needs to see it rather than infer it: it means "this
tenant is pinned, but to the instance's value rather than one of its own".

Without this endpoint, answering "which tenants have X pinned" would be
O(tenants) HTTP round trips.

### The signup flow

`POST /provision` is the endpoint to build a signup site on. One call:

```http
POST /api/organizations/provision
Authorization: Bearer <hyperadmin key>
Content-Type: application/json

{
  "name": "Acme Robotics",
  "first_admin": {
    "email": "owner@acme.test",
    "display_name": "Acme Owner"
  },
  "api_key_name": "Default API key",
  "model_id": "claude-sonnet-4.6",
  "providers": [
    {"provider": "openai", "api_key": "sk-..."}
  ],
  "settings": {
    "features.sandboxes_enabled": false,
    "memory.inject_max_memories": 2
  }
}
```

Response (`201`):

```json
{
  "organization": {"id": "...", "name": "Acme Robotics", "status": "active", ...},
  "user": {"id": "...", "email": "owner@acme.test", "role": "owner",
           "must_change_password": true},
  "api_key": "hs_...",
  "temporary_password": "...",
  "seeded": {"skills": 38, "agents": 14, "hooks": 2, "sandbox_templates": 3},
  "model_id": "claude-sonnet-4.6",
  "providers_configured": ["openai"],
  "settings_applied": {"features.sandboxes_enabled": false,
                       "memory.inject_max_memories": 2},
  "warnings": []
}
```

What it guarantees:

* **All or nothing.** If the first admin cannot be created (duplicate identity,
  weak password), the organization is rolled back. A tenant with nobody in it is
  worse than a clean failure — it is invisible in every UI and can only be
  removed by hand.
* **Usable on first login.** Built-in skills/agents/hooks/sandbox templates are
  seeded, and `model_id` is enabled *and* granted (an enabled-but-ungranted model
  is not usable — see `docs/` bootstrap notes).
* **Secrets are one-time.** `api_key` and `temporary_password` are returned in
  this response only. Both are stored hashed (the password) or hashed (the key);
  neither can be retrieved again.
* **Providers and settings are applied, not queued.** Each entry in `providers`
  is stored at the canonical `model_providers.<provider>.api_key` secret for that
  organization — the key the engines actually read — and each entry in `settings`
  goes through the same validator the settings UI uses. A setting that cannot be
  applied is reported in `warnings` rather than aborting the tenant; the
  organization still exists and is usable.
* **Auditable.** See below.

Omitting `first_admin` is a `400`. Use `POST /api/organizations` (no suffix) if
you genuinely want a headless shell — that endpoint predates `/provision` and is
kept for compatibility.

### Configuring an existing tenant's models

`/provision` is the fast path, but an operator also needs to change a tenant that
already exists. Three concepts matter, and conflating them is the most common way
to end up with a tenant whose chat does not work:

* **Provider credentials** are API keys, stored per organization at
  `model_providers.<provider>.api_key`. `PUT /{id}/credentials/{provider}` writes
  one; `DELETE` removes it so the instance credential applies again. Values are
  write-only — the API reports `configured: true`, never the key.
* **Engine backends** are optional routing overrides (base URL, gateway, pinned
  model). A backend alone does **not** make a provider usable.
* **Models** must be enabled *and* granted. Usage is clearance-driven and a
  clearance can only point at a row the organization owns, so a shared
  (instance-wide) model has to be **copied** into the tenant before it can be
  granted. `POST /{id}/models/{model_id}/grant` does that copy (id
  `<model>@<org-prefix>`) and leaves the shared row in place, so any number of
  tenants can grant the same model. `PATCH /{id}/models/{model_id}` refuses
  shared rows with `409` — flipping one would change every tenant.

After storing a key, run `POST /{id}/providers/discover` to register the models
that provider exposes, then grant the ones the tenant should use.

### Visibility of operator changes

Every mutating instance route writes **two** audit rows:

* into the **operator's own organization** — the durable instance trail. It
  survives the tenant being deleted, and it is what `/instance/audit` reads.
* into the **target organization** — so the tenant's own admins see, in
  Settings → Audit, that an operator changed their workspace. A change nobody
  can see is not acceptable for an operator-managed instance.

Actions: `org.provision`, `org.update`, `org.suspend`, `org.resume`,
`org.setting_update`, `org.user_create`, `org.user_role_change`,
`org.api_key_create`, `org.delete`, `user.delete`, `user.password_reset`.

### Suspension semantics

Suspension is a **soft stop**:

* sessions and API keys stop authenticating (`organization_allows_access`);
* nothing is deleted — members, memories, requests, and settings all remain;
* hyperadmins keep access, so a suspended tenant can still be inspected,
  resumed, or purged.

### One owner per organization

`idx_one_owner_per_org` (migration 007) permits exactly one `owner` row per
organization. Consequences the API enforces for you:

* adding a member with `role: "owner"` to a tenant that already has one returns
  `409` with an explanation;
* changing a member's role to `owner` under the same condition returns `409`;
* ownership must be **transferred** by the current owner, not granted twice.
  `UserRepository.transfer_ownership` performs the demote-then-promote pair in a
  single transaction — the reverse order always violates the index.
* removing the last owner returns `400`, so a tenant can never be left
  unmanageable.

### Organization names

Names are unique per instance, compared **case-insensitively**, enforced by a
database unique index (migration 149) rather than an application-level check.
Two concurrent signups with the same name can no longer create two tenants that
look identical in every list.

---

## 3. The ops console

A standalone web app for administering a few organizations by hand.

```
docker compose --profile ops-console up -d
open http://127.0.0.1:8767
```

### Setting it up from scratch

Three steps, all copy-pasteable.

**1. Create the operator account and mint its key.** This needs shell access to
the instance, which is the intended trust boundary for instance-level power.

```bash
docker exec lucent-server python -m lucent.hyperadmin \
    grant --email you@example.com --create --display-name "Instance Ops"
docker exec lucent-server python -m lucent.hyperadmin \
    mint-key --email you@example.com --name "ops-console"
```

Copy the `hs_...` key the second command prints — it is shown once.

**2. Put the two secrets in `.env`.** Compose reads them, so they survive
restarts and you never retype them.

```bash
cat >> .env <<'EOF'
LUCENT_OPS_API_KEY=hs_...                        # from mint-key
LUCENT_OPS_CONSOLE_TOKEN=choose-a-passphrase    # you type this to sign in
LUCENT_OPS_CONSOLE_SECRET=<random>              # keeps sessions across restarts
LUCENT_OPS_PORT=8900
EOF
```

Generate the last two with `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`.

**3. Start it.**

```bash
docker-compose --profile ops-console up -d --build ops-console
```

Then open <http://127.0.0.1:8900> and enter `LUCENT_OPS_CONSOLE_TOKEN`.

The console carries `restart: unless-stopped`, so it comes back after a reboot
and after `docker restart`. It is behind a compose **profile**, which means a
plain `docker-compose up -d` will not start it — that is deliberate, so the
operator tool is never running on an instance where nobody asked for it. Add the
profile whenever you want it.

### Why it is a separate service

The console owns **no database connection and no Lucent imports**. It is an HTTP
client of the hyperadmin API, using the same endpoints an external signup site
would use. Three things follow:

1. Anything the console can do, your hosted management site can do — so building
   the console is a rehearsal for building that site.
2. A bug in the console cannot corrupt data; the server's authorization rules
   always apply.
3. It can be restarted, replaced, or deleted without touching Lucent.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `LUCENT_OPS_API_KEY` | — | **Required.** Hyperadmin API key `mint-key` gave you |
| `LUCENT_OPS_CONSOLE_TOKEN` | — | **Required.** Passphrase that guards the console UI |
| `LUCENT_OPS_BASE_URL` | `$LUCENT_PUBLIC_URL` or `http://localhost:8766` | Lucent API base |
| `LUCENT_OPS_HOST` | `127.0.0.1` | Bind address |
| `LUCENT_OPS_PORT` | `8900` in compose | Port the console is published on |
| `LUCENT_OPS_CONSOLE_SECRET` | generated | Cookie signing; set it for stable sessions across restarts |
| `LUCENT_OPS_SECURE_COOKIES` | `false` | Set `true` behind HTTPS |
| `LUCENT_OPS_INSTANCE_LABEL` | `Local instance` | Shown in the header |

Two secrets with two jobs, on purpose: the API key is the console's identity
towards Lucent and never reaches the browser; the passphrase guards the UI. A
leaked passphrase does not leak the key, and rotating the key does not require
distributing a new passphrase.

The console refuses to start without both. A console that launches but cannot do
anything is worse than one that tells you which variable is missing.

### What it does

* **Organizations list** — search, filter by status, member/owner/key counts.
  The internal `__lucent_system__` organization is never listed.
* **New organization** — the `/provision` flow: name, first admin, role, optional
  password, a key field per provider (7 today, each labelled with the engine that
  will run it), and **all 82 operator-editable settings** grouped by their 15
  sections. Blank means "inherit the instance value", so a tenant can inherit
  everything and be configured later. There is no model picker: a new tenant has
  no models of its own, and its list comes from its own credentials once
  discovery runs. Shows the generated password and API key once.
* **Organization detail** — lifecycle (suspend/resume/rename), members (add,
  change role, reset password, mint key, remove), model providers (save or clear a
  per-tenant key, run discovery), models (grant a shared model into the tenant,
  enable/disable an owned one), runtime settings grouped by section with each
  value's source (`DB` / `env` / `default`) and a **lock** control per setting,
  recent activity, and a name-confirmed delete. Locking is how a plan tier is
  expressed: the tenant keeps its settings screen, but the settings you pinned
  render read-only with your stated reason.
* **Instance audit** — the operator trail, filterable by tenant and action.
* **Status** — reachability, key validity, and whether the key belongs to a
  hyperadmin.

### Safety properties

* Binds to `127.0.0.1` by default; the compose file publishes the port to
  loopback only. This is a local administration tool.
* Session cookie is HttpOnly + SameSite=Lax, HMAC-signed, with an expiry.
* Every state-changing request is CSRF-checked (double-submit), including login.
* Generated credentials live in memory for 120 seconds, keyed by an opaque
  reference, and are shown exactly once.
* The hyperadmin API key is never rendered into a template, cookie, or log line.

### Running it locally without Docker

```bash
LUCENT_OPS_API_KEY=hs_... \
LUCENT_OPS_CONSOLE_TOKEN=choose-a-passphrase \
LUCENT_OPS_BASE_URL=http://localhost:8766 \
lucent-ops-console
```

---

## 4. Operational notes

**Migrations.** 149 adds the unique organization-name index. It renames
pre-existing duplicates (`Name (1)`, …) instead of failing, so it is safe on an
instance that already accumulated collisions. Migrations run at server start;
to apply them manually:

```bash
docker exec lucent-server python -c 'asyncio.run(init_db(run_migrations=True))'
```

**Deleting a tenant.** Ten tables reference `organizations` with
`ON DELETE NO ACTION`, so a plain `DELETE` fails on any tenant that ever ran
work. `OrganizationRepository.delete` clears those tables first, in one
transaction. The list is kept in sync with `tests/conftest.py`, which fails loudly
if a new restrictive foreign key appears.

**Revoking the last hyperadmin** is refused by the CLI. An instance with no
operator has no way to create organizations, and no UI path back.

**Restoring access.** If the operator's key is lost, `mint-key` again. If the
operator account itself is gone, `grant --create --email ...` recreates it. Both
need shell access to the instance database, which is the intended trust boundary
for instance-level power.
