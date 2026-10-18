###############################################################################
# Naming
#
# Azure's naming rules differ per resource type, so rather than hope one pattern
# fits everything each name is derived explicitly. Globally-unique names (Key
# Vault, Container Registry, PostgreSQL server) carry a random suffix so two
# deployments of this module never collide.
###############################################################################

resource "random_string" "suffix" {
  length  = 4
  special = false
  upper   = false
  numeric = true
}

# The subscription the deployment lands in. Only needed explicitly because the
# controller must be told where its workers job lives.
data "azurerm_client_config" "current" {}

locals {
  base = "${var.name_prefix}-${var.environment}"

  subscription_id = coalesce(var.subscription_id, data.azurerm_client_config.current.subscription_id)

  resource_group_name = coalesce(var.resource_group_name, "${local.base}-rg")

  # Created by the module, or looked up when create_resource_group is false.
  resource_group = var.create_resource_group ? azurerm_resource_group.this[0] : data.azurerm_resource_group.this[0]

  vnet_name = "${local.base}-vnet"

  subnet_container_apps    = "snet-cae"
  subnet_postgres          = "snet-postgres"
  subnet_private_endpoints = "snet-pe"

  log_analytics_workspace_name = "${local.base}-log"

  # Key Vault: 3-24 characters, alphanumerics and hyphens, globally unique.
  key_vault_name = substr("${var.name_prefix}-${var.environment}-kv-${random_string.suffix.result}", 0, 24)

  # Container Registry: 5-50 alphanumerics only, globally unique. No hyphens.
  container_registry_name = coalesce(
    var.container_registry_name,
    substr("${var.name_prefix}${var.environment}${random_string.suffix.result}", 0, 50),
  )

  container_registry_login_server = var.create_container_registry ? azurerm_container_registry.this[0].login_server : var.existing_container_registry_login_server
  container_registry_id           = var.create_container_registry ? azurerm_container_registry.this[0].id : var.existing_container_registry_id

  # PostgreSQL server: 3-63 characters, lowercase letters, digits and hyphens.
  postgres_server_name = "${local.base}-pg"

  # Storage account: 3-24 characters, lowercase letters and digits only, globally
  # unique, no hyphens. name_prefix and environment are both validated as
  # lowercase alphanumeric, so a bare concatenation plus the random suffix is
  # already legal; substr guards the 24-character cap (12 + 8 + 4 is the worst
  # case, which exactly fits).
  storage_account_name = substr("${var.name_prefix}${var.environment}${random_string.suffix.result}", 0, 24)

  container_app_environment_name = "${local.base}-cae"
  web_app_name                   = "${local.base}-web"
  controller_app_name            = "${local.base}-controller"
  workers_job_name               = "${local.base}-workers"
  provisioning_job_name          = "${local.base}-provision"

  app_identity_name          = "${local.base}-app-id"
  controller_identity_name   = "${local.base}-controller-id"
  provisioner_identity_name  = "${local.base}-provisioner-id"
  sandbox_pull_identity_name = "${local.base}-sandbox-pull-id"

  # Sandbox (ACI) names. The subnet name and NSG/NAT names are fixed or
  # deployment-derived; the container groups themselves are created by the
  # application at runtime and are not named here.
  sandbox_resource_group_name    = coalesce(var.sandbox_resource_group_name, "${local.base}-sandbox-rg")
  sandbox_subnet_name            = "snet-sandbox"
  sandbox_nsg_name               = "${local.base}-sandbox-nsg"
  sandbox_nat_gateway_name       = "${local.base}-sandbox-nat"
  sandbox_nat_public_ip_name     = "${local.base}-sandbox-nat-ip"
  sandbox_policy_assignment_name = "${local.base}-sandbox-diag"

  tags = merge(
    {
      "app"         = "lucent"
      "environment" = var.environment
      "managed-by"  = "terraform"
    },
    var.tags,
  )

  # Private DNS zone names. The asymmetry is an Azure requirement, not a style
  # choice:
  #   * a VNet-INTEGRATED PostgreSQL Flexible Server needs a zone ending in
  #     `.postgres.database.azure.com` (no `privatelink.` prefix);
  #   * services reached through a PRIVATE ENDPOINT use the `privatelink.*` form.
  postgres_dns_zone_name = "${local.base}-pg.private.postgres.database.azure.com"
  key_vault_dns_zone     = "privatelink.vaultcore.azure.net"
  acr_dns_zone           = "privatelink.azurecr.io"
  blob_dns_zone          = "privatelink.blob.core.windows.net"

  # The zone each private endpoint attaches to. When the module creates the
  # zone, use it; otherwise use the caller's existing zone (which Azure already
  # has linked to the VNet — see network.tf). The private endpoint resources
  # carry a precondition that rejects create = false with no existing id.
  key_vault_dns_zone_id = var.create_key_vault_private_dns_zone ? azurerm_private_dns_zone.key_vault[0].id : var.existing_key_vault_private_dns_zone_id
  acr_dns_zone_id       = var.create_acr_private_dns_zone ? azurerm_private_dns_zone.acr[0].id : var.existing_acr_private_dns_zone_id
  blob_dns_zone_id      = var.create_blob_private_dns_zone ? azurerm_private_dns_zone.blob[0].id : var.existing_blob_private_dns_zone_id

  # Subnet and workspace selection: created here, or taken from the caller.
  container_apps_subnet_id   = var.create_vnet ? azurerm_subnet.container_apps[0].id : var.existing_container_apps_subnet_id
  postgres_subnet_id         = var.create_vnet ? azurerm_subnet.postgres[0].id : var.existing_postgres_subnet_id
  private_endpoint_subnet_id = var.create_vnet ? azurerm_subnet.private_endpoints[0].id : var.existing_private_endpoint_subnet_id
  vnet_id                    = var.create_vnet ? azurerm_virtual_network.this[0].id : var.existing_vnet_id

  log_analytics_workspace_id = var.create_log_analytics_workspace ? azurerm_log_analytics_workspace.this[0].id : var.existing_log_analytics_workspace_id

  #############################################################################
  # Sandboxes (Azure Container Instances)
  #
  # sandbox_backend is a three-way switch. Only "aci" changes this module's
  # plan; "docker" (the default) and "kubernetes" create none of the resources
  # below, so a deployment that does not use ACI is byte-for-byte unchanged.
  #
  # Every sandbox lookup is conditional on sandbox_enabled. The conditional is
  # lazy, so the zero-count resource indexes are never evaluated in the default
  # docker case.
  #############################################################################
  sandbox_enabled = var.sandbox_backend == "aci"

  # The dedicated resource group the application creates container groups in.
  # It is deliberately separate from the main group: the app identity's custom
  # role is assigned HERE, so the role's container-group permissions cannot
  # reach any other resource the deployment owns.
  sandbox_resource_group_id = local.sandbox_enabled ? (
    var.create_sandbox_resource_group
    ? azurerm_resource_group.sandbox[0].id
    : data.azurerm_resource_group.sandbox[0].id
  ) : null

  # Container groups deployed into a VNet must be created in the same region as
  # that VNet. When the module owns the sandbox group it is var.location, which
  # is also the VNet's region. When an existing group is adopted, its own region
  # is used and must be checked by the operator (Terraform cannot compare it to
  # an existing VNet's region without another data source).
  sandbox_location = local.sandbox_enabled ? (
    var.create_sandbox_resource_group
    ? azurerm_resource_group.sandbox[0].location
    : data.azurerm_resource_group.sandbox[0].location
  ) : null

  # The delegated subnet: created here when the module owns the VNet, otherwise
  # supplied by the caller. The existing subnet must already be delegated to
  # Microsoft.ContainerInstance/containerGroups and must contain no other
  # resource type.
  sandbox_subnet_id = local.sandbox_enabled ? (
    var.create_vnet ? azurerm_subnet.sandbox[0].id : var.existing_sandbox_subnet_id
  ) : null

  # Source ranges allowed to reach TCP 19390 on a sandbox group. Microsoft
  # documents that port for PORTAL access (logs/shell) to VNet-deployed groups
  # and, potentially, the exec data channel; it is not an ACI health path.
  #
  # Default behavior:
  #   * module-owned VNet — the Container Apps subnet, because the application's
  #     container-exec call (the worker) originates there;
  #   * adopted VNet — nothing, because the existing subnet's CIDR is not known
  #     here. Set sandbox_management_source_prefixes explicitly for exec/portal.
  # An empty list disables the rule.
  sandbox_management_source_prefixes = (
    var.sandbox_management_source_prefixes != null
    ? var.sandbox_management_source_prefixes
    : (var.create_vnet ? [var.container_apps_subnet_prefix] : [])
  )

  # Destination set for sandbox outbound VNet traffic. Module-owned networking
  # scopes it to the private-endpoint subnet (Key Vault, ACR, Blob). Adopting a
  # VNet means the private-endpoint subnet CIDR is not known here, so the whole
  # VNet is admitted; the README calls that out explicitly.
  sandbox_vnet_outbound_prefixes = concat(
    var.create_vnet ? [var.private_endpoint_subnet_prefix] : ["VirtualNetwork"],
    var.sandbox_extra_outbound_prefixes,
  )

  # The resource-group scope a role definition that is assigned on the SUBNET
  # must be assignable at. With module-owned networking it is the main group;
  # with an existing VNet it is derived from existing_vnet_id, because the
  # subnet may live in a different resource group. The role definition grants
  # nothing by itself — only the assignment at the subnet does.
  #
  # existing_vnet_id carries a validation that rejects a malformed resource ID,
  # so a non-null value that cannot be parsed fails clearly instead of silently
  # falling back to the main resource group (which would make the role
  # definition assignable at the wrong scope). A null value is separately
  # rejected by the preconditions in rbac.tf; the guarded fallback here merely
  # keeps the expression evaluable while those report the real problem.
  sandbox_subnet_role_scope = var.create_vnet ? local.resource_group.id : (
    var.existing_vnet_id == null
    ? local.resource_group.id
    : regex("(?i)^(.+/resourceGroups/[^/]+)/providers/Microsoft[.]Network/virtualNetworks/[^/]+/?$", var.existing_vnet_id)[0]
  )

  sandbox_pull_identity_id        = local.sandbox_enabled ? azurerm_user_assigned_identity.sandbox_pull[0].id : null
  sandbox_pull_identity_client_id = local.sandbox_enabled ? azurerm_user_assigned_identity.sandbox_pull[0].client_id : null

  # The image the ACI service pulls for the trusted MCP bridge sidecar. Nothing
  # in this repository builds or pushes it; see the README. Defaulting to the
  # module's own registry is the only sensible guess, and it is still only a
  # guess until the image is pushed there.
  sandbox_bridge_image = coalesce(
    var.sandbox_bridge_image,
    format("%s/lucent-sandbox:base", local.container_registry_login_server),
  )
}

###############################################################################
# Generated secrets
#
# Generated only where the operator did not supply a value. Every one of these —
# supplied or generated — is written to Key Vault so the application reads it
# through a managed identity instead of a plain environment variable. The
# generated ones also land in Terraform state; see the README's note on that.
###############################################################################

resource "random_password" "postgres_admin" {
  count = var.postgres_admin_password == null ? 1 : 0

  length  = 32
  special = true
  # Azure Flexible Server rejects some punctuation in the admin password, so the
  # special set is restricted deliberately.
  override_special = "_-+="
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
}

resource "random_password" "postgres_daemon" {
  count = var.postgres_daemon_password == null ? 1 : 0

  length           = 32
  special          = true
  override_special = "_-+="
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
}

# The application role owns the schema; the provisioning job creates it and
# applies this password.
resource "random_password" "postgres_app" {
  length           = 32
  special          = true
  override_special = "_-+="
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
}

resource "random_password" "lucent_secret_key" {
  count = var.lucent_secret_key == null ? 1 : 0

  length  = 48
  special = false
}

resource "random_password" "lucent_signing_secret" {
  count = var.lucent_signing_secret == null ? 1 : 0

  length  = 48
  special = false
}

locals {
  postgres_admin_password  = var.postgres_admin_password != null ? var.postgres_admin_password : random_password.postgres_admin[0].result
  postgres_daemon_password = var.postgres_daemon_password != null ? var.postgres_daemon_password : random_password.postgres_daemon[0].result
  postgres_app_password    = random_password.postgres_app.result

  lucent_secret_key     = var.lucent_secret_key != null ? var.lucent_secret_key : random_password.lucent_secret_key[0].result
  lucent_signing_secret = var.lucent_signing_secret != null ? var.lucent_signing_secret : random_password.lucent_signing_secret[0].result

  # Connection strings. Two deliberate choices:
  #
  #   * sslmode=require is stated explicitly rather than relying on asyncpg's
  #     default of `prefer`, so a TLS misconfiguration fails loudly instead of
  #     silently falling back to cleartext.
  #   * the password is percent-encoded. Generated passwords only ever use the
  #     characters `_-+=` (see override_special above), which survive a URL
  #     unchanged, but an operator-supplied password might contain anything —
  #     and a bare `@` or `/` would silently truncate the host.
  #
  # The host is the server's VNet-integrated FQDN, which resolves to a private
  # address inside the VNet through the linked private DNS zone. There is no
  # public endpoint to fall back to.
  database_url = format(
    "postgresql://%s:%s@%s:5432/%s?sslmode=require",
    var.postgres_app_role,
    urlencode(local.postgres_app_password),
    azurerm_postgresql_flexible_server.this.fqdn,
    var.postgres_database_name,
  )

  daemon_database_url = format(
    "postgresql://%s:%s@%s:5432/%s?sslmode=require",
    var.postgres_daemon_role,
    urlencode(local.postgres_daemon_password),
    azurerm_postgresql_flexible_server.this.fqdn,
    var.postgres_database_name,
  )

  # Optional operator-supplied secrets. Nulls are dropped rather than stored as
  # empty strings — a missing GITHUB_TOKEN fails more legibly than an empty one.
  optional_secrets = {
    GITHUB_TOKEN       = var.github_token
    ANTHROPIC_API_KEY  = var.anthropic_api_key
    OPENAI_API_KEY     = var.openai_api_key
    GOOGLE_API_KEY     = var.google_api_key
    LUCENT_LICENSE_KEY = var.lucent_license_key
  }

  provided_optional_secrets = { for k, v in local.optional_secrets : k => v if v != null }

  # Everything that must live in Key Vault. Keys are the environment variable
  # names Lucent expects; the Key Vault secret name is the same string with
  # underscores replaced by hyphens (Key Vault forbids underscores).
  application_secrets = merge(
    {
      DATABASE_URL          = local.database_url
      DAEMON_DATABASE_URL   = local.daemon_database_url
      LUCENT_SECRET_KEY     = local.lucent_secret_key
      LUCENT_SIGNING_SECRET = local.lucent_signing_secret
    },
    local.provided_optional_secrets,
    var.extra_secrets,
  )

  # Derived from the KEY SET, not from application_secrets. Deriving from the
  # sensitive map taints the names too, which makes the
  # key_vault_application_secret_names output sensitive and breaks any
  # consumer's plain root output. The keys are environment-variable names and
  # carry no secret material; the values stay sensitive in the secret resource.
  key_vault_secret_name = { for env_name in local.application_secret_keys : env_name => replace(env_name, "_", "-") }

  # Key sets for `for_each`.
  #
  # `for_each` refuses any value Terraform marks sensitive, and
  # application_secrets necessarily is: its values are the database passwords,
  # the generated keys and every operator-supplied extra secret, sourced from
  # variables declared `sensitive = true`. The KEYS, however, are only the
  # environment variable names the containers see (DATABASE_URL, GITHUB_TOKEN,
  # ...) and carry no secret material. Every resource iterates this key set and
  # reads its value with `local.application_secrets[each.key]` inside the
  # resource, so the values stay sensitive in state, in plan output and in
  # outputs; only the names are declassified here. Iterating the map itself, or
  # marking application_secrets non-sensitive, is not an option: the first is
  # the plan-time error this avoids, and the second would expose the values.
  application_secret_keys = nonsensitive(toset(keys(local.application_secrets)))

  # The web/API app never needs the daemon's restricted database credential —
  # only the daemon and the workers it spawns use DAEMON_DATABASE_URL. Withholding
  # it means the one process class that faces the internet does not hold a
  # credential it could leak. The converse is not done: the daemon keeps
  # DATABASE_URL, because the launcher's environment allowlist passes it through
  # and the worker code may fall back to it.
  #
  # This is the same filtering as the web container's secret/env blocks, applied
  # to the non-sensitive key set so those blocks can iterate it. The value is
  # still read from application_secrets by key, where it remains sensitive.
  web_secret_keys = setsubtract(local.application_secret_keys, toset(["DAEMON_DATABASE_URL"]))

  # The web app's stable FQDN is "<app name>.<environment default domain>", and
  # it is derived from the ENVIRONMENT rather than read off the web app's own
  # `ingress[0].fqdn`. That is not a cosmetic choice: the web app's environment
  # variables need this value, so reading it back from the container app would be
  # a dependency cycle (web app -> env -> web app). The environment's
  # default_domain does not depend on any container app, so the cycle disappears
  # and the value is merely "known after apply".
  web_fqdn = format("%s.%s", local.web_app_name, azurerm_container_app_environment.this.default_domain)

  # Public URL: the operator's own domain when they set one, otherwise the
  # Container Apps default domain Azure assigns.
  public_url = coalesce(var.lucent_public_url, format("https://%s", local.web_fqdn))

  # Non-secret configuration shared by every process class.
  common_environment = merge(
    {
      LUCENT_MODE            = var.lucent_mode
      LUCENT_LOG_LEVEL       = var.lucent_log_level
      LUCENT_SECRET_PROVIDER = "builtin"
      PYTHONUNBUFFERED       = "1"
    },
    var.extra_environment,
  )

  # Sandbox configuration for processes that DISPATCH tasks.
  #
  # This is the piece the daemon and the per-organization workers need, and it
  # is deliberately separate from sandbox_environment below (the web app's
  # view). A task sandbox is created by the dispatcher, and the dispatcher only
  # runs inside a worker: see daemon/sandbox/lifecycle.py's
  # _create_task_sandbox and its only caller in daemon/daemon.py. The worker is
  # a separate Container Apps Job execution, and daemon/launcher/base.py builds
  # its environment from the CONTROLLER's environment through
  # WORKER_ENV_ALLOWLIST — so these values must exist in the controller's
  # environment to be inherited. They are also written into the workers job
  # template, which is the base execution template the Azure launcher copies.
  #
  # AZURE_CLIENT_ID is intentionally NOT here. The controller's own
  # AZURE_CLIENT_ID must remain the CONTROLLER identity so it can start the
  # workers job. Workers, however, run as the APP identity, so the app
  # identity's client id travels under a distinct name,
  # LUCENT_WORKER_AZURE_CLIENT_ID. The application-side worker allowlist maps
  # that name to AZURE_CLIENT_ID in the worker process only; nothing in this
  # module changes the controller's AZURE_CLIENT_ID.
  #
  # The whole map is empty when sandbox_backend is not "aci", which keeps a
  # docker deployment unchanged.
  sandbox_worker_environment = local.sandbox_enabled ? {
    LUCENT_SANDBOX_BACKEND            = "aci"
    LUCENT_SANDBOX_ACI_RESOURCE_GROUP = local.sandbox_resource_group_name
    LUCENT_SANDBOX_ACI_SUBNET_ID      = local.sandbox_subnet_id
    LUCENT_SANDBOX_ACI_IDENTITY_ID    = local.sandbox_pull_identity_id
    LUCENT_SANDBOX_ACI_LOCATION       = local.sandbox_location
    LUCENT_SANDBOX_BRIDGE_IMAGE       = local.sandbox_bridge_image
    LUCENT_SANDBOX_BRIDGE_API_URL     = format("%s/api", trimsuffix(local.public_url, "/"))
    # Not in the LUCENT_SANDBOX_* contract, but required by the backend: the
    # Azure SDK's ContainerInstanceManagementClient is constructed with a
    # subscription id.
    AZURE_SUBSCRIPTION_ID = local.subscription_id
    # Contract for the app-side worker allowlist: map this to AZURE_CLIENT_ID
    # ONLY in the worker, so DefaultAzureCredential selects the app identity
    # (the identity that holds the sandbox RBAC). See the block comment above.
    LUCENT_WORKER_AZURE_CLIENT_ID = azurerm_user_assigned_identity.app.client_id
  } : {}

  # The controller and the workers reach the web app's MCP endpoint. The host
  # also feeds Lucent's MCP DNS-rebinding allowlist, which is derived from
  # LUCENT_MCP_URL and LUCENT_PUBLIC_URL, so the container FQDN must appear here
  # or the daemon's MCP calls are rejected with HTTP 421.
  #
  # HTTPS, not HTTP, matching the rest of the deployment. The web app's ingress
  # has allow_insecure_connections = false and Container Apps serves the app FQDN
  # over HTTPS on 443 with its free managed certificate; plain HTTP on 80 is only
  # a redirect, which an MCP client would not follow. An external environment is
  # reachable from other apps in the same environment over that same FQDN, so
  # nothing is gained by downgrading to cleartext internally.
  mcp_url = format("https://%s/mcp", local.web_fqdn)

  # The controller and every worker it starts share this. The sandbox map is
  # merged before the fixed values so it is inherited by the workers job (whose
  # template uses this map) and by the controller (which merges it into
  # controller_environment below).
  daemon_environment = merge(
    local.common_environment,
    local.sandbox_worker_environment,
    {
      LUCENT_MCP_URL                 = local.mcp_url
      LUCENT_PUBLIC_URL              = local.public_url
      LUCENT_LLM_ENGINE              = var.lucent_llm_engine
      LUCENT_DAEMON_INTERVAL         = "15"
      LUCENT_STALE_HEARTBEAT_MINUTES = "30"
    },
  )

  # File storage for user files.
  #
  # LUCENT_FILE_STORAGE_PATH is kept even when the Azure provider is active: the
  # registry in src/lucent/storage/providers.py always registers the local
  # provider so that a row written before the switch (provider='local') still
  # resolves. Note what that does and does not mean on Container Apps: the ROW
  # resolves, but the bytes on a replica's local disk do not survive a restart
  # or scale event and are not visible to another replica. Keeping the local
  # provider registered is compatibility for a row's provider name, not durable
  # storage.
  #
  # The Azure variables are exactly the ones the application-side provider reads:
  # LUCENT_AZURE_STORAGE_ACCOUNT_URL and LUCENT_AZURE_STORAGE_CONTAINER come from
  # src/lucent/storage/{providers,azure_blob}.py, and the active provider is
  # chosen by LUCENT_FILE_STORAGE_PROVIDER (the provider registers itself as
  # "azure"). They are set unconditionally, not only when
  # lucent_file_storage_provider == "azure", for the same reason the local path
  # is kept: a stored row that names either provider must stay resolvable.
  #
  # AZURE_CLIENT_ID is what makes DefaultAzureCredential select this container's
  # user-assigned identity rather than stalling on the chain — the blob provider
  # authenticates with managed identity only, never a key (the account rejects
  # shared-key requests). The controller is given the same variable with its own
  # identity in controller_environment below.
  file_storage_environment = {
    LUCENT_FILE_STORAGE_PROVIDER     = var.lucent_file_storage_provider
    LUCENT_AZURE_STORAGE_ACCOUNT_URL = azurerm_storage_account.files.primary_blob_endpoint
    LUCENT_AZURE_STORAGE_CONTAINER   = azurerm_storage_container.files.name
    AZURE_CLIENT_ID                  = azurerm_user_assigned_identity.app.client_id
  }

  # Sandbox configuration for the ACI backend, injected into the WEB APP.
  #
  # The web app is not the sandbox creator — the dispatcher in a per-org worker
  # is, and it gets its own copy through sandbox_worker_environment above. The
  # web app is still configured because it owns the sandbox API/tools surface
  # and reads the same settings. The names are the application's contract, not
  # this module's invention — the ACI backend reads exactly these strings.
  #
  # The whole map is empty when sandbox_backend is not "aci", which is what
  # keeps a docker deployment unchanged: the web app is not given a backend
  # selector at all and falls back to the application default.
  #
  # LUCENT_SANDBOX_BRIDGE_API_URL is the Lucent server's API base, because the
  # bridge runs INSIDE the sandbox network namespace and calls paths like
  # /memories and /requests/tasks/... (src/lucent/sandbox/mcp_bridge.py) against
  # a server that mounts those routers under /api. local.public_url is the
  # server's origin (custom domain when one is configured, otherwise the
  # Container Apps FQDN); the /api suffix is what makes the concatenation
  # resolve. It is public ingress, so the sandbox reaches it through the NAT
  # gateway — see network.tf.
  sandbox_environment = local.sandbox_enabled ? {
    LUCENT_SANDBOX_BACKEND            = "aci"
    LUCENT_SANDBOX_ACI_RESOURCE_GROUP = local.sandbox_resource_group_name
    LUCENT_SANDBOX_ACI_SUBNET_ID      = local.sandbox_subnet_id
    LUCENT_SANDBOX_ACI_IDENTITY_ID    = local.sandbox_pull_identity_id
    LUCENT_SANDBOX_ACI_LOCATION       = local.sandbox_location
    LUCENT_SANDBOX_BRIDGE_IMAGE       = local.sandbox_bridge_image
    LUCENT_SANDBOX_BRIDGE_API_URL     = format("%s/api", trimsuffix(local.public_url, "/"))
    # Not in the LUCENT_SANDBOX_* contract, but required by the backend: the
    # Azure SDK's ContainerInstanceManagementClient is constructed with a
    # subscription id. AZURE_CLIENT_ID (the app identity) already comes from
    # file_storage_environment above, so DefaultAzureCredential selects the
    # right principal; this names the subscription those calls are made in.
    AZURE_SUBSCRIPTION_ID = local.subscription_id
  } : {}

  web_environment = merge(
    local.common_environment,
    local.file_storage_environment,
    local.sandbox_environment,
    {
      LUCENT_HOST              = "0.0.0.0"
      LUCENT_PORT              = "8766"
      LUCENT_PUBLIC_URL        = local.public_url
      LUCENT_LLM_ENGINE        = var.lucent_llm_engine
      LUCENT_FILE_STORAGE_PATH = "/var/lib/lucent/files"
    },
  )

  # The one-shot bootstrap job runs as the PROVISIONER identity, so it must not
  # inherit web_environment: that carries file_storage_environment, whose
  # AZURE_CLIENT_ID is the app identity's client id and whose storage endpoints
  # are not granted to the provisioner. Any Azure SDK call in the job would then
  # select the wrong principal and get a 403 (latent today — the migrate and
  # bootstrap CLIs make no Azure calls). It gets the shared non-secret
  # configuration, and AZURE_CLIENT_ID names its OWN identity so a future Azure
  # call resolves correctly.
  bootstrap_environment = merge(
    local.common_environment,
    {
      AZURE_CLIENT_ID = azurerm_user_assigned_identity.provisioner.client_id
    },
  )

  # The controller needs everything the daemon needs, plus enough to find and
  # start the workers job. daemon/launcher/azure_launcher.py reads these
  # (LUCENT_WORKER_JOB_NAME / _RESOURCE_GROUP / _CONTAINER_APP_ENV /
  # _SUBSCRIPTION_ID), falling back to AZURE_SUBSCRIPTION_ID.
  #
  # AZURE_CLIENT_ID is what makes DefaultAzureCredential select the controller's
  # user-assigned identity instead of the system-assigned one — there is no other
  # identity on this app, but without it the credential chain can stall or pick
  # the wrong principal.
  controller_environment = merge(
    local.daemon_environment,
    var.worker_launcher == "azure" ? {
      LUCENT_WORKER_LAUNCHER          = "azure"
      LUCENT_WORKER_JOB_NAME          = local.workers_job_name
      LUCENT_WORKER_RESOURCE_GROUP    = local.resource_group_name
      LUCENT_WORKER_CONTAINER_APP_ENV = local.container_app_environment_name
      LUCENT_WORKER_SUBSCRIPTION_ID   = local.subscription_id
      LUCENT_WORKER_IMAGE             = var.image
      AZURE_CLIENT_ID                 = azurerm_user_assigned_identity.controller.client_id
      AZURE_SUBSCRIPTION_ID           = local.subscription_id
      } : {
      LUCENT_WORKER_LAUNCHER = var.worker_launcher
    },
  )
}
