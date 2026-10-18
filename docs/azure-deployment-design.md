# Azure Deployment Design (Kubernetes optional)

**Status:** Implemented — the Terraform module under `deploy/terraform/modules/lucent`
(with a runnable `examples/basic` root) now realizes this design; `deploy/terraform/README.md`
is the authoritative account of what it does and does not do. · **Date:** 2026-10-17 (implementation status updated) · **Context:** beta-launch blocker

> This document was originally a design proposal. Most of it is now implemented.
> Where a claim below has moved on, it is annotated inline. Do not read the
> un-annotated body as a promise of current behaviour; read the module and its
> README. The sandbox backend described in §4.7 now exists in the application
> (ACI container groups), but it has **not** been verified against a live Azure
> subscription — exec reliability and private ACR pulls remain unverified.

## 1. Goal

Deploy Lucent to Azure using **native managed services** — no Kubernetes required — provisioned
end-to-end with **Terraform**. AKS and the Helm chart remain a supported alternative for
operators who want them; they must not be a prerequisite.

Security and cost efficiency are the constraints that shape every choice below.

## 2. Recommended mapping

| Role | Azure service | Why |
|---|---|---|
| Web / API | **Container Apps**, external ingress, min replicas 0–1 | Scale-to-zero; no single-HTTP-port limit (App Service allows one); free managed certificate; private-endpoint story; idle-rate billing |
| Daemon controller | **Container Apps app with ingress disabled, `minReplicas: 1`** | Pure background process; pays the cheaper *idle* CPU rate; **must** pin min replicas 1 (see §2.1) |
| Per-org workers | **Container Apps Jobs**, manual trigger | Programmatic start via managed identity, per-execution template override, execution history, active-rate billing with no idle charge and no 60 s minimum |
| Task sandboxes | **Azure Container Instances container groups** | A group is pod-like: several containers share one network namespace, which is exactly what the trusted MCP-bridge sidecar needs (§4.7) |
| Database | **PostgreSQL Flexible Server**, VNet-integrated, Entra ID auth | Managed, PITR backups; pre-seed extensions and the `lucent_daemon` role (§4.3) |
| Registry | **Azure Container Registry**, Premium | Private link / private endpoints and disabling public network access are **Premium-only**; Basic (~$5/mo) and Standard cannot be made private. The module defaults to Premium (~$50/mo) to keep the private-registry posture |
| Secrets | **Key Vault** referenced by Container Apps secret refs via managed identity | Keeps secrets out of env without implementing an in-app provider (§4.6) |
| Logs | **Log Analytics** (Basic Logs for job output) | Job logs are not returned by the executions API; this is where worker output lands |
| Ingress hardening | **Skip Front Door / App Gateway for beta** | Container Apps ingress + free managed cert already gives TLS. Front Door Standard +$35/mo, App Gateway WAF_v2 ~$180–250+/mo — add only when WAF or global edge is genuinely required |

### 2.1 Two Container Apps gotchas that bite

- **A no-ingress app with no scale rule scales to zero and can never come back.** The controller
  has no inbound traffic to wake it. Pin `minReplicas = 1`.
- **A Job's `start` body replaces the entire execution template, not a patch.** Fetch the job,
  mutate only the env/args you need, resend.

### 2.2 Why not App Service

App Service is the obvious "PaaS" answer and we should say why it loses here. It cannot scale to
zero (always-on plan ≥ ~$13/mo), exposes one HTTP port (Lucent serves MCP, REST and the UI on one
port, so that is survivable — but it leaves no room), and its always-on requirement makes it a poor
fit for a mostly-idle controller. Container Apps is cheaper at idle, scale-to-zero capable, and has
a native job primitive we need anyway.

### 2.3 Why not AKS

The Helm chart now genuinely deploys the controller, so AKS is a real option — but it carries a
cluster to operate, upgrade and pay for. For a beta it is overhead without a matching benefit. Keep
it documented as the escape hatch, not the default.

## 3. Process topology

Four process classes; the platform must host all four:

- **Web/API** — HTTP, port 8766, N replicas behind ingress.
- **Controller** — no HTTP at all, singleton (two controllers duplicate worker supervision), and it
  must be able to *create other workloads*.
- **Workers** — ephemeral, one per org, minutes-to-hours, spawned and reaped by the controller.
- **Sandboxes** — ephemeral per task, untrusted-ish code.

The controller and the web server are separate console scripts with no combined mode. They stay
separate apps.

## 4. What must change in the application

This is the real work. The Azure services are the easy part.

### 4.1 Non-interactive bootstrap — **launch blocker**

First run is browser-only. `/setup` is the sole caller of `create_initial_user`
(`web/routes/auth.py`), it needs CSRF and a password, and it requires picking a model — but
migration 088 disables all seed models, so **a fresh database with no provider credentials cannot
complete setup at all**. Terraform cannot fill in a web form.

Needs: a CLI or env-driven bootstrap (create the first user + org, set the signing secret, select a
model) that a provisioning Job can run idempotently. Without it, every deployment is a manual
ceremony and a hosted beta is not repeatable.

**Implemented:** `src/lucent/bootstrap.py` provides `python -m lucent.bootstrap`, and the Terraform
module's gated `app_bootstrap` job runs it (after the database provisioning job). It stays off by
default because it creates a real owner account and holds its password in Terraform state.

### 4.2 Migrations must not race — **launch blocker for >1 replica**

Migrations run on server boot with **no advisory lock** — the runner reads the applied set and then
applies it. Two replicas starting together is a TOCTOU: the loser hits `DuplicateTable` and
**aborts startup**. Verified: `src/lucent/db/pool.py` contains no `advisory`/`LOCK`/`ON CONFLICT`.

Fix: take a `pg_advisory_lock` around the runner, or run migrations as a dedicated pre-deploy Job
and start the app with migrations disabled. Note `docs/kubernetes-deployment.md` tells operators to
run `python -m lucent.migrate` — **that module now exists** (added with this work); it reuses the
same runner and the runner now holds a session-level advisory lock, so the race is closed.

### 4.3 Database provisioning is out-of-band today

Nothing in the migrations creates what the app needs:

- **Extensions** `pg_trgm` (mandatory — trigram search) and `uuid-ossp` (legacy
  `uuid_generate_v4`). On Flexible Server, `CREATE EXTENSION` requires `azure_pg_admin` and the
  extension must be allowlisted via the `azure.extensions` parameter. `gen_random_uuid()` is core
  and avoids the second extension entirely — worth migrating to.
- **The `lucent_daemon` role** is created only by `docker/postgres-init.sh`, never by a migration;
  migrations merely `GRANT` to it behind `IF EXISTS` guards. It must be pre-created.
- `ALTER DATABASE … SET pg_trgm.similarity_threshold` and `ALTER DEFAULT PRIVILEGES FOR ROLE lucent`
  (which needs membership in that role) are run by the init script, not the app.

**Implemented:** the Terraform module's database provisioning job (`bootstrap.tf`) does all of the
above — creates the two roles, allowlists and creates the extension, sets the threshold, applies the
default privileges — because the database has no public endpoint (so a workstation cannot reach it)
and Terraform has no resource for any of it. It is created by Terraform and started by the operator
once.

### 4.4 File storage — **implemented with an Azure Blob provider**

The original gap: `LUCENT_FILE_STORAGE_PATH` defaults to local disk and
`LocalFileStorageProvider` was the only implementation. On ephemeral disk,
uploads vanish on restart and are invisible to a second replica. The in-process
LangChain file/shell tools have the same problem (they operate on the process
CWD).

**Implemented:** `src/lucent/storage/providers.py` holds the provider registry
(the local provider stays registered for rows that name it), and
`src/lucent/storage/azure_blob.py` adds `AzureBlobStorageProvider` (`azure`,
managed identity only — no key, connection string or SAS). The Terraform module
creates a private blob account and container and injects
`LUCENT_FILE_STORAGE_PROVIDER`, `LUCENT_AZURE_STORAGE_ACCOUNT_URL`,
`LUCENT_AZURE_STORAGE_CONTAINER` and `AZURE_CLIENT_ID` into the web app; the
account has shared-key access disabled, anonymous access denied, a private
endpoint and a default-deny firewall. See `deploy/terraform/README.md`,
"File storage on Azure Blob".

**Deployment precondition:** the image must be built from a revision that
contains the provider selector. An older image **ignores**
`LUCENT_FILE_STORAGE_PROVIDER` — it starts, passes `/api/health`, and fails at
the first file operation; if its local path is writable it silently writes to
the replica's ephemeral disk. In images that contain the selector the registry
fails closed on an unavailable provider. The app-side startup assertion that
makes the old-image case fail fast is a separate work item; Terraform cannot
check the image revision.

### 4.5 The signing secret must be provisioned, not generated

Signing-secret init is a non-atomic get-or-create. Two replicas booting on a fresh database can
store *different* secrets, after which cookies signed by one are rejected by the other. Provision it
deterministically (Key Vault → Container Apps secret) rather than letting the app invent it.

**Implemented:** the module provisions `LUCENT_SIGNING_SECRET` (and `LUCENT_SECRET_KEY`) into Key
Vault and injects them as Container Apps secret references.

### 4.6 Secrets can be solved without touching the app

`src/lucent/secrets/azure.py` is a `NotImplementedError` stub and is not registered. Rather than
implement it, inject the two or three secrets the app needs (`DATABASE_URL`, `LUCENT_SECRET_KEY`,
`LUCENT_SIGNING_SECRET`, license key, provider tokens) as **Container Apps secret references backed
by Key Vault** with a managed identity. That keeps secrets out of plain environment variables at the
platform layer, with no application change. Implementing a real Key Vault *provider* is only needed
if tenant-supplied secrets should also live there.

**Implemented:** the module injects `DATABASE_URL`, `DAEMON_DATABASE_URL`, `LUCENT_SECRET_KEY`,
`LUCENT_SIGNING_SECRET` and the optional provider/license tokens as Key Vault-backed Container Apps
secret references read with a managed identity, so no literal secret value appears in a container
spec. `src/lucent/secrets/azure.py` remains an unimplemented stub and is still not needed for this.

### 4.7 Sandboxes need a new backend, and the bridge constrains the choice

`SandboxBackend` requires `create`, `exec`, `read_file`, `write_file`, `list_files`, `get`,
`rebind_bridge`, `stop`, `destroy`, `list_all`.

The constraint: the trusted MCP bridge runs **in the sandbox's network namespace** so it can answer
on `127.0.0.1` inside the sandbox (Docker `network_mode: container:<id>`; on Kubernetes a pod
sidecar). That is a pod-like co-scheduling requirement, and it rules out any platform that only runs
a single container per unit:

- **ACI container groups** express it directly — multiple containers sharing one netns. This is the
  conservative, faithful mapping.
- **Container Apps Sandboxes** (`Microsoft.App/SandboxGroups`) are purpose-built for untrusted code
  and look like the better long-term home, but GA status and billing meters are **unconfirmed** —
  verify before designing around it. They would likely need the bridge in-process instead of as a
  sidecar.

Either way, `rebind_bridge` semantics matter: a reused sandbox must not attribute events to the
previous task.

**Implemented — unverified on a live subscription.**
`src/lucent/sandbox/aci_backend.py` adds `AzureContainerInstancesBackend`,
selected with `LUCENT_SANDBOX_BACKEND=aci`. A sandbox is a private ACI
*container group*: the user image and the trusted MCP bridge as two containers
sharing one network namespace, with an `emptyDir` mounted at `/workspace`.
Groups are deployed into the delegated subnet named by
`LUCENT_SANDBOX_ACI_SUBNET_ID`, have a private IP and no public endpoint, and
are found again by their Lucent sandbox-id tag, so lifecycle survives a process
restart. `rebind_bridge` returns `False` (group containers are immutable), so a
reusing task gets a fresh sandbox rather than inheriting the previous task's
bridge identity. `stop` maps to destroy: ACI cannot suspend a group and resume
it, so the workspace is discarded and the manager records and reports the
sandbox as `destroyed` rather than `stopped`.

What the implementation covers:

- Lifecycle (`create`, `get`, `stop`, `destroy`, `list_all`) over resource-group
  container groups tagged with the Lucent sandbox id.
- Exec through ACI's terminal-emulated WebSocket (`containers.execute_command`),
  with an exit-code sentinel appended to the command. Consequences are
  deliberate and documented in the module: stderr is merged into stdout, large
  or binary output is unreliable, a timeout abandons rather than kills the
  process, and a command that replaces or kills its shell loses the sentinel.
- File access through base64 over exec, chunked below Linux's `MAX_ARG_STRLEN`;
  **writes are confined to `/workspace`**, matching Docker.
- Setup commands, repository clone with the git-askpass helper staged under
  `/workspace/.lucent`, the bridge sidecar, and the bridge health probe.
- The environment split is preserved: the task-scoped key and bridge routing
  reach only the sidecar (the key as an ACI `secureValue`); the user container
  receives `primary_env` only.
- Networking: private group with no public IP or DNS label, subnet deployment,
  and `*.azurecr.io` pulls through a dedicated user-assigned identity.

**Unverified** (no live-subscription run yet):

- Exec reliability through the WebSocket/PTY path, especially with large
  output, concurrent sessions, and commands that do not reach the sentinel.
- A private ACR pull with the mandated dual identity attachment, including the
  `InvalidImageRegistryIdentity` failure mode when the group identity is
  missing.
- Resource clamping and readiness polling against real regional ACI limits.

Residual risks:

- **Registry-wide `AcrPull`.** The dedicated pull identity is granted `AcrPull`
  on the registry, not on a single repository, so a token for it can pull every
  image in that registry.
- **Identity reachable from sandbox code.** Untrusted code in *either* container
  of the group can reach IMDS and obtain a token for the group identity. The
  mitigation is scope, not secrecy: the identity must be dedicated and hold
  only `AcrPull` on the registry. It must never be the app, controller, or
  provisioner identity.
- **Subnet-wide egress.** ACI has no per-group NetworkPolicy. `allowlist` fails
  closed unless the operator sets `LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS=1` to
  acknowledge that the subnet NSG — not the sandbox — controls egress, and
  `none` is only as isolated as that NSG.
- **Non-root requirement.** Standard ACI rejects `runAsUser`, capabilities, and
  seccomp; the sandbox image must ship a non-root `USER` (the repo's
  `sandbox-images/Dockerfile.base` does), because Lucent can only set
  `privileged: false`.

The Container Apps Sandboxes path remains unconfirmed and unused; nothing in
the Terraform module creates sandbox infrastructure.

### 4.8 Built-in definitions are not in the image — **silent feature loss**

Built-in skills and agents are read from `/app/.github/...`, which the image does **not** contain;
Compose bind-mounts them read-only. Any deployment without that mount ships with **no built-in
skills or agents**, silently. Fix the Dockerfile to copy them, or make the sync tolerant and loud.

### 4.9 Operational notes

- **LISTEN/NOTIFY is load-bearing** (task dispatch and live UI events) and is session-scoped. Do not
  put those connections behind Azure's built-in PgBouncer in transaction mode, or set it to session
  mode. Symptoms otherwise are baffling (tasks never dispatch).
- **Connection limits**: Flexible Server `max_connections` is ~50 on B1ms (2 GiB). That is tight for
  N replicas plus a warm worker pool — either use PgBouncer for the app paths or size up.
- **Migrations and the app must run as the table owner** for `ALTER DEFAULT PRIVILEGES FOR ROLE lucent`.
- **The controller is a singleton.** Do not autoscale it.
- **`/api/health` is liveness only** — it returns a static `{"status":"healthy"}` with no database
  check, and there is no readiness endpoint. A replica can pass the probe while still migrating.
  Worth adding a readiness check before we run more than one replica.
- Per-replica in-memory state (rate limiter, settings cache, model registry cache) means admin edits
  and rate limits are per-replica. Acceptable at beta scale with 1–2 replicas; document it.

## 5. Terraform shape

**Implementing this.** This section describes the shape; the module under
`deploy/terraform/modules/lucent` is the implementation of it. A deployment creates:

**Networking** — VNet, a delegated subnet for Postgres VNet integration, private DNS zones, private
endpoints for Key Vault, ACR and Blob Storage, and the Container Apps environment subnet. The
fixed-name private-link zones can be adopted instead of created (`create_*_private_dns_zone` /
`existing_*_private_dns_zone_id`) so the module composes into a VNet that already owns one.

**Database** — `postgresql_flexible_server` (16+), database, `azure.extensions` allowlisting
(`pg_trgm`, optionally `uuid-ossp`), firewall/private access, Entra ID admin. Then a **bootstrap
step** — a psql job or a Container Apps Job — to create the `lucent_daemon` role, set
`pg_trgm.similarity_threshold`, and let the app create its schema.

**Compute** — one Container Apps environment; a web app (ingress, min 0–1); a controller app (no
ingress, min 1); a Job for workers; optionally a Job for the sandbox path.

**Identities and RBAC** — a managed identity for the controller with a **narrow custom role** that
can start the workers Job (`Microsoft.App/jobs/start/action`) and read its executions. Do not grant
the wildcard built-ins: `start` implies access to the job's secrets.

**Registry and images** — ACR; the repo builds nothing and pushes nowhere today, so image
build/push has to come from CI or a Terraform `null_resource` using `az acr build`.

**Secrets** — Key Vault with the app secrets; Container Apps secret refs wired via managed identity.

**Observability** — Log Analytics workspace; Container Apps environment wired to it.

### 5.1 Bootstrap order (Terraform can express this)

1. Network + Postgres + ACR + Key Vault + Log Analytics.
2. Build and push the image (CI or local-exec).
3. A **migration/bootstrap Job** that waits for the DB, ensures extensions/role/thresholds, runs
   migrations, and — §4.1 is now implemented — creates the first user non-interactively.
4. Deploy the web app and the controller; the controller's identity gets Job-start permission.

## 6. Security posture

As implemented (see `deploy/terraform/README.md` for the full account):

- Private networking for the database, Key Vault, ACR and Blob Storage; no public
  DB endpoint. The database is VNet-integrated with
  `public_network_access_enabled = false`. The storage account has shared-key
  access disabled, anonymous blob access disallowed, a private endpoint and an
  inline default-deny firewall, so the public endpoint answers nothing. Its soft
  delete is on by default, with the erasure consequences documented in the
  Terraform README.
- **TLS everywhere, including internally.** Container Apps ingress sets
  `allow_insecure_connections = false` (HTTP is only a redirect), and the daemon
  and workers reach the web app's MCP endpoint over `https://<fqdn>/mcp` rather
  than cleartext. `DATABASE_URL` carries an explicit `sslmode=require`.
- **The registry is private, and that forces Premium.** A private endpoint and
  disabling public network access are Premium-only ACR features; Basic and
  Standard support neither. The module defaults to Premium and refuses, with a
  fail-fast precondition, a private registry on a SKU that cannot provide one.
  This is the one place where "private" has an unavoidable cost (~$50/month vs
  ~$5 for Basic).
- **Key Vault is private by default.** `public_network_access_enabled = false`
  and the trusted-services bypass is `None`, per Microsoft's guidance for a
  private-endpoint vault. Consequence to plan for: Terraform writes the secrets
  into the vault and re-reads their values from the data plane on every
  plan/refresh, so **any Terraform run that touches the secrets must run from
  inside the VNet** (or with public access temporarily re-enabled). IP allowlisting
  does not help while public access is disabled — IP rules are ignored.
- Managed identity, not stored credentials, for ACR pulls, Key Vault reads and
  Job starts. Image pulls use `AcrPull`; the registry has `admin_enabled = false`.
- Secrets via Key Vault references — never plain env in the container spec.
- **Secrets are scoped per secret, not per vault.** `Key Vault Secrets User` is
  granted on each secret's own resource ID, so an identity reads only the secrets
  it is handed. The raw PostgreSQL passwords and the bootstrap owner password are
  readable by a dedicated `provisioner` identity alone; the internet-facing web
  app and the controller cannot read them.
- **The controller's RBAC is a custom role**, not a built-in. The role
  *definition* is assignable at the resource-group scope (Microsoft's guidance —
  a resource-instance `AssignableScopes` is "not recommended"); the *assignment*,
  which is what governs access, is scoped to the single workers Job. The four
  actions avoid the built-in wildcard that also matches `listSecrets`.
- Run the app and the daemon as the least-privilege Postgres roles we already have
  (`lucent_daemon` for the daemon).
- **Known gap — container hardening is not enforced at the platform layer.**
  Container Apps exposes no security context to the provider
  (`azurerm_container_app`'s container block has no such attribute), so
  `runAsNonRoot`, capability drops and seccomp/AppArmor profiles are not
  expressible here. Non-root comes only from the image's own `USER`. Kubernetes
  (AKS) remains the route to enforcing those controls.
- Remember the tenancy boundary is application-layer: RLS was removed, so the container hosting
  tenant data is the only structural bound. Keeping one instance per tenant (or a strong
  app-layer correctness bar) matters here, and this design does not change that.

## 7. Cost sketch (small beta, monthly, USD, East/Central US list)

| Component | Lean, secure | Hardened, private networking |
|---|---|---|
| Web app (scale-to-zero) | $0–24 | $0–24 |
| Controller (min 1, idle rate) | ~$12 | ~$12 |
| Worker jobs (low volume) | $5–15 | $5–15 |
| Postgres Flexible Server | ~$17–20 (B1ms) | ~$35–70 (B2s/GP, VNet) |
| ACR Premium (private registry) | $50 | $50 |
| Key Vault | ~$0.10 | ~$0.10 |
| Log Analytics | $0 (5 GB free) | $0–10 |
| Private endpoints + DNS + Dedicated Plan charge | — | ~$30–60+ |
| Front Door (only if WAF required) | — | +$35 (Standard) |
| **Total** | **~$85–120** | **~$165–295** |

Dominant lines: Postgres compute, the always-on controller, and private-networking fees.
Scale-to-zero is the single biggest lever — it applies to the web app and every job.

The ACR line moved from Basic to Premium when the module was implemented: a private endpoint and
disabling public network access — both part of the private-registry posture this document describes —
are Premium-only ACR features. Basic is ~$5/mo but cannot be private, so the earlier ~$40–75 lean
total was too low by about $45. This is the one line where the private posture is not free.

Microsoft does **not** recommend the burstable B-series for production; a production-acceptable
beta arguably starts at B2s or General Purpose, which moves the DB line up.

## 8. Must-verify before committing

1. **Container Apps Sandboxes**: GA/preview status and billing meters are
   unconfirmed. **Still open.** Not used — nothing in the module creates one.
   The ACI container-group backend is implemented (unverified live; §4.7), so
   ACI remains the conservative default. Confirm Container Apps Sandboxes
   before designing around them.
2. **Dedicated Plan Management** charge triggered by Container Apps private
   endpoints — exact rate. **Resolved.** Confirmed against Microsoft's billing
   documentation at ~$0.10/hour (~$73/month). The module deliberately triggers
   none of the meters that incur it (no dedicated workload profile, no private
   endpoint into Container Apps, no planned maintenance), so it is avoided rather
   than paid. Re-check the exact rate against the pricing calculator for the
   target region.
3. **Container Apps Consumption** rates and the free grant, against the Azure
   Pricing Calculator for the target region (pricing pages render figures
   dynamically). **Still open** — nothing in the module depends on the figure, but
   the cost sketches inherit numbers read from pages that render dynamically.
4. Whether a Container Apps **Job** can run more than one container (needed only
   if the sandbox bridge must be a sidecar rather than in-process — this is what
   decides ACI vs Container Apps for sandboxes). **Resolved: yes.** Microsoft's
   jobs documentation states a job runs a pod of containers sharing a network
   namespace, and a Microsoft engineer confirmed it (azure-container-apps issue
   #915). So the bridge can be a job sidecar and a multi-container job is not
   forced to ACI. Caveats: the CLI supports only a single `--image`, so
   multi-container jobs need the API/Terraform/YAML, and resource limits apply to
   all containers combined. The workers job the module creates is still
   single-container — multi-container is a sandbox concern.
5. Flexible Server `max_connections` on the chosen tier versus the planned
   replica and worker counts. **Flagged, not solved.** The module exposes
   `postgres_max_connections` (default null); the better answers are PgBouncer in
   **session** mode or a larger SKU.

## 9. Suggested phasing

1. **Unblock provisioning** (§4.1 non-interactive bootstrap, §4.2 migration locking, §4.3 DB
   bootstrap). These are app-side and are prerequisites for *any* automated deployment, Kubernetes
   included.
2. **Terraform foundations**: network, Postgres, ACR, Key Vault, Log Analytics, images.
3. **Container Apps**: web app, controller, managed identity + Job RBAC, secrets via Key Vault.
4. **Azure worker launcher**: `LUCENT_WORKER_LAUNCHER=azure` implementing `WorkerLauncher` against
   Container Apps Jobs, mirroring the Kubernetes backend's contract.
5. **File storage**: Azure Blob provider (§4.4). **Implemented** — the provider,
   the private account/container and the injected configuration are in place; the
   image must be built from a revision that contains them.
6. **Sandboxes**: ACI container-group backend. **Implemented** (unverified on a
   live subscription; §4.7); Container Apps Sandboxes remain unconfirmed.
7. Harden: private endpoints, WAF if required, readiness endpoint.
