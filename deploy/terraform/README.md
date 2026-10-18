# Lucent on Azure — Terraform

Native managed services, no Kubernetes. This is the Terraform implementation of
`docs/azure-deployment-design.md`: Container Apps for the web/API and the daemon
controller, a Container Apps job for per-organization workers, VNet-integrated
PostgreSQL Flexible Server, Container Registry, Key Vault, Log Analytics, an
Azure Blob Storage account for user files, and least-privilege managed-identity
RBAC. An optional Azure Container Instances path for task sandboxes (design doc
§4.7) is included behind `sandbox_backend = "aci"`; the default `"docker"` plans
none of it.

```text
deploy/terraform/
├── modules/lucent/          the reusable module
│   ├── versions.tf          provider constraints
│   ├── variables.tf         every knob, typed and described
│   ├── locals.tf            naming, generated secrets, connection strings, sandbox env
│   ├── network.tf           VNet, subnets, private DNS zones, sandbox subnet/NSG/NAT
│   ├── monitoring.tf        Log Analytics, sandbox diagnostic-settings policy
│   ├── registry.tf          ACR + private endpoint
│   ├── keyvault.tf          Key Vault, private endpoint, application secrets
│   ├── storage.tf           blob storage for user files, private endpoint, network rules
│   ├── database.tf          PostgreSQL Flexible Server, database, parameters, Entra admin
│   ├── identity.tf          user-assigned managed identities (three, plus the sandbox pull identity)
│   ├── rbac.tf              role assignments + the custom job-operator and sandbox roles
│   ├── container_apps.tf    environment, web app, controller app, workers job
│   ├── bootstrap.tf         database provisioning job, gated app-bootstrap job
│   └── outputs.tf           operator-facing outputs
└── examples/basic/          a runnable root configuration
    ├── versions.tf  main.tf  variables.tf  outputs.tf
    └── terraform.tfvars.example
```

## Why a module plus an example

A flat layout would be fewer files and quicker to read. It was rejected because
this deployment has to land next to things an operator already has — an existing
VNet, an existing Log Analytics workspace, an existing registry, an existing
resource group, a specific region — and a flat root module would have to be
copied and edited to do that. The module instead accepts either "create it" or
"here is the resource ID" for each of those (`create_vnet`, `create_resource_group`,
`create_log_analytics_workspace`, `create_container_registry`, and the matching
`existing_*` inputs). `examples/basic` is kept deliberately thin so it is obvious
which values a real deployment must supply.

## Prerequisites

| Requirement | Notes |
|---|---|
| Terraform | 1.6 or later. Validated with 1.9.8. |
| azurerm provider | `>= 5.10.0, < 6.0.0`. Validated against 5.10.0. |
| Azure subscription | With the `Microsoft.App`, `Microsoft.DBforPostgreSQL`, `Microsoft.ContainerRegistry`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`, `Microsoft.Network`, `Microsoft.Storage` and `Microsoft.ManagedIdentity` resource providers registered. Add `Microsoft.ContainerInstance` when `sandbox_backend = "aci"`. |
| Permissions | Owner (or Contributor + User Access Administrator) on the subscription or target resource group — the module creates custom role definitions and role assignments. |
| Storage management permission | Contributor (or Owner, or Storage Account Contributor) covers the file storage account: the module's own container resource is a **control-plane** resource and needs the ordinary `Microsoft.Storage/storageAccounts/blobServices/containers/write` action. A blob **data-plane** role (`Storage Blob Data Contributor`) is a *different* grant, needed only by whoever reads and writes file *contents* — the app (the module grants it) and any operator tooling. |
| Key Vault data access | With `rbac_authorization_enabled = true`, Contributor is not enough to write secrets. Whoever runs `apply` needs **Key Vault Secrets Officer** on the vault, or Owner (which includes data actions). |
| Quota | A spare `/23` of address space for the Container Apps environment, and PostgreSQL Flexible Server capacity in the chosen region. |

## Before you apply — the image

**This repository builds nothing and pushes nowhere.** Terraform will not create
an image for you, and the Container Apps will not start until `var.image` points
at an image that actually exists in a registry the environment can reach.

The module creates its own Azure Container Registry, which introduces a
chicken-and-egg problem: the registry's login server is `<name_prefix><environment><4 random
characters>.azurecr.io`, and that random suffix does not exist until the registry
does. Pick one of these:

1. **Two-phase apply (simplest).** Create just the registry, push, then apply
   everything else. Run this from the **repository root** — the `Dockerfile` and
   `.dockerignore` live there, so that is the build context `az acr build` needs
   (the `-chdir` flags point Terraform at the example, which contains no
   Dockerfile):

   ```bash
   terraform -chdir=deploy/terraform/examples/basic apply -target=module.lucent.azurerm_container_registry.this
   REG=$(terraform -chdir=deploy/terraform/examples/basic output -raw container_registry_login_server)
   az acr build --registry "${REG%%.*}" --image lucent:0.4.0 .
   terraform -chdir=deploy/terraform/examples/basic apply -var "image=${REG}/lucent:0.4.0"
   ```

   Both phases have the same network requirement, and it is not optional with
   the defaults: the registry has public access disabled and the Key Vault is
   private, so **the first phase must run from inside the VNet** (or a host with
   private connectivity), not from an ordinary workstation. From a workstation,
   `az acr build` cannot reach the registry and `apply` cannot write the secrets
   to the vault. If you deliberately want to run from outside, you must open
   both explicitly — `container_registry_public_network_access_enabled = true`
   for the push (turn it back off afterwards) and
   `key_vault_public_network_access_enabled = true` plus
   `key_vault_allowed_ip_ranges` for the vault. See
   [Writing secrets into a private vault](#writing-secrets-into-a-private-vault).

2. **Bring your own registry.** Set `create_container_registry = false` along with
   `existing_container_registry_id` and `existing_container_registry_login_server`,
   and point `image` at it.

3. **Set `container_registry_name`** so the name (and therefore the login server)
   is predictable before the first apply.

### A consequence of the private registry

`container_registry_public_network_access_enabled` defaults to `false`, which is
the design doc's posture but has a practical cost: with public access off, **you
cannot push an image from your laptop.** `az acr build` and `docker push` both
need to reach the registry. Your options are to push from a host inside the VNet,
to temporarily set the variable to `true` for the first push and turn it back
off, or to point at an existing registry. This is called out again in
[What you must still do by hand](#what-you-still-must-do-by-hand).

### The sandbox bridge image is a second image you must build

When `sandbox_backend = "aci"`, the application runs a trusted MCP bridge
sidecar **inside each sandbox's network namespace**, and ACI pulls that sidecar's
image from a registry. The application's default reference is
`lucent-sandbox:base`; this module sets `LUCENT_SANDBOX_BRIDGE_IMAGE` to
`<registry login server>/lucent-sandbox:base` unless you override
`sandbox_bridge_image`.

**Nothing in this repository builds or pushes it.** Terraform will not do it
either. Before enabling the ACI backend:

1. Build the base sandbox image from `sandbox-images/Dockerfile.base` and push
   it to the registry as `lucent-sandbox:base`. The Dockerfile is named
   `Dockerfile.base`, so pass `-f` explicitly:

   ```bash
   REG=$(terraform -chdir=deploy/terraform/examples/basic output -raw container_registry_login_server)
   az acr build \
     --registry "${REG%%.*}" \
     --image lucent-sandbox:base \
     -f sandbox-images/Dockerfile.base \
     sandbox-images
   ```

   The bridge source is injected into the container at run time (the image does
   not need the Lucent package); what it needs is a Python 3 interpreter, which
   this image provides.

2. If you use the GitHub MCP variant, build `sandbox-images/Dockerfile.github-mcp`
   on top of the base image and push it under its own tag, then set
   `sandbox_bridge_image` to that full reference.

3. ACI pulls with the sandbox **pull identity**, which this module grants
   `AcrPull` on the deployment's registry. If you push the image to a different
   registry, grant that identity `AcrPull` there yourself — Terraform only
   grants it on this module's registry. ACI cannot pull from a non-ACR private
   registry at all; a public registry works without a grant.

The pull happens when the application creates a container group, so the image
must exist **before the first sandbox task runs**. A missing image is a runtime
`ImagePullBackOff`-style failure in the group, not a Terraform error.

### The registry SKU is Premium by default, and it has to be
Both halves of the private-registry posture — a private endpoint and disabling
public network access — are **Premium-only** features of Azure Container
Registry. Basic and Standard registries support neither; Azure rejects a private
endpoint on them, and will not let public access be disabled. So the defaults
(`container_registry_sku = "Premium"`, a private endpoint, public access
disabled) are a matched set, and the module's earlier combination of a Basic SKU
with those two settings could not have applied.

What that means in practice:

- The honest cost is **~$50/month for Premium versus ~$5 for Basic.** This is the
  price of a private registry; there is no cheaper SKU that can be made private.
- To run a cheaper public registry instead, set
  `container_registry_private_endpoint_enabled = false` **and**
  `container_registry_public_network_access_enabled = true` with
  `container_registry_sku = "Basic"` (or `"Standard"`). That is a real,
  supported configuration — it is just not a private one.
- The module fails fast if you combine them wrongly: requesting a private
  endpoint or disabling public access on a non-Premium SKU stops the apply with a
  precondition that names the variable to change, rather than letting Azure
  reject the deployment later.

### Writing secrets into a private vault

The Key Vault is private by default: `public_network_access_enabled = false` and
the trusted-services bypass is `None`, so the vault is reachable only through its
private endpoint. This is what "private Key Vault" means, and it is Microsoft's
guidance for a private-endpoint vault — but it has a sharp edge that will bite an
operator on the very first apply.

**Terraform itself writes the application secrets into the vault, and Terraform
runs from wherever you run it.** With public access disabled, a run from outside
the VNet fails the moment it touches a `azurerm_key_vault_secret`, with an HTTP
**403** (`ForbiddenByConnection` from the vault's data plane).

And this is not a first-apply-only problem. The `azurerm_key_vault_secret`
resource **re-reads every secret value from the vault's data plane on every
`plan`/`refresh`** — the ARM control plane does not expose enough to detect drift
— so once the vault is private, **any Terraform run from outside the VNet that
touches the secrets fails**, including later plans. (This is a known provider
behaviour, not specific to this module; `-refresh=false` skips the read at the
cost of not detecting drift.)

The important part — and the part that is easy to get wrong — is that **adding
your workstation's IP to `key_vault_allowed_ip_ranges` does not fix this.** While
public network access is disabled there is no public endpoint for an IP rule to
admit; IP rules are simply ignored. The only routes in are private ones. So pick
one of:

1. **Run Terraform from inside the VNet** (or a host with private connectivity —
   a jump box, a VPN/ExpressRoute-connected network, or a CI runner with a
   private endpoint to the vault). This is the intended path and the one that
   keeps the vault fully private.
2. **Keep public access enabled while you actively manage secrets, and disable it
   when you are done.** Set `key_vault_public_network_access_enabled = true` and
   list your host in `key_vault_allowed_ip_ranges`, run the applies that create
   and update secrets, then set it back to `false` and apply once more to close
   the public endpoint. Because every later plan re-reads the secrets, you must
   re-open it again for any run that touches them — so this is a pragmatic option
   for an occasional change, not a steady state.
3. **Use `-refresh=false`** on the plan/apply when you know the secrets have not
   changed out of band. That skips the data-plane read, so a run from outside the
   VNet can complete without re-opening the vault.

The vault's private endpoint and the `privatelink.vaultcore.azure.net` zone are
created by the module, so the workloads themselves always reach the vault
privately regardless of which route you use to apply.

### Reaching the blob data plane

The file storage account's firewall is part of the account's own create: an
inline `network_rules` block on `azurerm_storage_account` sets
`default_action = "Deny"`, `bypass = ["None"]`, and no IP ranges unless you add
some. Two different problems are solved by that shape.

**There is no partial-apply window.** Azure's default for a new storage account
is public access with no firewall. A separate
`azurerm_storage_account_network_rules` resource is a second API call, so an
apply that fails between creating the account and applying the rules leaves the
account reachable from all networks until someone notices and re-runs. Setting
the block inline means the account is never created open. The provider's
documentation warns that configuring the rules both inline and via the standalone
resource causes spurious changes, so it is one or the other; this module uses the
inline block only.

**Why the container still works with Deny at create.** Azure's storage firewall
**"applies only to data plane operations"**; control-plane operations "aren't
subject to the restrictions specified in firewall rules". In **azurerm 5.10.0**,
the version this module pins, `azurerm_storage_container` is managed over the
**ARM control plane**. It takes its client from the provider's resource-manager
bundle (`Storage.ResourceManager.BlobContainers`, i.e.
`Microsoft.Storage/storageAccounts/blobServices/containers`), and the data-plane
packages it imports are used only to assemble the `url` attribute's string — no
data-plane request is made. So a Deny firewall in force from creation does not
block the container's create or its refresh.

That is a property of the pinned provider version, not a permanent promise. The
provider's own `storage_account` documentation still describes the older
data-plane behaviour — *"Terraform uses Shared Key Authorisation to provision
Storage Containers, Blobs and other items … when Shared Key Access is disabled,
you will need to enable the `storage_use_azuread` flag"* — so a future provider
could move the resource back. If that happens, the first apply fails loudly at
the container (a 403) rather than silently leaving the account open; re-check
this note before raising the provider bound in `versions.tf`.

Nothing needs to be added to `virtual_network_subnet_ids`: the firewall governs
the **public** endpoint only, and traffic arriving over a private endpoint is
admitted by the private link's own consent flow instead. So the applications
reach the account privately without any firewall exception.

**What the firewall does govern, and the routes in.** Everything that is
genuinely data-plane: Storage Explorer, `az storage … --auth-mode login`, AzCopy,
a backup or sync tool, any data-plane storage resource added to this configuration
later — and the container too, if a provider update moves that resource back to
the data plane. For those, in order of preference:

1. **Run them from inside the VNet** — the intended path, and what the private
   Key Vault already requires.
2. **Run them from a host in the storage account's own Azure region.** Azure
   documents that **IP network rules have no effect on requests that originate in
   the same region as the storage account** — a same-region client cannot be
   restricted by IP rules, which here works in your favour: it reaches the account
   whatever the firewall says. (Microsoft presents this as a limitation, not a
   feature, and this deployment inherits it.)
3. **List the host's public address** in `storage_allowed_ip_ranges`. This works
   from any region except the account's own (see 2). Two Azure limits apply: only
   public IPv4 ranges are accepted — no RFC 1918 addresses — and `/31` or `/32`
   prefixes are rejected, so give single addresses bare.
4. **Use `-refresh=false`** for a plan that cannot reach the account, which skips
   the read at the cost of not detecting drift.

`storage_network_default_action` is a variable rather than a fixed `Deny` so that
there is a fifth, less pleasant option: set it to `Allow` for one apply if you must
do something from somewhere that cannot reach the account at all, then set it back.
That is an escape hatch, not a posture — while it is `Allow`, the account's public
endpoint answers everyone.

**The provider flag.** `storage_use_azuread = true` is set on the `azurerm`
provider in `examples/basic`. The account denies shared-key access, so any storage
operation Terraform makes over the **data plane** must be authorized with Entra ID
rather than an account key, which is what this flag switches on — and it is what
the provider's own documentation asks for once shared-key access is off. With this
module as written, nothing Terraform does today is a data-plane call, so the flag
changes nothing yet; it matters the moment a data-plane resource or a data-plane
tool run is added, and it is the reason the file container is not managed with a
key. If you do add one, the identity needs a blob **data-plane** role
(`Storage Blob Data Contributor`, or Owner, which includes data actions) — a
management role such as Storage Account Contributor is enough for the control
plane and not for the data plane. `examples/basic` sets the flag; nothing in this
module can assert a provider argument, so a root configuration that omits it is
not caught at plan time.

## Quick start

```bash
cd deploy/terraform/examples/basic
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform apply
terraform output next_steps
```

`terraform output next_steps` repeats the manual steps below, so they are visible
from the CLI and not only from this file.

This quick start is only runnable end-to-end **from inside the VNet** with the
defaults: the registry and Key Vault are private, so a workstation can neither
push the image nor let `apply` write the secrets. The two-phase apply above and
[Writing secrets into a private vault](#writing-secrets-into-a-private-vault)
describe the reachability requirement and the explicit flags that relax it.

## What Terraform covers, honestly

This is the part of the design doc that is easiest to overstate, so it is split
out explicitly.

### Fully covered

VNet, three subnets, private DNS zones, private endpoints; PostgreSQL Flexible
Server (16) with VNet integration and no public endpoint; the `azure.extensions`
allowlist; the Entra ID administrator; Log Analytics; Container Registry + private
endpoint (Premium SKU); Key Vault + private endpoint with public network access
disabled; the Blob Storage account and its private file container, with shared-key
access disabled, public blob access denied, a private endpoint, a
denied-by-default firewall and soft delete; the Container Apps environment; the
web app; the controller app; the workers job; three managed identities and their
per-secret role assignments, including the custom job-operator role and the
container-scoped blob data role.

When `sandbox_backend = "aci"` it additionally covers the ACI-delegated sandbox
subnet, the dedicated sandbox resource group, the sandbox image-pull identity
and its `AcrPull` grant, the app identity's three narrow custom roles, the
subnet NSG and NAT gateway, the diagnostic-settings policy for container-group
logs, and the `LUCENT_SANDBOX_*` environment variables in the web app **and in
the controller/worker environment** — the dispatcher that creates task
sandboxes runs only in a per-org worker, so the web app alone would be the wrong
place for them. Container groups themselves are created by the application at
run time and are not Terraform resources.

### Covered only by the provisioning job

There is **no first-class Terraform resource** for any of the following, and
there is no way to avoid needing them:

| Needed | Why Terraform cannot do it |
|---|---|
| `CREATE ROLE lucent` / `lucent_daemon` | No role resource in the provider. Nothing in Lucent's migrations creates `lucent_daemon` either — `docker/postgres-init.sh` does, and the migrations only `GRANT` to it behind `IF EXISTS` guards, so on a fresh server every one of those grants silently no-ops. |
| `CREATE EXTENSION pg_trgm` | Requires `azure_pg_admin`. Terraform can allowlist the extension in `azure.extensions`, which is necessary but not sufficient. |
| `ALTER DATABASE … SET pg_trgm.similarity_threshold` | A database-level setting, not a server parameter. |
| `ALTER DEFAULT PRIVILEGES` | Must run as (or as a member of) the role that owns the tables. |
| `ALTER DEFAULT PRIVILEGES FOR ROLE lucent` | Same. |

A `null_resource` with a local-exec `psql` — the usual answer — **would not work
here anyway**: the server has no public endpoint, so a workstation cannot reach
it. The bootstrap has to run inside the VNet, which makes a Container Apps job
the right answer rather than a workaround. `bootstrap.tf` creates that job.

Terraform creates the job. It does not run it, because starting a Container Apps
execution is an ARM action (`…/jobs/<name>/start`) with no Terraform equivalent.
Run it once after apply:

```bash
az containerapp job start -g <resource-group> -n <prefix>-<env>-provision --subscription <subscription-id>
```

or set `trigger_provisioning_job_after_apply = true` to have a `null_resource`
shell out to `az` during apply. That option is off by default because it makes
`apply` non-hermetic and needs an authenticated `az` on `PATH`. The generated
`next_steps` output (`terraform output next_steps`) carries the same command with
the resource group, job name and `--subscription` already filled in from the
module's own `subscription_id` — which matters because the `az` CLI default
subscription need not be the one the deployment landed in.

The job is idempotent: every statement is guarded or uses `IF NOT EXISTS`, so
re-running it after a failure is safe.

### Covered, but gated behind a variable

The application-side prerequisites the design doc lists as launch blockers were
being written in parallel with this module, and they are present in the working
tree. The first three are wired here but left switched off, so that the module
does not assume code your image may not contain; the fourth — file storage — is on
by default, and the section below says why it is a variable all the same.

**First run without a browser (design doc §4.1).** `src/lucent/bootstrap.py`
provides `python -m lucent.bootstrap`, an idempotent, env-driven,
non-interactive bootstrap. Set:

```hcl
enable_app_bootstrap       = true
bootstrap_admin_email      = "ops@example.com"
bootstrap_admin_password   = var.from_your_secret_manager
bootstrap_admin_display_name = "Administrator"
bootstrap_org_name         = "Acme"     # optional
bootstrap_model            = "..."       # optional
```

and the module creates a second Container Apps job that runs
`python -m lucent.migrate` followed by `python -m lucent.bootstrap` on the
application image, with the same Key Vault-backed secrets. Run it *after* the
database provisioning job — the bootstrap connects as the application role,
which the provisioning job creates, so a precondition rejects
`enable_app_bootstrap` without `enable_provisioning_job`.

It stays off by default for two reasons rather than one: it creates a real owner
account, so it needs a password, and that password is then held in Terraform
state. If you would rather not put it there, run the job's command by hand from
inside the VNet, or set `LUCENT_BOOTSTRAP_ON_STARTUP` (the web app's guarded,
opt-in startup path) and let the app provision itself on first request.

**Migrations as a pre-deploy step (design doc §4.2).** The migration runner now
takes a session-level advisory lock (`_migration_lock` in `src/lucent/db/pool.py`),
so concurrent boots serialise instead of racing — that is what makes the
`web_max_replicas` default of 2 safe. `python -m lucent.migrate` also exists now
and reuses the same runner, so the migrate-then-start pattern is available; the
bootstrap job above uses it.

**The Azure worker launcher (design doc phase 4).** `daemon/launcher/azure_launcher.py`
implements `LUCENT_WORKER_LAUNCHER=azure` against the Container Apps job created
here, using `DefaultAzureCredential` — no stored secret. The controller is
configured for it automatically:

| Environment variable | Value |
|---|---|
| `LUCENT_WORKER_LAUNCHER` | `azure` (`worker_launcher` variable) |
| `LUCENT_WORKER_JOB_NAME` | the workers job name |
| `LUCENT_WORKER_RESOURCE_GROUP` | the module's resource group |
| `LUCENT_WORKER_CONTAINER_APP_ENV` | the Container Apps environment name |
| `LUCENT_WORKER_SUBSCRIPTION_ID` | `subscription_id`, or the caller's subscription |
| `LUCENT_WORKER_IMAGE` | `image` |
| `AZURE_CLIENT_ID` | the controller identity's client ID, so `DefaultAzureCredential` selects the user-assigned identity |

On the identification split: Container Apps **executions cannot carry tags or
labels**, so the only identifiers Azure can express are the workers job's static
tag `io.lucent.worker.managed=true` (set by this module — see `container_apps.tf`
— because the controller's role has no `Microsoft.App/jobs/write` to set it) and
the per-execution forced binding env (`LUCENT_DAEMON_ORG`) the launcher writes
into the execution template. The controller's own instance id has nowhere to live
on Azure; `daemon/launcher/azure_launcher.py`'s module docstring states that limit
explicitly.

`worker_launcher` defaults to `azure`, but it is a variable for a reason: the
backend module is imported lazily — `get_launcher` imports the chosen backend on
first use, which the supervisor does inside its reconcile pass. So if the image
you deploy predates the Azure launcher, the controller does **not** fail to
start; the reconcile loop logs an error on each pass (the import keeps raising
until the value matches a module the image ships) and no workers are spawned.
Check which revision your image was built from; if it predates that work, set
`worker_launcher = "docker"` or build a newer image.

**File storage on Azure Blob (design doc §4.4).** The design doc's item 5 is now
implemented application-side as a provider behind the existing boundary:
`src/lucent/storage/providers.py` holds the registry and the always-registered
`LocalFileStorageProvider`, and `src/lucent/storage/azure_blob.py` adds
`AzureBlobStorageProvider` (registered as `azure`, managed identity only, no key
and no connection string). This module creates the account and container and
injects the four variables the provider reads into the **web app**
(`locals.tf`, `web_environment`):

| Environment variable | Value | Read by |
|---|---|---|
| `LUCENT_FILE_STORAGE_PROVIDER` | `azure` (`lucent_file_storage_provider` variable) | `FileStorageRegistry` — selects the provider new writes go to |
| `LUCENT_AZURE_STORAGE_ACCOUNT_URL` | the account's blob endpoint, `https://<account>.blob.core.windows.net` | `azure_blob_configured()` and `AzureBlobStorageProvider` |
| `LUCENT_AZURE_STORAGE_CONTAINER` | the private container name | the same two |
| `AZURE_CLIENT_ID` | the **app** identity's client ID | `DefaultAzureCredential`, which otherwise may not pick the user-assigned identity |

These names are not invented here — they are the exact strings in
`src/lucent/storage/providers.py` (`ENV_ACTIVE_PROVIDER`, the
`LUCENT_AZURE_STORAGE_*` lookups) and `src/lucent/storage/azure_blob.py`
(`ENV_ACCOUNT_URL`, `ENV_CONTAINER`), read from the working tree to match them.
**The two sides must stay in step:** if the application-side work renames any of
them, the module's `file_storage_environment` in `locals.tf` has to change with
it, because nothing checks the names at plan time. `LUCENT_FILE_STORAGE_PATH` is
left in place: the registry always registers the local provider so that a row
written before a provider switch still *resolves*. That is compatibility for the
row's recorded provider name, not durable storage: on Container Apps the bytes
behind a `local` row live on one replica's ephemeral disk, so they do not
survive a restart or scale event and are not visible to another replica.

Two things this wiring deliberately does **not** do:

- **It is not wired into the controller.** The controller runs as the
  `controller` identity, and only the `app` identity holds the blob data role
  (see [Security posture](#security-posture)); handing the controller a container
  it cannot read would be a misleading setting, and the controller supervises
  jobs rather than processing user files.
- **It is not wired into the workers job.** The workers job *does* share the
  `app` identity and so already holds the blob role — but a worker's environment
  is built by `daemon/launcher/base.py`'s `WORKER_ENV_ALLOWLIST`, an explicit
  allowlist that does not contain these variables, and a job start replaces the
  whole execution template anyway. So the permission is in place and the
  configuration is not: if worker code needs to read or write user files, the
  allowlist is an application-side change, not a Terraform one.

**The failure point when the image predates the provider, and why the default
does not protect you.** Do not rely on `lucent_file_storage_provider = "azure"`
to stop an old image. An image built from a revision that does not contain the
provider selector ignores `LUCENT_FILE_STORAGE_PROVIDER` entirely: it boots,
passes `/api/health` (a static liveness payload), and then fails at the **first
file operation**. That failure is at least loud, but it is late. Worse, if an
operator "fixes" the local path to something writable, that same old image
silently writes user files to the replica's ephemeral disk — exactly the data
loss this module exists to prevent, with a green health check.

**The image must therefore be built from a revision that contains the provider
selector** (and `src/lucent/storage/azure_blob.py`). Terraform cannot inspect the
image or assert this at plan time, so verify the revision before apply. In
images that do contain the selector, `FileStorageRegistry` fails closed: naming a
provider it cannot build raises rather than silently falling back. A startup
assertion in the application to make the old-image case fail fast is being
handled separately; Terraform does not provide that protection.

`lucent_file_storage_provider = "local"` is not a safe alternative on Container
Apps. The Dockerfile makes `/var/lib/lucent/files` writable, but that path is the
replica's ephemeral writable layer: it is per-replica, invisible to the other
replica, and lost on restart or scale. Use `local` only to keep an older image
running long enough to build and deploy a newer one — never as durable storage.

These capabilities live in the working tree and are not committed at the time of
writing. This module was validated against the provider schema, not against those
modules — treat the wiring as correct-by-inspection until they land.

#### Task sandboxes on Azure Container Instances (design doc §4.7)

The ACI sandbox backend is opt-in. `sandbox_backend = "docker"` (the default)
creates none of the resources below and injects no sandbox variables, so a
deployment without ACI is unchanged. Set:

```hcl
sandbox_backend      = "aci"
sandbox_bridge_image = "acr…/lucent-sandbox:base"   # optional; defaults to <module registry>/lucent-sandbox:base
```

What is created when `sandbox_backend = "aci"`:

| Resource | Purpose |
|---|---|
| Resource group `<name_prefix>-<environment>-sandbox-rg` (or an adopted existing group via `create_sandbox_resource_group = false`) | A dedicated scope for the app identity's container-group role. It should contain **only** sandbox container groups. |
| Subnet `snet-sandbox`, `10.42.4.0/24` by default, delegated to `Microsoft.ContainerInstance/containerGroups` | ACI VNet integration. Once delegated, the subnet can contain only container groups. |
| User-assigned identity `<base>-sandbox-pull-id` | ACR pull for every sandbox group. `AcrPull` on this module's registry and **no other role**. |
| NSG `<base>-sandbox-nsg`, associated with the sandbox subnet | Default-deny inbound and outbound, with the explicit egress path described below. |
| NAT gateway `<base>-sandbox-nat` + static Standard public IP | The outbound path ACI supports for VNet groups. The public IP is egress only. |
| Policy assignment `<base>-sandbox-diag` | Deploys a diagnostic setting on every container group created in the sandbox group, sending `ContainerInstanceLog` and `ContainerEvent` to the existing Log Analytics workspace. |

Container groups are **not** Terraform resources — the dispatcher in a per-org
worker creates and deletes them at run time through the ARM API. Terraform
creates the scope, the network, the identity and the permissions they need.

#### Environment contract

**The dispatcher that creates task sandboxes runs in a per-org worker, not in
the web process.** `_create_task_sandbox` lives in
`daemon/sandbox/lifecycle.py` and is called only from the dispatcher path in
`daemon/daemon.py`; the dispatcher role exists only inside the workers the
controller starts. A worker's environment is built by
`daemon/launcher/base.py`'s `build_worker_env`, which copies the controller's
environment through `WORKER_ENV_ALLOWLIST`, so the sandbox variables are
therefore injected into **both the web app and the controller/daemon
environment**. They are also present in the workers job template, which is the
base execution template the Azure launcher copies. Putting them in the web app
alone would leave the actual creator unconfigured, and every task requesting a
sandbox would either select Docker (unavailable on Container Apps) or fail the
ACI backend's `_validate_config`.

| Variable | Value | Read by |
|---|---|---|
| `LUCENT_SANDBOX_BACKEND` | `aci` | `src/lucent/sandbox/manager.py` — selects the ACI backend |
| `LUCENT_SANDBOX_ACI_RESOURCE_GROUP` | the sandbox resource group **name** | the ACI backend |
| `LUCENT_SANDBOX_ACI_SUBNET_ID` | the delegated subnet's resource ID | the ACI backend — group `subnetIds` |
| `LUCENT_SANDBOX_ACI_IDENTITY_ID` | the pull identity's resource ID | the ACI backend — group `identity.userAssignedIdentities` |
| `LUCENT_SANDBOX_ACI_LOCATION` | the sandbox resource group's region | the ACI backend — group location; must match the subnet's region |
| `LUCENT_SANDBOX_BRIDGE_IMAGE` | `sandbox_bridge_image`, defaulting to `<registry login server>/lucent-sandbox:base` | `src/lucent/sandbox/bridge_runtime.py` (`DEFAULT_BRIDGE_IMAGE`, `BRIDGE_IMAGE_ENV`) |
| `LUCENT_SANDBOX_BRIDGE_API_URL` | the Lucent server API base, `<lucent_public_url or https://<web FQDN>>/api` | the bridge, which appends paths such as `/memories` and `/requests/tasks/...` and calls the server from inside the sandbox. The server mounts those routers under `/api`, so **the suffix is required**; without it every bridge call 404s. |
| `AZURE_SUBSCRIPTION_ID` | `subscription_id`, or the caller's subscription | the ACI backend — `ContainerInstanceManagementClient` is constructed with a subscription id. Not part of the `LUCENT_SANDBOX_*` contract, but the SDK requires it. |
| `LUCENT_WORKER_AZURE_CLIENT_ID` | the **app** identity's client ID | the application-side worker environment builder, which maps it to `AZURE_CLIENT_ID` **in the worker only** (see the contract below). |

**The `LUCENT_WORKER_AZURE_CLIENT_ID` contract.** The controller and the workers
run as different user-assigned identities:

- The controller's own `AZURE_CLIENT_ID` stays the **controller** identity, so
  `DefaultAzureCredential` can start executions of the workers job.
- The workers run as the **app** identity. The app identity is the one that
  holds the sandbox RBAC (container groups, subnet join, identity assign), so
  the worker's ACI calls must authenticate as it.
- Because the worker environment is inherited from the controller through an
  allowlist, propagating the controller's `AZURE_CLIENT_ID` would make workers
  authenticate as the wrong principal. The module therefore exports the app
  identity's client id separately as `LUCENT_WORKER_AZURE_CLIENT_ID`. The
  application-side allowlist change must map that name to `AZURE_CLIENT_ID` in
  the worker process only; Terraform deliberately never sets the worker's
  `AZURE_CLIENT_ID` directly and never overwrites the controller's.

The web app already receives `AZURE_CLIENT_ID` = the app identity through the
file-storage configuration, so its ACI calls resolve to the same principal as
the workers'.

The ACI backend also reads `LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS`. This module
**does not set it**: the backend's `allowlist` mode fails closed unless an
operator sets `LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS=1` (for example through
`extra_environment`), acknowledging that the subnet-wide NSG cannot enforce a
per-sandbox allow list.

The bridge image reference is the **full registry reference**, not the bare
`lucent-sandbox:base` the application falls back to on Docker. The bridge image
must actually exist in that registry — see
[the bridge image prerequisite](#the-sandbox-bridge-image-is-a-second-image-you-must-build).

The application also has its own sandbox feature switch (`LUCENT_SANDBOXES_ENABLED`
in `src/lucent/settings.py`). This module does not set it: `LUCENT_SANDBOX_BACKEND=aci`
selects the backend, but the feature still has to be enabled in the application.

#### RBAC: the exact grants, and why the scope is a resource group

| Grant | Actions | Scope | Principal |
|---|---|---|---|
| Custom role `<base>-sandbox-operator` | `Microsoft.ContainerInstance/containerGroups/read`<br>`…/containerGroups/write`<br>`…/containerGroups/delete`<br>`…/containerGroups/containers/exec/action`<br>`…/containerGroups/providers/Microsoft.Insights/diagnosticSettings/read`<br>`Microsoft.Insights/diagnosticSettings/read` | the **dedicated sandbox resource group** | app identity (web app and workers) |
| Custom role `<base>-sandbox-subnet-join` | `Microsoft.Network/virtualNetworks/subnets/join/action` | the **sandbox subnet** | app identity |
| Custom role `<base>-sandbox-identity-assign` | `Microsoft.ManagedIdentity/userAssignedIdentities/assign/action` | the **pull identity** | app identity |
| Built-in `AcrPull` | pull images | this module's **registry** | pull identity |
| Built-in `Monitoring Contributor` | write diagnostic settings | the **sandbox resource group** | the policy assignment's identity |
| Built-in `Log Analytics Contributor` | write to the workspace | the **Log Analytics workspace** | the policy assignment's identity |

Why these are the minimum that works:

- **`read`** serves `get`/`list`/`list_all`; **`write`** creates a group;
  **`delete`** destroys one; **`containers/exec/action`** is the one action
  behind `exec` and, in turn, `read_file`/`write_file`/`list_files`. The two
  **`diagnosticSettings/read`** actions (the resource-provider-specific form and
  the generic `Microsoft.Insights` form) are read-only and exist only because a
  `DeployIfNotExists` policy is evaluated as the caller that created the
  resource — the diagnostic-settings policy reads the group's setting as the app
  identity while the policy's own identity writes it. `restart`, `start`,
  `stop`, `attach` and `containers/logs/read` are
  deliberately not granted — `stop` is implemented as a delete and nothing else
  is used. `sandbox_operator_role_actions` is overridable if the backend grows.
- **Subnet join** is a separate ARM authorization check when a container group
  names a subnet. It is one action, not `Network Contributor`: the app cannot
  read, modify, delete or re-delegate the subnet, and cannot touch its NSG,
  route table or the VNet.
- **Identity assign** is required because attaching a user-assigned identity to
  a resource is itself authorized against that identity. It is one action, not
  the built-in `Managed Identity Operator`, and it is scoped to the single pull
  identity.

**Why the container-group role must not be subscription-scoped.**
`Microsoft.ContainerInstance/containerGroups/write` is permission to create a
group from **any image with any command and any environment** — i.e. to run
arbitrary code with whatever identity is attached — and
`containers/exec/action` is arbitrary code execution inside **every group the
assignment covers**. There is no narrower action that still creates a sandbox.
The mitigation is therefore scope, not a smaller permission: the assignment is
on a resource group that contains only sandbox containers, so the app identity
(shared by the web app and the workers) cannot use it to reach the database,
vault, storage account, registry or Container Apps. A subscription-scoped
`containerGroups/*` grant would extend that power over every container group in
the subscription and is exactly what this module does not do.

**The blast radius, stated honestly:** both holders of the app identity — the
internet-facing web process and every worker — have these grants by design. A
compromise of either yields arbitrary code execution in the sandbox resource
group and the ability to pull images from the registry — but not the database
credentials, the Key Vault, the file store or Container Apps administration.

**Sandbox groups get no application identity.** The only identity attached to a
group is the pull identity, whose sole role is `AcrPull`. There is no
system-assigned identity, no Key Vault read, no storage role and no
tenant/application role. Code inside a sandbox can use the pull identity (it is
attached to the group), but the worst that grants is reading images from this
registry.

#### Egress: what is enforceable and what is not

The honest summary is that **ACI has no per-sandbox networking policy**. The
NSG attaches to the **subnet**, so every container group in it gets exactly the
same rules. There is no way to say "sandbox A may reach the Lucent server but
sandbox B may not". The module's NSG is default-deny in both directions:

- **Outbound allowed:**
  - When the module owns the VNet, the destination is the **private-endpoint
    subnet CIDR only** (`private_endpoint_subnet_prefix`), plus any CIDRs in
    `sandbox_extra_outbound_prefixes` (for a custom in-VNet DNS resolver or
    another in-VNet service; the module itself configures Azure-provided DNS).
    Sandbox groups cannot reach the Container Apps subnet, the PostgreSQL
    subnet or the rest of the VNet.
  - When an existing VNet is adopted (`create_vnet = false`), the module does not
    know the private-endpoint subnet's CIDR, so the destination is the
    `VirtualNetwork` tag: the **whole VNet**, explicitly including the
    PostgreSQL subnet. That is broader than the module-created case; supply a
    narrower design (or accept the reach) deliberately.
  - Azure's DNS resolver at `168.63.129.16:53` is a separate rule because that
    address is outside the VNet address space.
  - TCP 443 to the Internet only when `sandbox_internet_egress_enabled = true`
    (the default, because the bridge must reach the public ingress). Everything
    else, including port 25 (which ACI does not support), is denied.
- **Inbound denied**, with one scoped exception: TCP 19390 from
  `sandbox_management_source_prefixes`. Microsoft documents 19390 for **portal
  access** (logs/shell) to VNet-deployed container groups and, potentially, the
  data channel used by container exec — it is **not** a management path ACI
  needs to keep a group running, and blocking it does not leave groups stuck in
  "Waiting"; an earlier claim here said otherwise and was wrong. The module
  defaults this list to the **module-created Container Apps subnet**
  (`container_apps_subnet_prefix`), because the application's exec call
  originates in a worker. When an existing VNet is adopted the existing subnet
  CIDR is not known, so **no rule is created** unless you set the variable —
  add an operator/ExpressRoute range if portal access is required, or `[]` to
  disable it deliberately. Groups are created with one unused placeholder port,
  no public IP and no DNS label (see the security table below).
- **NAT gateway** is the outbound path ACI supports for VNet groups (Microsoft:
  "the only supported configuration"). The static public IP is egress only and
  is exposed as `sandbox_nat_gateway_public_ip` so it can be put in a downstream
  allow list. The subnet also has `default_outbound_access_enabled = false`, so
  there is no implicit fallback that bypasses the NSG.

The limitation to design around: **an NSG cannot match on FQDN**, so "allow 443
to the Internet" means any host on 443, not just the Lucent server or a package
registry. Per-sandbox allow lists and FQDN allow lists are **not enforceable by
this infrastructure**. To get FQDN control you need an **Azure Firewall plus a
user-defined route** sending the sandbox subnet's egress through it; this
module creates neither. With a firewall in place, set
`sandbox_internet_egress_enabled = false` (or narrow the 443 rule) so traffic is
forced through it. The application must **fail closed** for a per-sandbox
allowlist mode unless an operator explicitly opts in to the subnet-wide policy:
the ACI backend's opt-in is `LUCENT_SANDBOX_ACI_ALLOW_SUBNET_EGRESS=1`, which
this module deliberately does not set — do not represent the NSG as per-sandbox
isolation. One consequence of turning the 443 rule off: an image pull from a
**public** registry (or a public ACR) then fails, because ACI reaches it over
the Internet. A private ACR reached through the private endpoint is unaffected.

**Group-to-group traffic.** ACI container groups are hypervisor-isolated from
each other, but the platform does not provide per-group L3 isolation inside a
subnet: Microsoft documents "direct communication between container groups in
the same subnet" as a supported scenario. The module's deny-inbound rule removes
that general path, but it is a **subnet-wide switch, not per-group isolation**:
it blocks every inbound flow from the VNet, and any exception added later
applies to every sandbox at once. If someone disables or weakens the NSG, groups
in the subnet can reach each other.

#### Logging

Sandbox container groups are created at run time, so there is no resource in
this configuration to attach an `azurerm_monitor_diagnostic_setting` to. The
module instead assigns the built-in Azure Policy **"Configure diagnostic
settings for container groups to Log Analytics workspace"**
(`41ebf9df-66cb-48e9-a8d0-98afb4e150ce`), scoped to the sandbox resource group.
Its `DeployIfNotExists` effect adds a diagnostic setting whenever a group is
created, with the only two categories
`Microsoft.ContainerInstance/containerGroups` supports:

| Category | Contents |
|---|---|
| `ContainerInstanceLog` | container stdout/stderr |
| `ContainerEvent` | container lifecycle events |

Three caveats:

- ACI's diagnostic-settings integration is documented as **public preview**,
  although the policy itself is GA. A region that rejects the diagnostic-setting
  resource leaves the group running and the policy non-compliant; it does not
  break the sandbox.
- Applying a diagnostic setting to an **existing** group restarts it. Groups
  created after the assignment are fine (the setting is deployed at creation).
  For groups that predate the policy, use the remediation command in
  `terraform output next_steps`, or delete/recreate them.
- A Terraform-created `DeployIfNotExists` assignment does **not** get the
  portal's automatic role grant for its managed identity. The module grants the
  two roles the built-in policy requires explicitly (see the RBAC table).

#### What this does not do

- **It does not create container groups.** The backend does, at task time.
- **It does not build the application image or vouch for its contents.** The
  deployed image must contain `src/lucent/sandbox/aci_backend.py` and the
  `azure-mgmt-containerinstance` / `azure-identity` packages. The backend must
  attach `LUCENT_SANDBOX_ACI_IDENTITY_ID` in **both** the group's `identity`
  block and its image-registry credential (the current backend does, for an
  ACR user image). The remaining cross-side risk is that the backend selects
  its registry credential from the user image only, so a public user image with
  a private bridge sidecar still fails to pull; that is an application-side
  fix. No Terraform change can substitute for the group identity itself.
- **It does not configure DNS on the groups.** ACI does not inherit a VNet's
  custom DNS servers; DNS settings must be set on the container group. With
  Azure-provided DNS (the default for this VNet) the linked private DNS zones
  resolve, so ACR/Key Vault/Blob private endpoints work. If you point the VNet
  at custom DNS servers, the application must set `dnsConfig` on each group.
- **It does not build or push the bridge image** (see the prerequisite above).
- **It does not create an Azure Firewall or UDR**, and therefore cannot provide
  FQDN or per-sandbox egress allow lists.
- **It does not grant `containerGroups/write` at subscription scope** and should
  never be changed to do so.

## What you still must do by hand

1. **Push the application image** to the registry (see
   [Before you apply](#before-you-apply--the-image)).
2. **Start the provisioning job** after apply, or set
   `trigger_provisioning_job_after_apply = true`. Without it there are no
   application roles, no `pg_trgm`, and no daemon privileges.
3. **Complete first-run setup** — either switch on `enable_app_bootstrap` and
   start that job after the provisioning job, or do it in a browser at
   `https://<app>/setup`.
4. **Add your DNS record** if you set `lucent_public_url` to your own hostname.
   The module does not manage DNS zones; add a CNAME to the app FQDN and, for a
   custom domain rather than the default one, bind it with
   `azurerm_container_app_custom_domain` plus a managed certificate (the resources
   exist, but were left out because they need a DNS zone this module does not own).
5. **Grant the operator `Key Vault Secrets Officer`** if applies fail writing
   secrets and the caller is only Contributor. Then make sure the apply can
   *reach* the vault at all: it is private by default, so run from inside the VNet
   or temporarily re-enable public access — see
   [Writing secrets into a private vault](#writing-secrets-into-a-private-vault).
6. **Make sure your image ships the Azure worker launcher** (or set
   `worker_launcher` to a backend it does ship).
7. **Give whoever touches file *contents* a blob data-plane role**, and set
   `storage_use_azuread = true` on the provider. The account rejects shared-key
   access, so Storage Explorer, `az storage --auth-mode login`, AzCopy and any
   data-plane resource need `Storage Blob Data Contributor` (or Owner) **and** a
   route to the data plane — see
   [Reaching the blob data plane](#reaching-the-blob-data-plane). Terraform's own
   apply needs only the ordinary storage management permission, because the
   container resource is a control-plane one in this provider version.
8. **Make sure your image is built from a revision that contains the Azure file
   storage provider and its selector.** An older image ignores
   `LUCENT_FILE_STORAGE_PROVIDER`, boots, passes `/api/health`, and fails at the
   first file operation — or silently writes to ephemeral replica disk if its
   local path is writable. Setting `lucent_file_storage_provider = "local"` is
   **not** a durable workaround: on Container Apps that path is per-replica and
   ephemeral. Terraform cannot verify the image revision; check it before apply.
   See [the failure-point note](#covered-but-gated-behind-a-variable).
9. **If you enable `sandbox_backend = "aci"`, build and push the sandbox bridge
   image** to `<registry>/lucent-sandbox:base` (or set `sandbox_bridge_image` to
   wherever you pushed it). Nothing in the repository does this. See
   [The sandbox bridge image](#the-sandbox-bridge-image-is-a-second-image-you-must-build).
10. **If you enable the ACI backend, confirm the sandbox resource group, the
    delegated subnet and the Container Apps VNet are in the same region.** ACI
    requires a container group to be created in its subnet's region;
    `LUCENT_SANDBOX_ACI_LOCATION` is the sandbox group's region. An adopted
    existing resource group in a different region is a runtime failure, not a
    Terraform one.
11. **Remediate the diagnostic-settings policy for pre-existing groups** if you
    create container groups outside Terraform's view before the policy existed.
    `terraform output next_steps` prints the `az policy remediation create`
    command when diagnostics are enabled.

## Security posture

| Concern | How it is handled |
|---|---|
| Database exposure | VNet-integrated, `public_network_access_enabled = false`. There is no public endpoint at all. |
| TLS | Container Apps ingress sets `allow_insecure_connections = false`, so the free managed certificate serves HTTPS; plain HTTP on port 80 is only a redirect. Internal calls follow suit: the daemon and workers reach the web app's MCP endpoint over **HTTPS** (`LUCENT_MCP_URL = https://<fqdn>/mcp`), not cleartext. `DATABASE_URL` carries an explicit `sslmode=require` rather than relying on asyncpg's `prefer`, so a TLS misconfiguration fails loudly. |
| Stored credentials | None in the container specs. Image pulls use `AcrPull` on a user-assigned identity; the registry has `admin_enabled = false`, so there is no admin password to fall back on. |
| Key Vault network posture | Private by default: `public_network_access_enabled = false` and the trusted-services bypass is `None`. The vault is reachable only through its private endpoint. See [Writing secrets into a private vault](#writing-secrets-into-a-private-vault) for the apply-time consequence. |
| Secrets | Key Vault references, read by the container's managed identity. The URI is version-less, so rotation is a vault operation and Container Apps picks the new version up within 30 minutes. |
| Secret access scoping | `Key Vault Secrets User` is granted **per secret** (at each secret's own resource ID), not at the vault scope — so an identity can read only the secrets it is actually handed. The raw PostgreSQL passwords and the bootstrap owner password are readable by the `provisioner` identity alone; neither the internet-facing web app nor the controller has any read on them. |
| Container Apps secrets | Passed as `secret` blocks with `key_vault_secret_id` + `identity`, then referenced from environment variables by `secret_name`. No literal secret value appears in a container spec. |
| Registry posture | Azure Container Registry on the Premium SKU, public access disabled, reached over a private endpoint. Premium is required for both the private endpoint and for disabling public access. |
| File storage exposure | Blob Storage reached over a private endpoint, with the public endpoint denied **from the account's own creation** (inline `network_rules.default_action = "Deny"`, `bypass = ["None"]`) and public blob access disallowed account-wide (`allow_nested_items_to_be_public = false`). The container itself is `private`. See [Reaching the blob data plane](#reaching-the-blob-data-plane). |
| File storage credentials | `shared_access_key_enabled = false` — Azure rejects *every* request authorized with an account key, which kills account SAS and service SAS outright (both are Shared-Key signed; only Entra-ID-signed user-delegation SAS survives). The account's keys are **not deleted** — a management-plane principal can still retrieve them with `listKeys` — but they are unusable for data-plane authentication, so there is no *usable* key for a workload to leak. The application authenticates with `DefaultAzureCredential` only. The operator consequences are real and are listed [below](#disabling-shared-key-access-what-stops-working). |
| File access scoping | `Storage Blob Data Contributor` is granted on the **container's own resource ID**, not the account, so the app identity is bounded to the one container it uses and a container added later is not silently granted. It is granted to the `app` identity only — the controller identity has no file access at all. |
| File storage deletion protection | Blob **and** container soft delete are on (`storage_soft_delete_retention_days`, default 7 days): no per-operation cost, no effect on reads and writes, and it expires on its own. Two consequences are documented in [the retention note](#file-storage-retention-soft-delete-versioning-and-immutability): deleted data remains recoverable and billable for the window (so a delete is not an immediate erasure), and overwrites accumulate soft-deleted snapshots while versioning is off. The account and container both carry `prevent_destroy`. Blob **versioning is off** by default and immutability/legal hold is off entirely. |
| Controller RBAC | A custom role listing four actions — not the built-in `Container Apps Jobs Operator`, whose `Microsoft.App/jobs/*/action` wildcard also matches `listSecrets`. The role *definition* is assignable at the resource-group scope (Microsoft's guidance) and the *assignment* — which is what governs access — is scoped to the single workers job. |
| Sandbox isolation | ACI container groups are **hypervisor-isolated** from each other. They are **not** L3-isolated inside the delegated subnet: the platform has no per-group network policy, and Microsoft documents group-to-group communication in one subnet as supported. The module's subnet NSG denies inbound from the VNet, with only a scoped TCP 19390 portal/exec exception (see [the egress section](#egress-what-is-enforceable-and-what-is-not)), which removes the general path; but that is a subnet-wide switch — not per-sandbox isolation. See [the egress section](#egress-what-is-enforceable-and-what-is-not). |
| Sandbox identity | The only identity attached to a sandbox group is the user-assigned pull identity, whose sole role is `AcrPull` on this module's registry. No system-assigned identity, no Key Vault read, no storage role, no tenant/application role. Sandbox code can use the pull identity, so it can read images from the registry — nothing else. |
| Sandbox RBAC | Three narrow custom roles, each assigned where its action is actionable: container-group read/write/delete/exec on the **dedicated sandbox resource group**; subnet join on the **sandbox subnet**; identity assign on the **pull identity**. Not subscription-scoped. `containerGroups/write` and `containers/exec/action` are powerful by nature (arbitrary images, commands and exec), so the app identity's blast radius is deliberately the sandbox group only. |
| Sandbox network exposure | Container groups have **no public IP and no DNS label**. A VNet-deployed group must still declare one container **port**; the backend uses a single unused placeholder port that nothing listens on. Inbound is denied by the module's NSG apart from the scoped TCP 19390 portal/exec exception. Outbound is default-deny, with a NAT gateway as the only supported egress path and a static public egress IP. Per-sandbox and FQDN allow lists are **not enforceable** with an NSG; an Azure Firewall + UDR would be required, and is not created. |
| Sandbox logs | A resource-group-scoped Azure Policy `DeployIfNotExists` assignment adds a diagnostic setting to each group, sending `ContainerInstanceLog` and `ContainerEvent` to the existing workspace. ACI's diagnostic-settings integration is in public preview. The policy identity holds `Monitoring Contributor` on the sandbox group and `Log Analytics Contributor` on the workspace. |
| Identity separation | Three workload identities. The web app and the workers job share the `app` identity, which has no `Microsoft.App/jobs` permissions at all; only the no-ingress controller can start jobs; the one-shot provisioning and bootstrap jobs run as a separate `provisioner` identity that holds the raw credentials and no job permissions. The ACI sandbox adds a fourth, non-workload identity — the pull identity — with `AcrPull` only. |
| Least privilege at the database | Two roles: the owner role the app connects as, and `lucent_daemon` for the daemon and workers, matching what the code already expects (`build_worker_env` prefers `DAEMON_DATABASE_URL`). |
| Least privilege per workload | The web/API app is **not injected with** `DAEMON_DATABASE_URL` — it has no use for it (`src/lucent/db/pool.py` builds its pool from `DATABASE_URL`) and it is the one process class that faces the internet. Because the web app and the workers job share the `app` identity, the identity can still *read* that secret; see the shared-identity caveat below. |
| Container hardening | **Not enforced at the platform layer** — see the gap noted below. |

### Honest caveats on the security story

**The custom role is necessary but not sufficient, and Microsoft says so.** Their
Container Apps documentation is explicit that `Microsoft.App/jobs/start/action`
alone is enough to reach a job's secrets and managed identities, because the
start call accepts an execution template that can override images, commands and
environment variables. Treat job-start permission as permission to use the job's
secrets. What the custom role buys is that the grant is scoped to one job and
carries no unrelated Container Apps access; the remaining protection is that only
the controller holds it and the controller has no ingress.

**Entra ID authentication covers human administration, not the application.**
The design doc's table says "Entra ID auth", which is worth being precise about.
Lucent builds its asyncpg pool from a plain `DATABASE_URL` and has no Entra ID
token provider, so the application path needs password authentication. Both modes
are enabled, but the split is not the one the phrase suggests: the **provisioning
DDL is run with the server administrator login** (`bootstrap.tf` connects as
`postgres_admin_login` with its password), and on Flexible Server that login is a
member of `azure_pg_admin` — the role `CREATE EXTENSION` requires. The **Entra
administrator** is registered so humans can administer the server with an Entra
identity; it is also a member of `azure_pg_admin`, but nothing in the provisioning
path uses it, so it is not "required for CREATE EXTENSION to be possible". The
application path uses a generated password, stored in Key Vault. Closing the
remaining gap — an Entra ID token provider for the app — is an application change,
not a Terraform one.

**The `app` identity is shared, so per-secret scoping does not fully separate
the web app from the worker's credential.** The web app and the workers job
share one identity on purpose (neither holds any `Microsoft.App/jobs`
permission). Because the identity is shared, the per-secret grant that lets the
workers read `DAEMON_DATABASE_URL` also lets the web container read it — even
though the web container is never injected with it. Fully separating the two
would mean giving the workers job its own identity. What per-secret scoping *does*
buy, unambiguously, is that the raw PostgreSQL passwords and the bootstrap owner
password are readable by neither the web app nor the controller: those are
granted only to the `provisioner` identity, which no internet-facing or
long-running workload uses.

**There is no container hardening at the platform layer.** See
[the hardening gap](#resource-by-resource-notes) in the notes below.

### Secrets in Terraform state — where this is unavoidable

The module keeps secrets out of *container specs* and out of *plain environment
variables*, which is what the design doc asks for. It cannot keep them out of
Terraform state. These values are in state:

- `random_password.postgres_admin`, `.postgres_daemon`, `.postgres_app`
- `random_password.lucent_secret_key`, `.lucent_signing_secret`
- every `azurerm_key_vault_secret.application[*].value`
- `azurerm_postgresql_flexible_server.administrator_password`
- anything you pass in `github_token`, `anthropic_api_key`, `openai_api_key`,
  `google_api_key`, `lucent_license_key`, `extra_secrets`

Mitigations, in order of preference: use an encrypted remote backend with a
locking table; keep the state bucket's access narrower than the deployment's;
supply the sensitive variables from a secret manager rather than a `tfvars` file;
and rotate anything that was ever in a local state file. A `-target`-less apply
after `terraform destroy` will regenerate every generated secret — which will
invalidate cookies signed with the old `LUCENT_SIGNING_SECRET` and any secrets
encrypted under the old `LUCENT_SECRET_KEY`.

One more state note: `key_vault_purge_protection_enabled` defaults to `true`, so
a destroyed vault keeps its name reserved for the retention period. That is the
right default for a real deployment and mildly annoying in a throwaway one.

A teardown caveat that follows from the same posture: `prevent_destroy` is set on
the PostgreSQL server and its database (`database.tf`), on the storage account and
on the file container (`storage.tf`), and the vault has purge protection by
default, so a botched first apply **cannot simply be `terraform destroy`ed** —
those guards have to be lifted first (edit the `prevent_destroy` blocks; there is
deliberately no variable for them), and the vault name stays reserved regardless.
Lift the storage guards in one place, for one deliberate teardown, rather than
leaving them off.

The two storage guards exist because their names are effectively immutable for a
live deployment:

- Changing `name_prefix` or `environment` changes the storage account name, which
  replaces the account. Replacement destroys **every tenant file and every
  soft-deleted copy** — soft delete lives inside the account and goes with it.
- Changing `storage_container_name` replaces the container. The application stores
  only `provider` + `storage_key` on each file row and **never the container
  name**, so a rename strands every row even though the bytes still exist under
  the old container name.

Soft delete protects against a wrong `delete` while the account exists; it is not
a backup. If a tenant's files must survive a destroy, copy them somewhere else
before tearing the deployment down.

## Cost notes

The design doc's levers, and where they live in this module:

| Lever | Where |
|---|---|
| **Scale to zero** — the single biggest lever | `web_min_replicas = 0` (default). Inbound HTTP wakes the app. Also `worker_replica_timeout_seconds`, since jobs bill only while running with no idle charge and no 60-second minimum. |
| **The controller is pinned to 1** | Not a variable, on purpose. It must be exactly 1: `min_replicas = 0` on a no-ingress app means it can never wake, and more than 1 duplicates worker supervision. Because it is pinned, Azure bills it at the cheaper idle CPU rate. |
| **Skip Front Door / App Gateway** | Nothing here creates one. Container Apps ingress + the free managed certificate gives TLS. Front Door Standard adds ~$35/month, App Gateway WAF_v2 several hundred. |
| **Avoid the Dedicated Plan Management charge** | ~$0.10/hour (~$73/month) applies to dedicated workload profiles, private endpoints **into Container Apps**, and planned maintenance. This module declares no `workload_profile`, and reaches the web app over its public managed-certificate endpoint rather than a private endpoint into the environment, so the meter stays off. |
| **Burstable database** | `postgres_sku_name` defaults to `B_Standard_B1ms` (~$17–20/month). Microsoft does not recommend burstable SKUs for production; `B_Standard_B2s` or `GP_Standard_D2s_v3` is the honest production floor and moves this line up. |
| **Log Analytics free grant** | The first 5 GB/month are free, which a small beta will not exceed at `log_analytics_retention_days = 30`. |
| **Registry SKU** | `container_registry_sku` defaults to `Premium`, ~$50/month, because the default private endpoint and disabled public network access are Premium-only features. A public `Basic` registry is ~$5/month but cannot be private. This is the one place where "private" is not free; turn the private endpoint off and re-enable public access on the registry if the cheaper public posture is acceptable. |
| **Blob storage for user files** | `Standard` tier, LRS by default (`storage_account_replication_type`). Data is about $0.02/GB-month at the hot access tier plus a small per-operation charge, so a beta file store is a couple of dollars a month; `ZRS` costs roughly 1.25x and `GRS`/`RAGRS` roughly 2x. Soft delete adds the stored bytes of retained blobs (and of every blob version, if `storage_blob_versioning_enabled` is turned on) but has no per-operation charge. The account's private endpoint is part of the private-networking line below. |
| **Sandbox compute (ACI, opt-in)** | `sandbox_backend = "aci"` adds three metered pieces: ACI container-group vCPU/GB-seconds while a sandbox runs (billed per second, so short tasks are cheap and idle groups are not), the sandbox NAT gateway (an hourly charge plus data processing), and a static Standard public IP. There is no cost when `sandbox_backend = "docker"` — the subnet, NSG, identity, roles and policy are not created. Log Analytics ingestion of sandbox logs counts against the workspace's 5 GB/month free grant. |

What this does **not** give you, and what it would cost: a workload-profiles
environment (required for UDRs, NAT Gateway egress, or private endpoints into the
environment) triggers the ~$73/month management meter, and private endpoint
infrastructure and DNS add roughly $30–60/month. The defaults here are the lean,
private-enough tier: private database, private Key Vault, private registry
(Premium, ~$50/month — see the SKU note above), public HTTPS ingress. Note that
these defaults now carry the Premium registry line that the design doc's cost
sketch did not; the design doc's "~$40–75" lean total predates the private
registry.

## Resource-by-resource notes

A few arguments are load-bearing in non-obvious ways.

**The environment is Consumption-only.** No `workload_profile` block is declared,
which is what makes it the legacy Consumption environment type. That is why the
subnet is a `/23` and why it must **not** be delegated — delegation to
`Microsoft.App/environments` is required only for workload-profiles environments
and is wrong for this one. It also caps containers at 2 vCPU / 4 GiB.

**Ingress is disabled on the controller by omitting the block.** There is no
`enabled = false` flag in the provider. Combined with `min_replicas = 1`, that is
the whole point: a no-ingress app with no scale rule and `min_replicas = 0` would
scale to zero and could never come back, because nothing can send it traffic.

**The web app's FQDN is derived from the environment, not read off the app.**
`local.web_fqdn = "<app name>.<environment default_domain>"`. Reading
`azurerm_container_app.web.ingress[0].fqdn` back into the web app's own
environment variables is a dependency cycle, which Terraform rejects outright.

**Container Apps secret names are short aliases.** They are derived from the
Lucent environment variable name (`DATABASE_URL` → `database-url`), with longer
names truncated and disambiguated by a short hash so two variables can never
collide.

**Job `start` replaces the entire execution template.** It is not a patch. The
Azure launcher (`daemon/launcher/azure_launcher.py`) must resend every
environment variable including the secret references, not just the ones it wants
to change. The workers job's template here is only a default.

**The registry SKU is coupled to the two private-registry features.** A private
endpoint and disabling public network access are both Premium-only, so the module
defaults the SKU to Premium and refuses (via a lifecycle precondition) any
combination of those two features with a Basic or Standard SKU. This is checked
at plan time, so a contradictory configuration stops before Azure sees it.

**The Key Vault is private by default and its grants are per secret.** See
[Writing secrets into a private vault](#writing-secrets-into-a-private-vault) for
the apply-time consequence and
[the security posture table](#security-posture) for the per-secret scoping.

**Internal calls use HTTPS too.** `LUCENT_MCP_URL` is `https://<fqdn>/mcp`. The
web app's external environment is reachable from other apps in the same
environment over that FQDN with the managed certificate, and `allow_insecure_connections = false`
means HTTP on port 80 is only a redirect that an MCP client would not follow, so
there is no reason to downgrade to cleartext.

**Provisioning SQL never carries the passwords on the psql command line.** The
provisioning job's SQL file binds the role passwords from the environment with
psql's `\getenv` meta-command and interpolates them with `:'name'`, so no
password appears in the process argument list. This requires psql 15 or later —
`bootstrap_image` defaults to `postgres:16-alpine`, which has it.

**No container hardening — a platform gap.** The Kubernetes deployment runs its
containers under a security context that can set `runAsNonRoot`, drop
capabilities and pin a seccomp/AppArmor profile. The Azure Container Apps
provider exposes **none** of that: `azurerm_container_app`'s container block has
only `image`, `name`, `command`, `args`, `cpu`, `memory`, `env`, `volume_mounts`
and the probes — there is no `security_context`. This is a genuine provider and
platform limitation, not a choice made here, so these controls are **not**
enforced at the Azure platform layer. Non-root-ness in the Azure deployment comes
only from the image's own `USER`; capability drops and seccomp/AppArmor profiles
are not expressible. If those controls are a hard requirement, Container Apps
cannot meet it and Kubernetes (AKS) can.

**The database is reached by VNet integration, not by a private endpoint.** Both
produce a private-only database; integration is simpler and is what "VNet-integrated"
in the design doc describes. It brings one naming trap: the private DNS zone must
end in `.postgres.database.azure.com` and **not** `privatelink.postgres.database.azure.com`
— the `privatelink.` form is for the private-endpoint path and Azure rejects it
here. Key Vault, ACR and Blob Storage, which do use private endpoints, get the
usual `privatelink.vaultcore.azure.net`, `privatelink.azurecr.io` and
`privatelink.blob.core.windows.net`.

**The private-link DNS zones are composable, because their names are fixed.**
`privatelink.vaultcore.azure.net`, `privatelink.azurecr.io` and
`privatelink.blob.core.windows.net` are product-wide names, and Azure rejects
linking two zones with the same name to one VNet. Composing this module into an
existing VNet that already owns one of them — the normal case when another Key
Vault, registry or storage account already uses a private endpoint there — would
therefore fail the apply if the module created its own. Each has a
`create_<service>_private_dns_zone` / `existing_<service>_private_dns_zone_id`
pair: set create to `false` and supply the existing zone's resource ID. In that
mode the module creates neither the zone nor a VNet link; the existing zone is
expected to be linked to the VNet already, which is what the duplicate-zone
failure implies. The private endpoint carries a precondition that rejects
create = false with no existing ID. The PostgreSQL zone does not need this: its
name embeds `name_prefix`/`environment`, so it is deployment-specific.

**Blob storage gets a private endpoint, not a service endpoint.** The alternative
— a `Microsoft.Storage` service endpoint on a subnet, with that subnet listed in
`virtual_network_subnet_ids` — would also work and would allow the same-region VNet
traffic, but it changes the account's routing posture for the subnet and does not
give a private address. The private endpoint is the stricter and more explicit of
the two, and it needs no firewall rule at all (the firewall governs the public
endpoint only). Note that Container Apps itself is Consumption-only and reaches
the account from its subnet through the environment's network, which is why the
`privatelink.blob.core.windows.net` zone is linked to the VNet. One consequence of
the private-endpoint path that is worth knowing before you build on it: once a VNet
has a blob private endpoint, clients in that VNet must reach any *other* storage
account that also has a blob private endpoint through a private endpoint of its
own — a documented DNS consequence of the `privatelink` CNAME, and something to
plan for if you later point a Lucent component at a second blob account.

**The sandbox subnet is exclusive.** A subnet delegated to
`Microsoft.ContainerInstance/containerGroups` can contain only container groups;
Azure rejects any other resource type in it. That is why the module creates a
fourth subnet rather than reusing the Container Apps or private-endpoint subnet.
The module also associates its own NSG and (by default) its own NAT gateway with
that subnet. When composing into an existing VNet, supply
`existing_sandbox_subnet_id` pointing at a delegated subnet that contains nothing
else; the module takes over the subnet's NSG association, so an NSG that was
already attached there is replaced.

`default_outbound_access_enabled = false` is set only on the subnet the module
creates. It is a subnet property, and the module cannot change an adopted
subnet's outbound-access setting here: verify (or disable) it on the existing
subnet yourself, or the implicit outbound path bypasses the NAT-gateway/NSG
egress model this module documents. Likewise, the scoped TCP 19390 rule needs
`sandbox_management_source_prefixes` when an existing VNet is adopted, because
the module does not know the existing Container Apps subnet's CIDR.

**Container-group logging is a policy assignment, not a diagnostic-setting
resource.** The application creates groups at run time, so there is no resource
in the plan to attach an `azurerm_monitor_diagnostic_setting` to. The
`azurerm_resource_group_policy_assignment` in `monitoring.tf` deploys the
diagnostic setting when a group is created. Two consequences: a Terraform-created
`DeployIfNotExists` assignment does not receive the portal's automatic role grant
for its identity, so the two roles are assigned explicitly in `rbac.tf`; and
groups that already exist need a remediation task (or recreation) to pick the
setting up.

### Disabling shared key access: what stops working

`shared_access_key_enabled = false` is the setting that makes the storage account
refuse every request authorized with an account key. It is what removes the last
long-lived credential from the file path, and it is also the change most likely to
surprise an operator. Be precise about what it does *not* do: the account's keys
still exist, and a management-plane principal can still retrieve them with
`listKeys`. What changes is that they are **unusable for data-plane
authentication** — there is no usable key, not no key. Concretely, from
Microsoft's own documentation:

- **Account SAS and service SAS stop working entirely.** Both are Shared-Key
  signed (`Request is denied for all Azure Storage services`). Only a
  **user-delegation SAS** — Blob Storage only, signed with Entra ID — still works.
  Anything this deployment or an integration generates as an account or service
  SAS is dead.
- **Some tools need to be told to use Entra ID.** The Azure portal, Storage
  Explorer, AzCopy, Azure PowerShell and the Azure CLI all support Entra
  authorization for Blob Storage, but several of them default to the account key.
  `az storage … --auth-mode login` is the flag that matters; AzCopy needs
  `--auth-mode=login` or a user-delegation SAS. Sign in with an identity that has
  a blob **data** role, not a management role, or every read still fails.
- **Other Azure services that speak to storage with Shared Key cannot use the
  account.** If you later point a backup, a Data Factory pipeline or a VM disk at
  this account, the assumption that an account key exists is what breaks. (Azure
  Files is the sharpest case: the portal uses Shared Key for file shares by
  default, so file shares on this account are not reachable in the portal without
  changing how the portal authorizes.)
- **Cloud Shell's own storage is unrelated**, but if you were relying on that
  account's key for anything scripted, that breaks too.
- **It is reversible.** `shared_access_key_enabled = true` restores the previous
  behaviour — and note the account's keys are not rotated by this change, so
  anyone who captured a key before it was disabled regains access when it is
  re-enabled. Microsoft's guidance is to migrate clients to Entra ID first and
  then disallow keys, which is the order this module assumes.

For day-to-day operations on this account, the practical recipe is: sign in with
Entra ID, hold `Storage Blob Data Contributor` (or Owner) on the container, be
inside the VNet or listed in `storage_allowed_ip_ranges`, and pass
`--auth-mode login`. There is no key to paste into a tool.

### File storage retention: soft delete, versioning and immutability

Three settings, and the module treats them differently on purpose.

**Soft delete — on, 7 days by default.** Both `delete_retention_policy` (blobs)
and `container_delete_retention_policy` (containers) are set from
`storage_soft_delete_retention_days`. It has no per-operation cost, no effect on
reads or writes, and it expires on its own, so it is the cheap protection against
an accidental or malicious delete or overwrite — including a mistaken container
drop. It protects data *inside* the account; it is not a backup, and it does not
survive the account being deleted.

**Soft delete is not erasure — the consequence to plan for.** Deleting a tenant's
files (or dropping the container) does **not** remove the bytes. They remain
recoverable, and billed at the same rate as active data, for the retention
window. Microsoft's own documentation is explicit: "You cannot permanently delete
a blob that has been soft deleted before the retention period expires." The
documented way to purge a soft-deleted **base blob** early is to disable blob
soft delete for the **whole account**, then delete it again (undelete it first if
needed); that is account-wide and affects every tenant, not a per-tenant
operation. The practical choices for an erasure-sensitive deployment are:

- Set `storage_soft_delete_retention_days = 1`, the shortest supported window.
  A deletion then becomes unrecoverable (and stops billing) after one day.
- Set `storage_permanent_delete_enabled = true` if you need to remove
  soft-deleted **snapshots and versions** early — see below. This is account-wide
  and it also weakens accidental-overwrite protection; it does not let you purge a
  soft-deleted base blob early.
- Otherwise, treat an erasure request as taking up to the retention window, and
  document that in your own privacy policy rather than claiming immediate
  deletion.

**Overwrites accumulate soft-deleted snapshots.** While versioning is off,
overwriting a blob automatically creates a soft-deleted snapshot of its prior
state (Microsoft: "Overwriting a blob automatically creates a soft-deleted
snapshot … When the retention period expires, the system permanently deletes the
soft-deleted snapshot"). Those snapshots are billed like active data for the
window. That matters for an overwrite-heavy store and for erasure: deleting the
current blob does not remove the prior contents captured by an overwrite.
`storage_permanent_delete_enabled = true` is what lets a caller permanently delete
those soft-deleted snapshots (and soft-deleted versions) before the window
expires. It defaults to **false** — the protective setting — because enabling it
also removes the ability to recover an overwritten file for the full window. Set
it deliberately, account-wide, for erasure-sensitive deployments.

**Versioning — off by default, and that is a recommendation rather than an
inherited default.** Azure keeps a new version of a blob on every write when
versioning is on, and **versions have no expiry**: unlike soft delete, nothing
prunes them. Every version stays until it is explicitly deleted. For a store that
holds tenants' data that creates a standing retention obligation and works against
the two operations this deployment most needs — deleting one tenant's data on
offboarding or on an erasure request, and tearing the deployment down cleanly. It
also makes an erasure request harder to satisfy honestly: you must delete every
version, not just the current blob, and prove it. Soft delete already covers the
realistic incident at no retention cost. Enable
`storage_blob_versioning_enabled` if you additionally need to recover from a bad
write that is older than the soft-delete window, and accept the retention
consequence; do not enable it and then assume a data subject's files are gone
because the current blob was deleted.

**Immutability (a time-based `immutability_policy`) and legal hold — off, with no
variable.** A WORM policy is exactly what must not exist on this store. It blocks
deletion and modification of blobs for its period — which is the point of a WORM
policy and the reason it conflicts with deleting a tenant's data, with an erasure
request, and with `terraform destroy`. If a compliance regime requires retention
that outlives a tenant relationship, the right shape is a separate, deliberately
immutable account for the retained copies, not a policy on the live file store.
There is no variable for it so that enabling it is a deliberate edit to
`storage.tf`.

**Connection limits.** Lucent's pool opens min 2 / max 10 connections per process
(`src/lucent/db/pool.py`), and every replica and every warm worker is a process.
A B1ms server defaults to about 50 connections, which three or four processes
will approach. `postgres_max_connections` is exposed but defaults to null, because
raising it costs memory and the real answer is either PgBouncer — which must be
in **session** mode, or LISTEN/NOTIFY breaks and tasks silently never dispatch —
or a larger SKU.

**Probes.** The web app gets a `liveness_probe` and a `startup_probe` on
`/api/health`, matching the Dockerfile. That endpoint is liveness only: it returns
a static payload with no database check, and there is no readiness endpoint, so a
replica can pass while still running migrations. Nothing in Terraform fixes that.

## Verification status

Be precise about what has and has not been checked.

**Verified on this machine:**

- `terraform fmt -check -recursive` — clean.
- `terraform init` — succeeded, resolving `hashicorp/azurerm 5.10.0`,
  `hashicorp/random 3.9.1`, `hashicorp/null 3.3.2`.
- `terraform validate` — "The configuration is valid." This checks every
  argument name and every referenced attribute against the real provider schema,
  and it caught two errors during development: an unsupported
  `resource_group_name` on `azurerm_private_dns_zone_virtual_network_link`, and a
  dependency cycle through the web app's own FQDN.
- Argument existence for the later security changes was confirmed directly
  against the provider schema with `terraform providers schema -json`:
  `azurerm_key_vault_secret.resource_versionless_id` exists (it is the per-secret
  scope the role assignments use), `azurerm_key_vault.network_acls` accepts
  `bypass`, and the `azurerm_container_app` container block genuinely has no
  security-context attribute (the hardening gap above).
- The blob storage arguments were confirmed the same way, before a line of
  `storage.tf` was written, because several of the names are not the ones you
  would guess: the schema attribute is `allow_nested_items_to_be_public` (**not**
  `allow_nested_items_to_public`, which does not exist — an argument the provider
  does not recognise is a plan-time error, not a silent no-op); the storage
  container's access setting is `container_access_type`; soft delete is
  `blob_properties.delete_retention_policy.days` plus
  `blob_properties.container_delete_retention_policy.days`; versioning is
  `blob_properties.versioning_enabled`; and `storage_use_azuread` is a provider
  argument, not a resource one. The schema also showed that
  `public_network_access_enabled` on a storage account is marked **deprecated** in
  5.10.0 in favour of the `public_network_access` string, which is why neither is
  set.
- After the storage work, `terraform fmt -check -recursive` is clean and
  `terraform validate` again reports "Success! The configuration is valid." — so
  every argument added above exists in the azurerm 5.10.0 schema.
- **Which Azure plane `azurerm_storage_container` uses was checked in the
  provider source, not assumed**, because the firewall ordering turns on it. The
  5.10.0 implementation of `storage_container_resource.go` takes its client from
  the provider's resource-manager bundle (`Storage.ResourceManager.BlobContainers`)
  and makes no data-plane request, so it is a **control-plane** resource — while
  the `storage_account` page's `shared_access_key_enabled` note still describes
  Shared-Key (data-plane) provisioning. The inline `network_rules` block is set
  through the account's own create/update (`NetworkAcls` in the storage-account
  request), which is also control-plane. That is what makes Deny-at-create safe
  for the container. If a provider upgrade moves the container back to the data
  plane, the apply fails at the container instead of leaving the account open.
- **The provider's schema defaults were checked in the 5.10.0 source**, not
  inferred from Azure documentation: `cross_tenant_replication_enabled` and
  `allow_nested_items_to_be_public` both default to **false** in
  `storage_account_resource.go`, and
  `blob_properties.delete_retention_policy.permanent_delete_enabled` defaults to
  **false**. The module still sets the first two explicitly (so the posture does
  not depend on a provider default), and exposes the third as a variable.
- **The private-DNS-zone composition inputs were validated**, not just written:
  `create_{blob,key_vault,acr}_private_dns_zone` and the matching
  `existing_*_private_dns_zone_id` inputs, with the zones and VNet links counted
  and the private endpoints carrying preconditions that reject a missing existing
  id.
- After the defect fixes, `terraform fmt -check -recursive` is clean and
  `terraform validate` reports "Success! The configuration is valid."
- **The ACI sandbox infrastructure was validated against the azurerm 5.10.0
  schema after it was written.** `terraform fmt -check -recursive` is clean and
  `terraform validate` reports "Success! The configuration is valid." Every
  argument used below was confirmed present in the provider schema before use:
  `azurerm_nat_gateway` (`sku_name`, `idle_timeout_in_minutes`),
  `azurerm_nat_gateway_public_ip_association`,
  `azurerm_subnet_nat_gateway_association`,
  `azurerm_subnet_network_security_group_association`,
  `azurerm_network_security_group.security_rule`,
  `azurerm_subnet.default_outbound_access_enabled` and the
  `Microsoft.ContainerInstance/containerGroups` delegation,
  `azurerm_public_ip` (`sku`, `allocation_method`), and
  `azurerm_resource_group_policy_assignment` (`identity`, `location`,
  `parameters` as a JSON string, `resource_group_id`, `policy_definition_id`).
  The role-action strings are *not* schema-checked — Terraform treats them as
  opaque strings — so they were verified against Microsoft's "Azure permissions
  for Containers" reference instead.
- **The plan shape is now a committed regression test, not a scratch root.**
  `modules/lucent/tests/plan.tftest.hcl` uses Terraform 1.9.8 `mock_provider`
  for `azurerm`, `random` and `null` and runs three plans: the default
  `sandbox_backend = "docker"`, `sandbox_backend = "aci"`, and an
  `extra_secrets` case. It asserts that docker injects no sandbox variables,
  that ACI carries the sandbox configuration into the controller AND the
  workers job with `LUCENT_SANDBOX_BRIDGE_API_URL` ending in `/api` and
  `LUCENT_WORKER_AZURE_CLIENT_ID` present alongside the controller's
  `AZURE_CLIENT_ID`, and that the secret-name output stays usable with an
  operator-added secret. Run it with:

  ```bash
  cd deploy/terraform/modules/lucent
  terraform init -backend=false
  terraform test
  ```

  Earlier scratch-root runs measured **85 resources** for ACI and **66** for
  docker, the difference being exactly the 19 ACI resources (resource group,
  subnet, NSG + association, NAT gateway + public IP + two associations, pull
  identity, three role definitions, four app/sandbox role assignments, two
  policy-identity role assignments and the policy assignment). No Azure API is
  called and this is not an Azure plan; it verifies that expressions, counts
  and references evaluate. The run uses the repository copy **unchanged** — no
  variable is de-sensitized — because every secret-backed `for_each` now iterates
  `local.application_secret_keys` (`nonsensitive(toset(keys(local.application_secrets)))`),
  and the non-sensitive `local.web_secret_keys` for the web app. Those are key
  sets only: the environment-variable names (`DATABASE_URL`,
  `OPERATOR_ADDED_SECRET`, ...) carry no secret material, and each resource
  reads its value with `local.application_secrets[each.key]` inside the block,
  so the values remain sensitive in state, plan output and outputs. Iterating
  the map itself was a plan-time error (*"Invalid for_each argument …
  local.application_secrets has a sensitive value"*); no secret was
  declassified to fix it.

**Not verified:**

- **No `terraform plan` and no `apply`.** This environment has no Azure
  credentials and no `az` CLI, so the provider cannot build an authorizer. Plan
  is expected to fail for exactly that reason and did.
- **Nothing has been deployed.** Every resource argument is written from provider
  and Azure documentation, not from observed behaviour. The provisioning SQL in
  `bootstrap.tf` in particular has never run against a Flexible Server.
- **The blob storage account has never been created.** The firewall reasoning in
  [Reaching the blob data plane](#reaching-the-blob-data-plane) rests on two
  things read from documentation and source rather than observed: that the
  storage firewall exempts control-plane operations, and that this provider
  version's container resource is a control-plane one. If the second is wrong,
  the failure is a loud 403 at the container, not an open account;
  `storage_network_default_action` exists so that an operator who hits something
  unforeseen has a way forward that does not require editing the module.
- **Which role an Entra ID principal needs for storage data-plane access** is
  stated from how Azure's data-plane authorization works — a management role such
  as Storage Account Contributor reads and writes zero blobs — and from the
  provider's own note that `storage_use_azuread` is needed once shared-key access
  is off. It is not quoted from a page that spells the role out, and it has not
  been exercised. It matters only for data-plane tooling and for any data-plane
  resource added later; this module's own resources are control-plane.
- **Whether the storage firewall would in fact block the container** is untested
  against Azure: the reasoning that it does not is from the provider source (the
  container is control-plane in 5.10.0) and Microsoft's documented plane split.
  Unlike the previous split-resource ordering, inline Deny no longer tolerates the
  other answer — if the container turns out to be data-plane, the first apply
  fails rather than the account being briefly open.
- **Which Azure services in *your* subscription depend on this account's key** is
  not knowable from here. The consequences are listed from Microsoft's
  documentation; the audit is a separate step.
- **No live PostgreSQL was used.** The privilege sequence in the provisioning job
  is designed to avoid needing role membership — all cross-role grants are done
  by the administrator and the application role only alters its own default
  privileges — but that reasoning is from PostgreSQL semantics, not from a test.
- **The four application-side capabilities this module wires up** — the bootstrap
  CLI, the migrate CLI with its advisory lock, the Azure worker launcher, and the
  Azure file storage provider — were read from the working tree to match their
  interfaces (exact environment variable names, exact entry points). Their
  correctness is not something this module can vouch for, and none of them is
  committed yet.
- **Nothing about the ACI sandbox path has been deployed.** In particular:
  - The **container-group role action set** (read, write, delete,
    `containers/exec/action`, plus the diagnostic-settings read) was matched to
    `src/lucent/sandbox/aci_backend.py`, which appeared in the working tree
    during this work and was read for its env names and its ARM calls: it uses
    `container_groups` create/get/list/delete and `containers.execute_command`,
    and does **not** call `stop/action`, `attach`, or read container logs. If a
    later revision adds those, the action must be added to
    `sandbox_operator_role_actions`. One cross-side mismatch is called out
    below.
  - **The pull identity is attached in both required places, but the registry
    credential is chosen from the USER image only.** `aci_backend.py` now puts
    the identity in the group's `identity` block as well as in the
    image-registry credential, when the user image is an `*.azurecr.io`
    reference — the two places ACI requires. The remaining risk is the bridge
    sidecar: `_registry_credential(config.image)` looks only at the user image,
    so a **public user image plus a private-ACR bridge image** produces no
    credential and no group identity, and the bridge pull fails. This module
    creates the pull identity, grants it `AcrPull`, and grants the app identity
    permission to assign it; deciding credentials from every image in the group
    (or always attaching the identity for an ACR bridge) is an application-side
    fix that is being handled separately.
  - The **subnet-join** and **identity-assign** actions are required by ARM's
    authorization model and documented for ACI/VNet deployments, but the exact
    failure mode without them has not been observed.
  - The **NSG rules** — in particular the TCP 19390 inbound exception and the
    claim that denying VNet inbound removes ACI's same-subnet communication path
    — follow Microsoft's ACI and NSG documentation, not a live test. NSGs are
    evaluated on both the source outbound and destination inbound side, so a
    deny-inbound rule should stop group-to-group traffic, but this is unverified.
  - The **diagnostic-settings policy** uses the built-in definition
    `41ebf9df-66cb-48e9-a8d0-98afb4e150ce`, whose `DeployIfNotExists` template
    enables `ContainerInstanceLog` and `ContainerEvent`. The assignment's
    `parameters` JSON, the explicit role grants for its identity, and the
    public-preview status of ACI diagnostic settings have not been exercised.
  - **Private ACR pulls** from a VNet-deployed group depend on the
    `privatelink.azurecr.io` zone linked to the VNet and on ACI resolving through
    Azure-provided DNS. The zone and link are created by this module; the pull
    path is unverified, and custom VNet DNS servers would require the backend to
    set `dnsConfig` on each group.
  - The **NAT gateway + `default_outbound_access_enabled = false`** combination
    is the documented supported egress path, but has not been deployed. No
    subscription credentials were available, so even `terraform plan` could not
    run.
- **Resolved — no longer a known issue:** the earlier sensitive-value
  `for_each` blocker is fixed and plan-verified, not merely written around. The
  mock plan above runs against the module as committed. All nine sites that iterated a sensitive collection
  (`azurerm_key_vault_secret.application`; the web app's `dynamic "secret"` and
  `dynamic "env"`; the controller's and workers' `dynamic "secret"` and
  `dynamic "env"`; the bootstrap job's `dynamic "secret"` and `dynamic "env"`)
  now iterate non-sensitive key sets. Resource counts and instance keys are
  unchanged, and an operator-supplied `extra_secrets` key still produces its
  Key Vault secret, its per-secret role assignments, and its injected
  `secret`/`env` entries.

Provider arguments were taken from the azurerm v5.10.0 documentation (the
published `latest` at the time of writing) and cross-checked against the Azure
service documentation. Where the two disagreed, the provider won.

**First-apply flakiness to expect.** Role assignments are eventually consistent
in Entra ID. A Container Apps revision that resolves a Key Vault secret
immediately after its `Key Vault Secrets User` assignment is created can fail
with an identity-not-found style error on the very first apply. Re-running apply,
or restarting the revision, clears it. `depends_on` reduces but cannot eliminate
this.

## Open questions from the design doc, §8

The design doc's §8 is now a numbered list; its §8.2 (Dedicated Plan Management
charge) and §8.4 (multi-container jobs) are marked **resolved** there, and that
is reflected here.

| Question | What this module assumes |
|---|---|
| **Container Apps Sandboxes**: GA status and billing meters unconfirmed | Not used. The conservative ACI path is now provisioned behind `sandbox_backend = "aci"` (delegated subnet, pull identity, narrow RBAC, NSG/NAT, diagnostics policy); Container Apps Sandboxes (`Microsoft.App/SandboxGroups`) are not created, and their GA status and billing meters remain unconfirmed. |
| **Dedicated Plan Management charge rate** | Assumed to be ~$0.10/hour (~$73/month), triggered by dedicated workload profiles, private endpoints into Container Apps, and planned maintenance. The module avoids all three. The design doc's "~$30–60+" private-networking line is therefore avoided, not paid. Confirmed by Microsoft's billing docs; the exact rate should still be checked against the pricing calculator for the target region. |
| **Container Apps Consumption rates and free grant** | Not encoded. Nothing here depends on them, but the cost table in this README inherits the design doc's numbers, which were read from pages that render figures dynamically. Re-check with the Azure Pricing Calculator. |
| **Can a Container Apps Job run more than one container?** | **Yes — resolved.** Microsoft's jobs documentation states containers are an array and a job runs a pod of containers sharing a network namespace, and a Microsoft engineer confirmed it on the azure-container-apps tracker. So the trusted MCP bridge could be a job sidecar and ACI is not forced by this constraint. Two caveats: the CLI only supports a single `--image`, so multi-container jobs need the API/Terraform/YAML; and the resource limits apply to all containers combined. This module still creates a single-container workers job — the sandbox path is separate and opt-in (see [Sandboxes on ACI](#task-sandboxes-on-azure-container-instances-design-doc-47)). |
| **Flexible Server `max_connections` versus replicas and workers** | Flagged, not solved. See the connection-limits note above. `postgres_max_connections` is exposed; the better answers are PgBouncer in session mode or a larger SKU. |

Two more findings that §8 did not list but that affect the deployment:

- The **Consumption-only subnet must be `/23`** and must not be delegated. A
  workload-profiles environment needs only `/27` and must be delegated. Choosing
  the wrong pair is a failed apply.
- **`web_max_replicas` defaults to 2**, matching the design doc's beta guidance
  and only safe because the migration runner now holds an advisory lock. Each
  replica opens up to 10 PostgreSQL connections, and the rate limiter and
  settings/model caches are per-replica, so scale-out has a database cost and a
  consistency cost — both bounded at 2, both real beyond it.

## Documentation consulted

Provider documentation — `registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/…`
for `container_app_environment`, `container_app`, `container_app_job`,
`postgresql_flexible_server`, `postgresql_flexible_server_configuration`,
`postgresql_flexible_server_database`,
`postgresql_flexible_server_active_directory_administrator`, `key_vault`,
`storage_account`, `storage_container`, `storage_account_network_rules`,
`role_definition`, `role_assignment`, `user_assigned_identity`, `private_endpoint`,
`private_dns_zone`, `private_dns_zone_virtual_network_link`, `subnet`,
`virtual_network`, `log_analytics_workspace`, and for the sandbox path
`nat_gateway`, `nat_gateway_public_ip_association`,
`subnet_nat_gateway_association`, `network_security_group`,
`subnet_network_security_group_association`, `public_ip` and
`resource_group_policy_assignment`. Content matches the v5.10.0 tag of
`hashicorp/terraform-provider-azurerm`; the storage pages were read from that tag
in the repository rather than from the rendered registry pages, which need
JavaScript. The `azurerm` provider block's `storage_use_azuread` argument is
documented on the
[provider index page](https://registry.terraform.io/providers/hashicorp/azurerm/5.10.0/docs).

Azure service documentation:

- [Jobs in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/jobs) — trigger types, the start permission, and the warning that start implies secret access.
- [Manage secrets in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets) — Key Vault references, the `Key Vault Secrets User` role, versioned vs version-less URIs, the custom-role example, and the wildcard-roles warning.
- [Networking in an Azure Container Apps Environment](https://learn.microsoft.com/en-us/azure/container-apps/networking) — environment types, `/23` vs `/27`, and delegation rules.
- [Billing in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/billing) and the [Container Apps pricing page](https://azure.microsoft.com/en-us/pricing/details/container-apps/) — the Management (hour) meter.
- [Azure permissions for Compute](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute) — the `Microsoft.App/jobs/*` action list.
- [Containers in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/containers) — the allowed vCPU/memory pairs and the Consumption-only 2 vCPU / 4 GiB cap.
- [Allow extensions on PostgreSQL Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-allow-extensions) and [the extension catalogue](https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions) — `azure.extensions` format and spelling.
- [Private networking for PostgreSQL Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-private) — the `.postgres.database.azure.com` required suffix and the `/28` minimum.
- [Azure Storage firewall rules and network access](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security) — the rule types, the `None`/`AzureServices` bypass, and the private-endpoint-versus-service-endpoint distinction.
- [Guidelines and limitations: Azure Storage firewall](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-limitations) — that firewall rules apply to **data-plane** operations and not control-plane ones, and that **IP network rules have no effect on requests originating in the same region as the storage account**.
- [Use private endpoints for Azure Storage](https://learn.microsoft.com/en-us/azure/storage/common/storage-private-endpoints) — that the firewall governs the public endpoint only, that private-endpoint traffic from the VNet is always allowed, the `blob` sub-resource and `privatelink.blob.core.windows.net` zone, and the same-VNet constraint on reaching other accounts.
- [Soft delete for blobs](https://learn.microsoft.com/en-us/azure/storage/blobs/soft-delete-blob-overview) — that soft-deleted data remains recoverable and billed for the retention window, that overwrites create soft-deleted snapshots when versioning is off, and that permanent delete overrides retention for soft-deleted snapshots and versions.
- [Manage and restore soft-deleted blobs](https://learn.microsoft.com/en-us/azure/storage/blobs/soft-delete-blob-manage) — that a soft-deleted base blob cannot be permanently deleted before its retention period expires, and the account-wide disable-and-re-delete procedure.
- [Prevent authorization with Shared Key](https://learn.microsoft.com/en-us/azure/storage/common/shared-key-authorization-prevent) — the SAS table (account and service SAS denied, user-delegation SAS permitted), the per-tool Entra ID support table, and the 403 consequence.
- [Azure Container Apps GitHub issue #915](https://github.com/microsoft/azure-container-apps/issues/915) — multiple containers in one job, confirmed by a Microsoft engineer.
- [Azure Container Apps GitHub issue #1024](https://github.com/microsoft/azure-container-apps/issues/1024) — the history of built-in roles for operating jobs.
- [Deploy container group to Azure virtual network](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-vnet) — the delegated subnet requirement, the /24 minimum, and the unsupported public-IP/DNS-label and non-ACR-private-registry scenarios.
- [Scenarios to use a virtual network with Azure Container Instances](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-virtual-network-concepts) — "you must use a NAT gateway for outbound connectivity … the only supported configuration", direct communication between container groups in the same subnet, port 19390, the service-endpoint note, the /24 sizing and 1,000/3,000-group guidance, and that container instances do not inherit VNet DNS settings.
- [Configure container group egress with NAT gateway](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-nat-gateway) — the NAT gateway + static Standard public IP pattern.
- [Enable managed identity in a container group](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-managed-identity) — user-assigned identity attachment for ACR pulls.
- [Azure permissions for Containers](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/containers) — the exact `Microsoft.ContainerInstance/containerGroups/*` action names, including `containers/exec/action`.
- [Supported log categories: Microsoft.ContainerInstance/containerGroups](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-logs/microsoft-containerinstance-containergroups-logs) — `ContainerInstanceLog` and `ContainerEvent`.
- [Collect and analyze resource logs — Azure Container Instances](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-log-analytics) — diagnostic settings, the public-preview status, and the restart-on-apply behaviour.
- [Built-in policy definitions for Azure Container Instances](https://learn.microsoft.com/en-us/azure/container-instances/policy-reference) — the policy assignment used for container-group diagnostics (`41ebf9df-66cb-48e9-a8d0-98afb4e150ce`).
- [Remediate non-compliant resources](https://learn.microsoft.com/en-us/azure/governance/policy/how-to/remediate-resources) and [the deployIfNotExists effect](https://learn.microsoft.com/en-us/azure/governance/policy/concepts/effect-deploy-if-not-exists) — that the assignment's managed identity needs explicit role grants when the assignment is created outside the portal.
- [Azure built-in roles for Monitor](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/monitor) — `Monitoring Contributor` and `Log Analytics Contributor` and their role-definition IDs.
