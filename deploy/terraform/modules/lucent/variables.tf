###############################################################################
# Naming and placement
###############################################################################

variable "name_prefix" {
  description = "Prefix used to derive the name of every resource this module creates. Keep it short (<= 12 characters) — Azure Container Registry names are globally unique, alphanumeric-only, and derived from this prefix plus a random suffix."
  type        = string
  default     = "lucent"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,11}$", var.name_prefix))
    error_message = "name_prefix must start with a lowercase letter and contain only lowercase letters and digits (max 12 characters)."
  }
}

variable "environment" {
  description = "Short deployment-environment label folded into resource names (for example \"beta\", \"staging\", \"prod\")."
  type        = string
  default     = "beta"

  validation {
    condition     = can(regex("^[a-z0-9]{2,8}$", var.environment))
    error_message = "environment must be 2-8 lowercase alphanumeric characters."
  }
}

variable "location" {
  description = "Azure region for every resource. Must be a region that offers PostgreSQL Flexible Server, Container Apps and the SKUs you select."
  type        = string
  default     = "eastus2"
}

variable "resource_group_name" {
  description = "Name of the resource group to create. Set to null to use an existing group named <name_prefix>-<environment>-rg (the group is then adopted via a data source rather than created)."
  type        = string
  default     = null
}

variable "create_resource_group" {
  description = "When true, Terraform creates the resource group. Set to false to deploy into an existing resource group named by resource_group_name."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every taggable resource created by this module."
  type        = map(string)
  default     = {}
}

###############################################################################
# Container image
###############################################################################

variable "image" {
  description = "Fully-qualified container image reference for the Lucent application (web/API container, controller and workers all run the same image). Terraform never builds or pushes an image — see deploy/terraform/README.md for what must happen before apply. Example: \"acrlucentbeta.azurecr.io/lucent:0.4.0\"."
  type        = string

  validation {
    condition     = length(trimspace(var.image)) > 0
    error_message = "image must be a non-empty container image reference."
  }
}

variable "bootstrap_image" {
  description = "Image used by the one-shot database provisioning job. It must contain a psql client recent enough to support \\getenv (PostgreSQL 15 or later) — the job reads the role passwords from the environment with that meta-command rather than passing them on the psql command line. The default is the public postgres image; substitute a mirror if your environment forbids Docker Hub egress."
  type        = string
  default     = "postgres:16-alpine"
}

###############################################################################
# Networking
###############################################################################

variable "vnet_address_space" {
  description = "Address space for the virtual network created by this module. Only used when create_vnet is true."
  type        = list(string)
  default     = ["10.42.0.0/16"]
}

variable "create_vnet" {
  description = "When true the module creates the virtual network and all subnets. When false, existing_vnet_id and the three existing_*_subnet_id variables must all be supplied."
  type        = bool
  default     = true
}

variable "existing_vnet_id" {
  description = "Resource ID of an existing virtual network to place subnets in. Required when create_vnet is false. It must be a full ARM resource ID (/subscriptions/.../resourceGroups/.../providers/Microsoft.Network/virtualNetworks/...) because the sandbox subnet-join role definition is made assignable at the VNet's resource group, which is parsed from this ID."
  type        = string
  default     = null

  validation {
    condition = var.existing_vnet_id == null ? true : can(regex(
      "(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft[.]Network/virtualNetworks/[^/]+/?$",
      var.existing_vnet_id,
    ))
    error_message = "existing_vnet_id must be a full ARM virtual network resource ID of the form /subscriptions/<subscription>/resourceGroups/<resource group>/providers/Microsoft.Network/virtualNetworks/<name>. The sandbox subnet-join role definition's assignable scope is derived from it, and a malformed ID would otherwise silently fall back to the main resource group."
  }
}

variable "container_apps_subnet_prefix" {
  description = "CIDR for the subnet dedicated to the Container Apps environment. Azure requires at least a /23 for a Consumption-only environment (which is what this module creates) and at least a /27 for a workload-profiles environment. The subnet must not be delegated."
  type        = string
  default     = "10.42.0.0/23"
}

variable "postgres_subnet_prefix" {
  description = "CIDR for the subnet delegated to PostgreSQL Flexible Server VNet integration. Azure requires at least a /28; a /24 leaves room to grow. The subnet must be delegated to Microsoft.DBforPostgreSQL/flexibleServers."
  type        = string
  default     = "10.42.2.0/24"
}

variable "private_endpoint_subnet_prefix" {
  description = "CIDR for the subnet that holds the private endpoints for Key Vault, Container Registry and Blob Storage."
  type        = string
  default     = "10.42.3.0/24"
}

variable "existing_container_apps_subnet_id" {
  description = "Resource ID of an existing subnet for the Container Apps environment. Required when create_vnet is false."
  type        = string
  default     = null
}

variable "existing_postgres_subnet_id" {
  description = "Resource ID of an existing delegated subnet for PostgreSQL Flexible Server. Required when create_vnet is false."
  type        = string
  default     = null
}

variable "existing_private_endpoint_subnet_id" {
  description = "Resource ID of an existing subnet for private endpoints. Required when create_vnet is false."
  type        = string
  default     = null
}

###############################################################################
# Private DNS zones for private endpoints
#
# The Key Vault, Container Registry and Blob Storage zones have fixed,
# product-wide names. Azure rejects linking two zones with the same name to one
# VNet, so composing into an existing VNet that already owns one of them (normal
# when another service uses a private endpoint there) fails if this module
# creates its own. Set the matching create_* flag to false and supply the
# existing_*_id. The existing zone must already be linked to the target VNet;
# that is the situation the duplicate-zone failure implies.
###############################################################################

variable "create_blob_private_dns_zone" {
  description = "Create the privatelink.blob.core.windows.net zone and link it to the VNet. Set false, and supply existing_blob_private_dns_zone_id, when the target VNet already has that zone (Azure rejects a second zone with the same name linked to the same VNet). In that mode the module creates neither the zone nor a VNet link."
  type        = bool
  default     = true
}

variable "existing_blob_private_dns_zone_id" {
  description = "Resource ID of an existing privatelink.blob.core.windows.net private DNS zone to attach the blob private endpoint to. Required when create_blob_private_dns_zone is false. The zone must already be linked to the VNet."
  type        = string
  default     = null
}

variable "create_key_vault_private_dns_zone" {
  description = "Create the privatelink.vaultcore.azure.net zone and link it to the VNet. Set false, and supply existing_key_vault_private_dns_zone_id, when the target VNet already has that zone."
  type        = bool
  default     = true
}

variable "existing_key_vault_private_dns_zone_id" {
  description = "Resource ID of an existing privatelink.vaultcore.azure.net private DNS zone to attach the Key Vault private endpoint to. Required when create_key_vault_private_dns_zone is false. The zone must already be linked to the VNet."
  type        = string
  default     = null
}

variable "create_acr_private_dns_zone" {
  description = "Create the privatelink.azurecr.io zone and link it to the VNet. Set false, and supply existing_acr_private_dns_zone_id, when the target VNet already has that zone."
  type        = bool
  default     = true
}

variable "existing_acr_private_dns_zone_id" {
  description = "Resource ID of an existing privatelink.azurecr.io private DNS zone to attach the registry private endpoint to. Required when create_acr_private_dns_zone is false. The zone must already be linked to the VNet."
  type        = string
  default     = null
}

###############################################################################
# PostgreSQL Flexible Server
###############################################################################

variable "postgres_version" {
  description = "Major version of PostgreSQL. Lucent requires 16 or later for the features its migrations use."
  type        = string
  default     = "16"
}

variable "postgres_sku_name" {
  description = "SKU for the Flexible Server. B_Standard_B1ms is the cheapest tier that runs the workload but Microsoft does not recommend burstable SKUs for production. Consider B_Standard_B2s or GP_Standard_D2s_v3 for a real beta."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  description = "Storage size in MiB for the Flexible Server."
  type        = number
  default     = 32768
}

variable "postgres_backup_retention_days" {
  description = "Point-in-time-restore backup retention in days."
  type        = number
  default     = 7
}

variable "postgres_zone" {
  description = "Availability zone for the primary server. Set to null to let Azure choose."
  type        = string
  default     = null
}

variable "postgres_high_availability_enabled" {
  description = "Enable zone-redundant high availability. Roughly doubles the database cost and requires a region with availability zones."
  type        = bool
  default     = false
}

variable "postgres_extensions" {
  description = "Extensions to allowlist in the server's azure.extensions parameter. pg_trgm is mandatory for Lucent's trigram search. UUID-OSSP is only needed for the legacy uuid_generate_v4() default; the design doc recommends migrating to the core gen_random_uuid() and dropping it."
  type        = list(string)
  default     = ["PG_TRGM"]
}

variable "postgres_similarity_threshold" {
  description = "Value applied to the database for pg_trgm.similarity_threshold. docker/postgres-init.sh sets 0.3; keep them in step."
  type        = string
  default     = "0.3"
}

variable "postgres_admin_login" {
  description = "Administrator login name for password authentication. The app connects with this password (see postgres_password_auth_enabled) because Lucent's asyncpg pool builds its DSN from DATABASE_URL and has no Entra ID token support today."
  type        = string
  default     = "lucentadmin"
}

variable "postgres_admin_password" {
  description = "Administrator password. Leave null to have Terraform generate a strong random password. Either way the value is stored in Terraform state and in Key Vault — read the README's state-sensitivity note."
  type        = string
  default     = null
  sensitive   = true
}

variable "postgres_password_auth_enabled" {
  description = "Enable PostgreSQL password authentication. This must stay true while Lucent builds DATABASE_URL from a username and password: asyncpg is given a plain DSN and the application has no Entra ID token provider. Entra ID authentication is enabled separately for administrative use."
  type        = bool
  default     = true
}

variable "postgres_max_connections" {
  description = "Override the server's max_connections parameter. Lucent's pool opens min 2 / max 10 connections per process (src/lucent/db/pool.py), so every replica plus every warm worker consumes up to 10. B1ms defaults to about 50, which is tight for more than a couple of processes. Set to null to leave the platform default."
  type        = number
  default     = null
}

variable "postgres_database_name" {
  description = "Name of the application database."
  type        = string
  default     = "lucent"
}

variable "postgres_app_role" {
  description = "Name of the owner role the application connects as and whose default privileges the daemon role relies on (ALTER DEFAULT PRIVILEGES FOR ROLE <this>)."
  type        = string
  default     = "lucent"
}

variable "postgres_daemon_role" {
  description = "Name of the least-privilege role the daemon and workers connect as. Lucent's migrations only GRANT to it behind IF EXISTS guards; nothing creates it, so the provisioning job creates it."
  type        = string
  default     = "lucent_daemon"
}

variable "postgres_daemon_password" {
  description = "Password for the daemon role. Leave null to have Terraform generate one. The provisioning job applies it with ALTER ROLE and the app is handed the matching DAEMON_DATABASE_URL from Key Vault."
  type        = string
  default     = null
  sensitive   = true
}

###############################################################################
# Entra ID
###############################################################################

variable "tenant_id" {
  description = "Microsoft Entra ID tenant ID. Used for the Key Vault, the PostgreSQL Entra administrator and the role assignments."
  type        = string
}

variable "subscription_id" {
  description = "Azure subscription ID. Leave null to use the subscription of the credentials running Terraform. It is only needed explicitly because the daemon controller has to be told which subscription its workers job lives in (LUCENT_WORKER_SUBSCRIPTION_ID) for the Azure worker launcher."
  type        = string
  default     = null
}

variable "entra_admin_object_id" {
  description = "Object ID of the Entra ID principal granted administrator rights on the PostgreSQL server, for human/interactive administration with an Entra identity (psql, the portal). A security group is strongly preferred over an individual user. This is NOT the identity that runs CREATE EXTENSION: the provisioning job connects as the server administrator login (postgres_admin_login), which Flexible Server places in azure_pg_admin. Leave null to skip registering an Entra administrator; provisioning still works."
  type        = string
  default     = null
}

variable "entra_admin_principal_name" {
  description = "Display name of the Entra ID principal named by entra_admin_object_id."
  type        = string
  default     = null
}

variable "entra_admin_principal_type" {
  description = "Type of the Entra ID principal: User, Group or ServicePrincipal."
  type        = string
  default     = "Group"

  validation {
    condition     = contains(["User", "Group", "ServicePrincipal"], var.entra_admin_principal_type)
    error_message = "entra_admin_principal_type must be one of User, Group or ServicePrincipal."
  }
}

###############################################################################
# Container Apps compute
###############################################################################

variable "web_min_replicas" {
  description = "Minimum replicas for the web/API app. 0 lets the app scale to zero and is the single biggest cost lever; inbound HTTP wakes it."
  type        = number
  default     = 0
}

variable "web_max_replicas" {
  description = "Maximum replicas for the web/API app. The default of 2 matches the design doc's \"1-2 replicas\" beta guidance, and is only safe because the migration runner now takes a session-level advisory lock (src/lucent/db/pool.py, _migration_lock) — before that, two replicas starting together raced and the loser aborted startup. Do not raise this much further without also reading the connection-limit note: each replica opens up to 10 PostgreSQL connections, and per-replica in-memory rate-limit and settings caches mean behaviour is per-replica."
  type        = number
  default     = 2
}

variable "web_cpu" {
  description = "vCPU for the web/API container. Must form one of Azure's allowed vCPU/memory pairs (see the README) and, in a Consumption-only environment, must not exceed 2.0."
  type        = number
  default     = 1.0
}

variable "web_memory" {
  description = "Memory for the web/API container, as an Azure quantity string such as \"2Gi\". Must pair with web_cpu."
  type        = string
  default     = "2Gi"
}

variable "controller_cpu" {
  description = "vCPU for the daemon controller container. Small on purpose: the controller is largely idle in a beta deployment."
  type        = number
  default     = 0.5
}

variable "controller_memory" {
  description = "Memory for the daemon controller container. Must pair with controller_cpu."
  type        = string
  default     = "1Gi"
}

variable "worker_cpu" {
  description = "vCPU requested for the workers job."
  type        = number
  default     = 1.0
}

variable "worker_memory" {
  description = "Memory requested for the workers job. Must pair with worker_cpu."
  type        = string
  default     = "2Gi"
}

variable "worker_job_parallelism" {
  description = "Maximum number of worker replicas the job may run concurrently. Workers are one-per-organization, so this bounds concurrent organizations."
  type        = number
  default     = 10
}

variable "worker_replica_timeout_seconds" {
  description = "Maximum seconds a single worker replica may run before it is killed. Design notes say workers run for minutes to hours; the default allows two hours."
  type        = number
  default     = 7200
}

variable "worker_replica_retry_limit" {
  description = "Times a worker replica is retried after failure. Deliberately 0: a worker must never restart itself, the controller owns its lifecycle."
  type        = number
  default     = 0
}

variable "worker_launcher" {
  description = "Value for LUCENT_WORKER_LAUNCHER on the controller, and the launcher the controller uses to spawn workers. \"azure\" drives the Container Apps job created here through daemon/launcher/azure_launcher.py. Set to \"docker\" or \"kubernetes\" only if you are running a different image that lacks the Azure backend. The import is lazy (get_launcher imports the backend on first reconcile), so an image without the Azure module does not fail to boot — the supervisor logs a reconcile error once per pass until the value matches a launcher the image actually ships."
  type        = string
  default     = "azure"

  validation {
    condition     = contains(["azure", "docker", "kubernetes"], var.worker_launcher)
    error_message = "worker_launcher must be one of azure, docker or kubernetes."
  }
}

###############################################################################
# Observability
###############################################################################

variable "log_analytics_retention_days" {
  description = "Retention for the Log Analytics workspace. The first 5 GB per month are free, so a small beta typically costs nothing here."
  type        = number
  default     = 30
}

variable "create_log_analytics_workspace" {
  description = "When false, existing_log_analytics_workspace_id must be supplied and the workspace is not created."
  type        = bool
  default     = true
}

variable "existing_log_analytics_workspace_id" {
  description = "Resource ID of an existing Log Analytics workspace to attach the Container Apps environment to."
  type        = string
  default     = null
}

###############################################################################
# Container registry
###############################################################################

variable "create_container_registry" {
  description = "When false, existing_container_registry_id and existing_container_registry_login_server must be supplied and no registry is created."
  type        = bool
  default     = true
}

variable "container_registry_sku" {
  description = "SKU for Azure Container Registry. This module creates a private endpoint and, by default, disables public network access — and Azure supports BOTH of those only on Premium. Basic and Standard registries cannot use a private endpoint and cannot have public access disabled. Premium is roughly $50/month against Basic at about $5, so this SKU is the one honest cost consequence of the private-registry posture; set container_registry_private_endpoint_enabled = false and container_registry_public_network_access_enabled = true to run a cheaper public registry instead."
  type        = string
  default     = "Premium"

  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.container_registry_sku)
    error_message = "container_registry_sku must be one of Basic, Standard or Premium."
  }
}

variable "container_registry_name" {
  description = "Globally unique ACR name (alphanumeric only, 5-50 characters). Leave null to derive one from name_prefix and environment plus a random suffix."
  type        = string
  default     = null
}

variable "existing_container_registry_id" {
  description = "Resource ID of an existing container registry to pull from."
  type        = string
  default     = null
}

variable "existing_container_registry_login_server" {
  description = "Login server of an existing container registry, for example \"myregistry.azurecr.io\"."
  type        = string
  default     = null
}

variable "container_registry_public_network_access_enabled" {
  description = "Allow public network access to the container registry. Defaults to false, matching the design doc's private-networking posture, and the registry is given a private endpoint by default. Disabling public access requires the Premium SKU; the module rejects the combination on a SKU that cannot provide it. Operational consequence: with public access off, an image cannot be pushed from an operator workstation. Push from a host inside the VNet, or set this true for the first push and turn it back off."
  type        = bool
  default     = false
}

variable "container_registry_private_endpoint_enabled" {
  description = "Create a private endpoint for the container registry. This requires the Premium SKU — Azure rejects a private endpoint on a Basic or Standard registry — and the module fails fast if the two are combined. Set this to false only when you deliberately run a cheaper public registry (container_registry_public_network_access_enabled = true) and reach it over its public login server."
  type        = bool
  default     = true
}

###############################################################################
# Key Vault and secrets
###############################################################################

variable "key_vault_purge_protection_enabled" {
  description = "Enable purge protection on the Key Vault. Strongly recommended, but it means a destroyed vault cannot be purged before its retention expires — which makes it awkward in throwaway environments."
  type        = bool
  default     = true
}

variable "key_vault_soft_delete_retention_days" {
  description = "Soft-delete retention for the Key Vault, in days."
  type        = number
  default     = 7
}

variable "key_vault_public_network_access_enabled" {
  description = "Allow public network access to the Key Vault. Defaults to false, which is what the 'private Key Vault' posture means: the vault is reachable only through its private endpoint. There is a real consequence to plan for — Terraform writes the application secrets into the vault AND re-reads their values from the vault's data plane on every plan/refresh, so with public access off any Terraform run from outside the VNet that touches the secrets fails (HTTP 403 ForbiddenByConnection), not just the first apply. Adding your workstation to key_vault_allowed_ip_ranges does NOT help: IP rules are ignored while public access is disabled. The routes are to run Terraform from inside the VNet (or a host with private connectivity), to keep public access enabled (with key_vault_allowed_ip_ranges) while you manage secrets and disable it afterwards, or to use -refresh=false. See the README, 'Writing secrets into a private vault'."
  type        = bool
  default     = false
}

variable "key_vault_network_bypass" {
  description = "Trusted-services bypass for the Key Vault firewall: None or AzureServices. Microsoft's guidance for a private-endpoint-only vault is None. AzureServices lets the services on Microsoft's trusted-services list reach the vault data plane without a private endpoint — a broader set than this deployment's VNet — and, importantly, that bypass is NOT suppressed by disabling public network access. Set it to AzureServices only if a trusted Azure service (a backup or customer-managed-key scenario, for example) genuinely needs it."
  type        = string
  default     = "None"

  validation {
    condition     = contains(["AzureServices", "None"], var.key_vault_network_bypass)
    error_message = "key_vault_network_bypass must be either AzureServices or None."
  }
}

variable "key_vault_network_default_action" {
  description = "Default action for Key Vault network ACLs: Allow or Deny. Deny is the secure default. These ACLs govern the vault's PUBLIC endpoint only, so they have no effect while key_vault_public_network_access_enabled is false (the default), and private-endpoint traffic from the Container Apps environment is never subject to them."
  type        = string
  default     = "Deny"

  validation {
    condition     = contains(["Allow", "Deny"], var.key_vault_network_default_action)
    error_message = "key_vault_network_default_action must be either Allow or Deny."
  }
}

variable "key_vault_allowed_ip_ranges" {
  description = "Public IP addresses or CIDR ranges permitted to reach the Key Vault's public endpoint when key_vault_network_default_action is Deny. Only effective when key_vault_public_network_access_enabled is true — with public access disabled, IP rules are ignored and cannot be used to let a workstation write secrets. Ignored when the default action is Allow."
  type        = list(string)
  default     = []
}

variable "lucent_secret_key" {
  description = "Value for LUCENT_SECRET_KEY, used to encrypt stored secrets. Leave null to have Terraform generate one. Changing it invalidates previously stored secrets."
  type        = string
  default     = null
  sensitive   = true
}

variable "lucent_signing_secret" {
  description = "Value for LUCENT_SIGNING_SECRET, the HMAC key for impersonation cookies. The design doc calls out that app-side generation is a non-atomic get-or-create that can diverge across replicas, so it is provisioned here and injected. Leave null to generate."
  type        = string
  default     = null
  sensitive   = true
}

variable "lucent_license_key" {
  description = "Lucent license key. Required when lucent_mode is \"team\"; leave null in personal mode."
  type        = string
  default     = null
  sensitive   = true
}

variable "lucent_mode" {
  description = "Value for LUCENT_MODE: \"personal\" or \"team\"."
  type        = string
  default     = "personal"

  validation {
    condition     = contains(["personal", "team"], var.lucent_mode)
    error_message = "lucent_mode must be either personal or team."
  }
}

variable "github_token" {
  description = "GitHub personal access token with the copilot scope. Required by the default Copilot SDK engine. Leave null to omit the secret entirely."
  type        = string
  default     = null
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "Anthropic API key, only used when the LangChain engine is selected. Leave null to omit."
  type        = string
  default     = null
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key, only used when the LangChain engine is selected. Leave null to omit."
  type        = string
  default     = null
  sensitive   = true
}

variable "google_api_key" {
  description = "Google API key, only used when the LangChain engine is selected. Leave null to omit."
  type        = string
  default     = null
  sensitive   = true
}

variable "extra_secrets" {
  description = "Additional application configuration delivered as Key Vault-backed Container Apps secrets, keyed by the environment variable name Lucent should see. Values are written to Key Vault and therefore land in Terraform state."
  type        = map(string)
  default     = {}
  sensitive   = true
}

###############################################################################
# Application configuration
###############################################################################

variable "extra_environment" {
  description = "Additional plain (non-secret) environment variables injected into the web app, the controller and the workers job. Anything sensitive belongs in extra_secrets instead."
  type        = map(string)
  default     = {}
}

variable "lucent_llm_engine" {
  description = "Value for LUCENT_LLM_ENGINE. \"copilot\" is Lucent's default engine; choose \"langchain\" to use provider API keys instead."
  type        = string
  default     = "copilot"
}

variable "lucent_public_url" {
  description = "Value for LUCENT_PUBLIC_URL, the canonical externally reachable URL. Leave null to use the Container Apps default domain, which is only known after apply and is used automatically."
  type        = string
  default     = null
}

variable "lucent_log_level" {
  description = "Value for LUCENT_LOG_LEVEL."
  type        = string
  default     = "INFO"
}

###############################################################################
# File storage (Azure Blob Storage)
###############################################################################

variable "storage_container_name" {
  description = "Name of the private blob container that holds user files. Azure's container rules: 3-63 characters, lowercase letters, digits and hyphens, starting with a letter or digit, with no consecutive hyphens. Injected into the app as LUCENT_AZURE_STORAGE_CONTAINER."
  type        = string
  default     = "lucent-files"

  validation {
    condition     = can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.storage_container_name)) && length(var.storage_container_name) >= 3 && length(var.storage_container_name) <= 63
    error_message = "storage_container_name must be 3-63 characters of lowercase letters, digits and non-consecutive hyphens, starting with a letter or digit."
  }
}

variable "storage_account_replication_type" {
  description = "Replication for the storage account. LRS is the honest default for a beta: three copies inside one region at the lowest price. ZRS survives a zone failure at roughly 1.25x, GRS/RAGRS replicate to a paired region at roughly 2x, and changing between the non-zonal and zonal families is an Azure-initiated migration that can take days (the provider does not wait for it)."
  type        = string
  default     = "LRS"

  validation {
    condition     = contains(["LRS", "ZRS", "GRS", "GZRS", "RAGRS", "RAGZRS"], var.storage_account_replication_type)
    error_message = "storage_account_replication_type must be one of LRS, ZRS, GRS, GZRS, RAGRS or RAGZRS."
  }
}

variable "storage_network_default_action" {
  description = "Default action for the storage account's public-endpoint network rules: Deny or Allow. Deny is the secure default: the public endpoint answers nothing that is not explicitly allowed, and the workloads reach the account over its private endpoint instead. The rules are set INLINE on azurerm_storage_account, so Deny is in force as part of the account's creation rather than in a later API call that a failed apply could leave undone. It is a variable rather than a fixed Deny so there is an escape hatch in the one case that needs it — DATA-PLANE access from outside the VNet. Note precisely what is and is not affected: the firewall applies to data-plane operations only, and azurerm_storage_container is managed over the ARM control plane in 5.10.0, so Terraform's own apply and refresh are not governed by this setting. It governs Storage Explorer, az storage with --auth-mode login, AzCopy, backups, any data-plane resource added later — and the container too if a provider update moves that resource back to the data plane. See the README, 'Reaching the blob data plane'."
  type        = string
  default     = "Deny"

  validation {
    condition     = contains(["Allow", "Deny"], var.storage_network_default_action)
    error_message = "storage_network_default_action must be either Allow or Deny."
  }
}

variable "storage_allowed_ip_ranges" {
  description = "Public IPv4 addresses or CIDR ranges permitted to reach the storage account's public endpoint when storage_network_default_action is Deny. Add the host that runs data-plane tooling here when it cannot reach the account privately. Two Azure rules limit this: only public internet ranges are accepted (no RFC 1918 addresses, and /31 or /32 prefixes are rejected — give single addresses bare), and IP rules have NO EFFECT on requests that originate in the same Azure region as the storage account, which is why a same-region host works without being listed."
  type        = list(string)
  default     = []
}

variable "storage_soft_delete_retention_days" {
  description = "Retention for blob and container soft delete, in days (1-365). Soft delete is the cheap protection against an accidental or malicious delete or overwrite: no per-operation cost, no effect on reads or writes, and it expires on its own. It also has an erasure consequence that must be planned for: deleting a tenant's files or container does NOT remove the bytes. They remain recoverable and are billed at the active-data rate for this many days, and a soft-deleted base blob cannot be purged early without changing this account-wide setting. See the README, 'File storage retention: soft delete, versioning and immutability'. A shorter window (1 day) narrows that exposure for erasure-sensitive deployments. It protects data INSIDE the account — it is not protection against deleting the account itself, because soft-deleted blobs belong to the account and go with it."
  type        = number
  default     = 7

  validation {
    condition     = var.storage_soft_delete_retention_days >= 1 && var.storage_soft_delete_retention_days <= 365
    error_message = "storage_soft_delete_retention_days must be between 1 and 365."
  }
}

variable "storage_permanent_delete_enabled" {
  description = "Set delete_retention_policy.permanent_delete_enabled on the blob service. Defaults to FALSE, which is the protective setting: while it is false, soft-deleted snapshots and versions cannot be removed before the retention window expires. Setting it true (account-wide) lets a caller with data permissions permanently delete soft-deleted SNAPSHOTS AND VERSIONS early — relevant to an erasure request, and to the soft-deleted snapshots that every overwrite creates while versioning is off. It does not by itself purge anything, and it does not let you permanently delete a soft-deleted BASE blob before the window expires: that requires turning soft delete off for the whole account (or waiting the window out). Enable it deliberately for erasure-sensitive deployments, understanding that it also weakens accidental-overwrite protection."
  type        = bool
  default     = false
}

variable "storage_blob_versioning_enabled" {
  description = "Enable blob versioning. Defaults to FALSE, deliberately — this is a recommendation, not an inherited default, so it is worth the paragraph. Versioning keeps every previous copy of every blob, and unlike soft delete those versions have NO expiry: they are retained until each one is explicitly deleted. That makes it an indefinite retention commitment on a store that holds tenants' data, and it works against the two things this deployment most needs to be able to do — delete one tenant's data on offboarding or on an erasure request, and tear the deployment down. Soft delete already covers the realistic accident (recover a delete or overwrite within storage_soft_delete_retention_days) at no retention cost. Turn this on if you additionally need to recover from a bad write older than the soft-delete window, and accept that you must then delete versions explicitly to remove data."
  type        = bool
  default     = false
}

variable "lucent_file_storage_provider" {
  description = "Value for LUCENT_FILE_STORAGE_PROVIDER: \"azure\" (the default) writes new files to the blob container created here, \"local\" keeps the filesystem provider in LUCENT_FILE_STORAGE_PATH. The image you deploy MUST be built from a revision that contains the provider selector (and src/lucent/storage/azure_blob.py). An older image ignores this variable entirely: it boots, passes /api/health, and fails at the first file operation — and if its local path is writable it silently writes to the replica's ephemeral disk instead, which is the data loss this module exists to prevent. Terraform cannot detect that mismatch. The app-side startup assertion that will make it fail fast is being handled separately; until it lands, verify the image revision before apply. On Container Apps, setting \"local\" writes to /var/lib/lucent/files, which is the replica's ephemeral writable layer — not shared between replicas and lost on restart/scale — so use it only to keep an older image running while you upgrade, never as durable storage. The blob endpoint, container name and AZURE_CLIENT_ID are injected either way; rows keep their recorded provider, but local bytes are not durable here."
  type        = string
  default     = "azure"

  validation {
    condition     = contains(["local", "azure"], var.lucent_file_storage_provider)
    error_message = "lucent_file_storage_provider must be either local or azure."
  }
}

###############################################################################
# Sandboxes (Azure Container Instances)
#
# The whole ACI path is opt-in. sandbox_backend defaults to "docker", and in
# that mode this module creates no sandbox resource at all, so an existing
# deployment's plan does not change.
###############################################################################

variable "sandbox_backend" {
  description = "Sandbox backend selected via LUCENT_SANDBOX_BACKEND. \"docker\" (the default) and \"kubernetes\" create none of this module's ACI infrastructure and inject no sandbox variables. \"aci\" creates the delegated subnet, the image-pull identity and its AcrPull grant, the app identity's container-group RBAC, the NSG/NAT egress path, the diagnostic-settings policy, and the LUCENT_SANDBOX_* variables in the web app AND in the controller/worker environment (the dispatcher that actually creates task sandboxes runs in a per-org worker)."
  type        = string
  default     = "docker"

  validation {
    condition     = contains(["docker", "kubernetes", "aci"], var.sandbox_backend)
    error_message = "sandbox_backend must be one of docker, kubernetes or aci."
  }
}

variable "sandbox_subnet_prefix" {
  description = "CIDR for the ACI-delegated subnet, used only when create_vnet is true. The module creates the subnet delegated to Microsoft.ContainerInstance/containerGroups, and once delegated the subnet can contain only container groups. Microsoft recommends at least a /24 because ACI allocates one address per container group and cannot release addresses quickly; smaller prefixes are rejected here because they produce \"subnet full\" failures and stuck groups. A /24 gives roughly 250 usable group addresses after Azure's five reserved addresses."
  type        = string
  default     = "10.42.4.0/24"

  validation {
    condition     = can(cidrhost(var.sandbox_subnet_prefix, 0)) && tonumber(split("/", var.sandbox_subnet_prefix)[1]) <= 24
    error_message = "sandbox_subnet_prefix must be a valid CIDR of /24 or larger (a prefix length of 24 or less). Microsoft documents smaller ACI subnets as causing \"subnet full\" deployment failures."
  }
}

variable "existing_sandbox_subnet_id" {
  description = "Resource ID of an existing subnet delegated to Microsoft.ContainerInstance/containerGroups, required when sandbox_backend = \"aci\" and create_vnet = false. The subnet must contain no other resource type, must be in the same region as the sandbox resource group, and its NSG association is taken over by this module (this module associates its own NSG). Because the module does not create this subnet, `default_outbound_access_enabled = false` (which this module sets on the subnet it creates) does NOT apply: verify or disable the subnet's implicit outbound access yourself, or the NAT-gateway/NSG egress model described in the README is not enforced."
  type        = string
  default     = null
}

variable "sandbox_extra_outbound_prefixes" {
  description = "Additional destination CIDRs admitted by the sandbox NSG's VNet outbound rule. Use this for a custom in-VNet DNS resolver (the module configures Azure-provided DNS only) or another in-VNet service the sandboxes must reach. When create_vnet = true the rule is otherwise scoped to private_endpoint_subnet_prefix; when false it already covers the whole adopted VNet, so entries here are usually unnecessary."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for prefix in var.sandbox_extra_outbound_prefixes : can(cidrhost(prefix, 0))
    ])
    error_message = "sandbox_extra_outbound_prefixes must be a list of valid IPv4/IPv6 CIDR prefixes."
  }
}

variable "sandbox_management_source_prefixes" {
  description = "Source CIDRs allowed to reach TCP 19390 on sandbox container groups. Microsoft documents 19390 for portal (logs/shell) access to VNet-deployed groups and, potentially, the container-exec data channel; it is not an application port and blocking it does not keep groups from starting. Leave null (the default) to allow the module-created Container Apps subnet (container_apps_subnet_prefix), which is where the application's exec call originates. When create_vnet = false the existing subnet's CIDR is unknown, so null creates NO rule: set this explicitly (for example an operator or ExpressRoute range) for portal/exec access, or [] to disable the rule deliberately."
  type        = list(string)
  default     = null

  validation {
    condition = var.sandbox_management_source_prefixes == null ? true : alltrue([
      for prefix in var.sandbox_management_source_prefixes : can(cidrhost(prefix, 0))
    ])
    error_message = "sandbox_management_source_prefixes must be null or a list of valid IPv4/IPv6 CIDR prefixes."
  }
}

variable "create_sandbox_resource_group" {
  description = "When true, the module creates the dedicated sandbox resource group. Set false to adopt an existing group named by sandbox_resource_group_name. The group exists only to scope the app identity's custom container-group role; it should contain only sandbox container groups."
  type        = bool
  default     = true
}

variable "sandbox_resource_group_name" {
  description = "Name of the dedicated sandbox resource group. Leave null to derive <name_prefix>-<environment>-sandbox-rg. When create_sandbox_resource_group is false this is the existing group that is adopted. It must be in the same region as the ACI subnet."
  type        = string
  default     = null
}

variable "sandbox_nat_gateway_enabled" {
  description = "Create a Standard NAT gateway with a static public IP and associate it with the sandbox subnet. Microsoft documents a NAT gateway as the only supported outbound configuration for a container group deployed into a virtual network, and the module also sets default_outbound_access_enabled = false on the subnet. Set false only when you are supplying egress some other way (an Azure Firewall with a UDR): with this false and no replacement, sandboxes have no Internet egress at all and the MCP bridge cannot reach the Lucent server."
  type        = bool
  default     = true
}

variable "sandbox_internet_egress_enabled" {
  description = "Add an NSG rule allowing outbound TCP 443 from the sandbox subnet to the Internet. This is what lets the MCP bridge call the Lucent server (LUCENT_SANDBOX_BRIDGE_API_URL) and lets sandboxes reach package registries. It is deliberately broad because an NSG cannot match on FQDN: the rule means any host on 443, not only the Lucent server. For an FQDN allow list, put an Azure Firewall in the egress path with a UDR and set this false (or narrow the rule to the firewall's address). With it false and no firewall, sandboxes can reach only the VNet and Azure DNS."
  type        = bool
  default     = true
}

variable "sandbox_bridge_image" {
  description = "Full image reference for the trusted MCP bridge sidecar, injected as LUCENT_SANDBOX_BRIDGE_IMAGE. Leave null to default to <registry login server>/lucent-sandbox:base. Nothing in this repository builds or pushes this image: build sandbox-images/Dockerfile.base (and the bridge variant) and push it to the registry before enabling sandbox_backend = \"aci\". ACI pulls it with the sandbox pull identity's AcrPull grant. ACI cannot pull from a non-ACR PRIVATE registry; a public registry works, but the pull identity's grant then has to be made on that registry by the operator."
  type        = string
  default     = null
}

variable "sandbox_diagnostics_enabled" {
  description = "Create the Azure Policy assignment that deploys a diagnostic setting on every container group created in the sandbox resource group, sending ContainerInstanceLog and ContainerEvent to the deployment's Log Analytics workspace. This is the only way to log container groups Terraform never creates; an azurerm_monitor_diagnostic_setting cannot be attached to a group that does not exist at apply time. ACI's diagnostic-settings integration is in public preview. The assignment's system-assigned identity gets Monitoring Contributor on the sandbox resource group and Log Analytics Contributor on the workspace (Terraform-created DeployIfNotExists assignments do not receive the portal's automatic role grant)."
  type        = bool
  default     = true
}

variable "sandbox_operator_role_actions" {
  description = "Management-plane actions granted to the web app's identity on the DEDICATED SANDBOX RESOURCE GROUP. The default is the precise set the ACI sandbox backend needs: read, write (create/update), delete, exec, and two read-only actions on the group's diagnostic setting so the diagnostic-settings policy can evaluate its existence condition as the creating caller. It deliberately excludes restart/start/stop, attach and container-log reads — destroy is a delete, and no other path is used. Note the nature of the grant before widening it: containerGroups/write is permission to run arbitrary images and commands, and containers/exec/action is arbitrary code execution in every group in scope. That is why the assignment is resource-group scoped and must never be made at subscription scope."
  type        = list(string)
  default = [
    "Microsoft.ContainerInstance/containerGroups/read",
    "Microsoft.ContainerInstance/containerGroups/write",
    "Microsoft.ContainerInstance/containerGroups/delete",
    "Microsoft.ContainerInstance/containerGroups/containers/exec/action",
    # Read-only, and not part of creating or running a sandbox. Azure Policy
    # evaluates a DeployIfNotExists assignment's existence condition as the
    # CALLER that created the resource; the diagnostic-settings policy in
    # monitoring.tf therefore needs the app identity to be able to read the
    # group's diagnostic setting. The assignment's own identity is what writes
    # it. Without these actions the group is still created, but the diagnostic
    # setting may not be deployed and the group shows as non-compliant. Both the
    # resource-provider-specific form and the generic Microsoft.Insights action
    # are listed because authorization for the child resource can be checked
    # against either.
    "Microsoft.ContainerInstance/containerGroups/providers/Microsoft.Insights/diagnosticSettings/read",
    "Microsoft.Insights/diagnosticSettings/read",
  ]
}

###############################################################################
# Database provisioning / bootstrap
###############################################################################

variable "enable_provisioning_job" {
  description = "Create the Container Apps job that finishes database provisioning (daemon role, pg_trgm threshold, default privileges). Terraform cannot create a role or run ALTER DATABASE, and the database has no public endpoint so this cannot be done from an operator workstation — the job is the mechanism. It is created by Terraform but started by the operator (see README)."
  type        = bool
  default     = true
}

variable "trigger_provisioning_job_after_apply" {
  description = "Also shell out to the Azure CLI to start the provisioning job during apply, via a null_resource. Requires az on PATH and an authenticated session. Off by default so that apply stays hermetic."
  type        = bool
  default     = false
}

variable "enable_app_bootstrap" {
  description = "Create the job that runs `python -m lucent.migrate` and then `python -m lucent.bootstrap` against the application image, so a fresh instance is provisioned with no browser. Off by default because it creates a real owner account and needs bootstrap_admin_email and bootstrap_admin_password — and because the password is then held in Terraform state. Note the ordering dependency: this job needs the application roles, so the database provisioning job must have run first."
  type        = bool
  default     = false
}

variable "bootstrap_admin_email" {
  description = "Email address of the first owner account. Required when enable_app_bootstrap is true. Becomes LUCENT_BOOTSTRAP_EMAIL."
  type        = string
  default     = null
}

variable "bootstrap_admin_password" {
  description = "Password for the first owner account. Required when enable_app_bootstrap is true, and passed to the job as a Key Vault-backed secret. It is also stored in Terraform state — prefer to inject it from a secret manager and rotate it after first sign-in."
  type        = string
  default     = null
  sensitive   = true
}

variable "bootstrap_admin_display_name" {
  description = "Display name for the first owner account. Becomes LUCENT_BOOTSTRAP_DISPLAY_NAME."
  type        = string
  default     = "Administrator"
}

variable "bootstrap_org_name" {
  description = "Name of the organisation the bootstrap creates. Leave null to let Lucent choose its default. Becomes LUCENT_BOOTSTRAP_ORG_NAME."
  type        = string
  default     = null
}

variable "bootstrap_model" {
  description = "Model id the bootstrap should adopt, enable and grant to the organisation. Leave null to let Lucent pick from the models available in the registry. Becomes LUCENT_BOOTSTRAP_MODEL. Do not assume an enabled seed model is usable: usage is clearance-driven and default-deny, which is the trap the bootstrap module exists to handle."
  type        = string
  default     = null
}

###############################################################################
# Controller RBAC
###############################################################################

variable "controller_job_role_actions" {
  description = "Management-plane actions granted to the controller's managed identity on the workers job. The default is the documented minimum for starting a job and reading its executions, and deliberately avoids the Microsoft.App/jobs/*/action wildcard that the built-in Container Apps Jobs Operator role uses — that wildcard also matches listSecrets. Note that start permission alone already implies the ability to use the job's secrets, which is why this role is scoped to the single job resource."
  type        = list(string)
  default = [
    "Microsoft.App/jobs/read",
    "Microsoft.App/jobs/start/action",
    "Microsoft.App/jobs/executions/read",
    "Microsoft.App/jobs/execution/read",
  ]
}
