###############################################################################
# Outputs
#
# Grouped by who needs them: the operator, the bootstrap steps, and the
# application's own configuration (which the operator must mirror anywhere else
# Lucent runs, such as a local daemon pointed at the same deployment).
###############################################################################

output "resource_group_name" {
  description = "Resource group holding the deployment."
  value       = local.resource_group.name
}

output "location" {
  description = "Azure region the deployment lives in."
  value       = local.resource_group.location
}

output "application_url" {
  description = "Public HTTPS URL of the web/API app. Azure's managed certificate terminates TLS here; there is no separate ingress resource to configure."
  value       = format("https://%s", azurerm_container_app.web.ingress[0].fqdn)
}

output "application_fqdn" {
  description = "Hostname of the web/API app, without scheme."
  value       = azurerm_container_app.web.ingress[0].fqdn
}

output "mcp_url" {
  description = "MCP endpoint the controller and workers connect to."
  value       = local.mcp_url
}

output "container_registry_login_server" {
  description = "Login server for the registry the Container Apps pull from. Push images here before the workloads can start."
  value       = local.container_registry_login_server
}

output "container_registry_name" {
  description = "Name of the container registry, or null when an existing registry was supplied."
  value       = var.create_container_registry ? azurerm_container_registry.this[0].name : null
}

output "container_app_environment_name" {
  description = "Name of the Container Apps environment."
  value       = azurerm_container_app_environment.this.name
}

output "container_app_environment_default_domain" {
  description = "Default domain Azure assigned to the Container Apps environment."
  value       = azurerm_container_app_environment.this.default_domain
}

output "web_app_name" {
  description = "Name of the web/API container app."
  value       = azurerm_container_app.web.name
}

output "controller_app_name" {
  description = "Name of the daemon controller container app. Ingress is disabled and replicas are pinned to 1."
  value       = azurerm_container_app.controller.name
}

output "workers_job_name" {
  description = "Name of the workers Container Apps job the controller starts."
  value       = azurerm_container_app_job.workers.name
}

output "provisioning_job_name" {
  description = "Name of the one-shot database provisioning job, or null when it is disabled."
  value       = var.enable_provisioning_job ? azurerm_container_app_job.provisioning[0].name : null
}

###############################################################################
# File storage
###############################################################################

output "storage_account_name" {
  description = "Name of the storage account holding user files. Globally unique and derived from name_prefix, environment and the random suffix."
  value       = azurerm_storage_account.files.name
}

output "storage_blob_endpoint" {
  description = "Blob service endpoint URL of the storage account, for example https://lucentbetaab12.blob.core.windows.net. Injected into the web app as LUCENT_AZURE_STORAGE_ACCOUNT_URL; inside the VNet it resolves to the private endpoint through the linked privatelink.blob.core.windows.net zone."
  value       = azurerm_storage_account.files.primary_blob_endpoint
}

output "storage_container_name" {
  description = "Name of the private blob container holding user files. Injected into the web app as LUCENT_AZURE_STORAGE_CONTAINER."
  value       = azurerm_storage_container.files.name
}

output "storage_container_id" {
  description = "Resource ID of the file container — the scope the app identity's Storage Blob Data Contributor grant is made on, and therefore the narrowest place to audit it."
  value       = azurerm_storage_container.files.id
}

output "storage_file_container_url" {
  description = "Full URL of the file container, for a person with data-plane access to browse. Note that this is the container's blob endpoint URL, not the account's; use az storage blob list --auth-mode login against it rather than a key."
  value       = azurerm_storage_container.files.url
}

output "postgres_server_name" {
  description = "Name of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.this.name
}

output "postgres_fqdn" {
  description = "Private FQDN of the PostgreSQL server. It resolves only inside the VNet, through the linked private DNS zone; there is no public endpoint."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}

output "postgres_database_name" {
  description = "Name of the application database."
  value       = var.postgres_database_name
}

output "key_vault_name" {
  description = "Name of the Key Vault holding the application secrets."
  value       = azurerm_key_vault.this.name
}

output "key_vault_uri" {
  description = "Vault URI, for example https://myvault.vault.azure.net/."
  value       = azurerm_key_vault.this.vault_uri
}

output "key_vault_application_secret_names" {
  description = "Key Vault secret names holding application configuration, keyed by the environment variable name Lucent sees. Values are deliberately not exported."
  value       = local.key_vault_secret_name
}

output "log_analytics_workspace_id" {
  description = "Log Analytics workspace the Container Apps environment logs to. Worker job output appears here — the executions API does not return it."
  value       = local.log_analytics_workspace_id
}

output "app_identity_principal_id" {
  description = "Principal ID of the identity used by the web app and the workers job. It can read the application secrets but not the raw database or bootstrap passwords."
  value       = azurerm_user_assigned_identity.app.principal_id
}

output "controller_identity_principal_id" {
  description = "Principal ID of the identity used by the controller, which additionally holds the custom job-start role."
  value       = azurerm_user_assigned_identity.controller.principal_id
}

output "provisioner_identity_principal_id" {
  description = "Principal ID of the identity used by the one-shot provisioning and bootstrap jobs — the only identity that can read the raw PostgreSQL passwords and the bootstrap owner password."
  value       = azurerm_user_assigned_identity.provisioner.principal_id
}

output "controller_job_role_definition_id" {
  description = "Resource ID of the custom role that can start the workers job and read its executions. The role is assignable at the resource-group scope; the assignment that grants it is scoped to the workers job alone."
  value       = azurerm_role_definition.controller_job_operator.role_definition_resource_id
}

output "vnet_id" {
  description = "Virtual network holding the deployment's subnets."
  value       = local.vnet_id
}

###############################################################################
# Sandboxes (ACI)
#
# Null in every output when sandbox_backend is not "aci".
###############################################################################

output "sandbox_backend" {
  description = "Sandbox backend the web app is configured for. \"docker\" and \"kubernetes\" create no ACI infrastructure."
  value       = var.sandbox_backend
}

output "sandbox_resource_group_name" {
  description = "Dedicated resource group the application creates ACI container groups in, or null when sandbox_backend is not \"aci\". The app identity's container-group role is scoped here."
  value       = local.sandbox_enabled ? local.sandbox_resource_group_name : null
}

output "sandbox_resource_group_id" {
  description = "Resource ID of the dedicated sandbox resource group — the scope of the custom container-group role. Null when sandbox_backend is not \"aci\"."
  value       = local.sandbox_resource_group_id
}

output "sandbox_subnet_id" {
  description = "Resource ID of the ACI-delegated subnet. Injected into the web app as LUCENT_SANDBOX_ACI_SUBNET_ID. Null when sandbox_backend is not \"aci\"."
  value       = local.sandbox_subnet_id
}

output "sandbox_subnet_prefix" {
  description = "CIDR of the module-created ACI subnet, or null when an existing subnet is used or the sandbox backend is not \"aci\"."
  value       = local.sandbox_enabled && var.create_vnet ? var.sandbox_subnet_prefix : null
}

output "sandbox_pull_identity_id" {
  description = "Resource ID of the user-assigned identity attached to sandbox container groups for ACR pulls. Injected into the web app as LUCENT_SANDBOX_ACI_IDENTITY_ID. It holds AcrPull on the registry and no other role."
  value       = local.sandbox_pull_identity_id
}

output "sandbox_pull_identity_client_id" {
  description = "Client ID of the sandbox image-pull identity. ACI needs the resource ID; this is useful when auditing the principal."
  value       = local.sandbox_pull_identity_client_id
}

output "sandbox_pull_identity_principal_id" {
  description = "Principal ID of the sandbox image-pull identity — the principal holding AcrPull and nothing else. Null when sandbox_backend is not \"aci\"."
  value       = local.sandbox_enabled ? azurerm_user_assigned_identity.sandbox_pull[0].principal_id : null
}

output "sandbox_bridge_image" {
  description = "Full image reference the application sets as LUCENT_SANDBOX_BRIDGE_IMAGE. This image must exist in the registry (or in sandbox_bridge_image, if overridden); nothing in the repository builds or pushes it."
  value       = local.sandbox_enabled ? local.sandbox_bridge_image : null
}

output "sandbox_nat_gateway_public_ip" {
  description = "Static public egress IP of the sandbox NAT gateway, for a downstream allow list. Egress only; sandbox container groups have no public IP. Null when the NAT gateway is disabled or sandbox_backend is not \"aci\"."
  value       = local.sandbox_enabled && var.sandbox_nat_gateway_enabled ? azurerm_public_ip.sandbox_nat[0].ip_address : null
}

output "sandbox_nat_gateway_id" {
  description = "Resource ID of the sandbox NAT gateway, or null when it is disabled or sandbox_backend is not \"aci\"."
  value       = local.sandbox_enabled && var.sandbox_nat_gateway_enabled ? azurerm_nat_gateway.sandbox[0].id : null
}

output "sandbox_network_security_group_id" {
  description = "Resource ID of the subnet-wide sandbox NSG. Note that it is subnet-wide: ACI has no per-container-group network policy."
  value       = local.sandbox_enabled ? azurerm_network_security_group.sandbox[0].id : null
}

output "sandbox_diagnostics_policy_assignment_id" {
  description = "Resource ID of the DeployIfNotExists policy assignment that sends container-group logs to Log Analytics, or null when diagnostics are disabled or sandbox_backend is not \"aci\"."
  value       = local.sandbox_enabled && var.sandbox_diagnostics_enabled ? azurerm_resource_group_policy_assignment.sandbox_diagnostics[0].id : null
}

output "next_steps" {
  description = "Ordered list of the things an operator still has to do by hand after apply. Repeats the README so it is visible from `terraform output`."
  value = compact([
    "1. Push the application image to ${local.container_registry_login_server} (the apps cannot start until it exists).",
    var.enable_provisioning_job ? "2. Start the database provisioning job: az containerapp job start -g ${local.resource_group.name} -n ${local.provisioning_job_name} --subscription ${local.subscription_id}" : "2. Database provisioning job is disabled; create the ${var.postgres_app_role} and ${var.postgres_daemon_role} roles and run CREATE EXTENSION yourself inside the VNet.",
    var.enable_app_bootstrap ? "3. Start the application bootstrap job (after the provisioning job has finished): az containerapp job start -g ${local.resource_group.name} -n ${local.base}-bootstrap --subscription ${local.subscription_id}" : "3. First-run setup is a browser step: open ${local.public_url}/setup. Set enable_app_bootstrap to run python -m lucent.bootstrap in a job instead.",
    "4. The image must contain daemon/launcher/azure_launcher.py for LUCENT_WORKER_LAUNCHER=${var.worker_launcher} to work. Build from a revision that has it, or set worker_launcher to a backend your image ships.",
    "5. The image must be built from a revision that contains the file-storage provider selector and src/lucent/storage/azure_blob.py. An older image ignores LUCENT_FILE_STORAGE_PROVIDER=${var.lucent_file_storage_provider}: it boots, passes /api/health, then fails at the first file operation — and if its local path is writable it silently writes to ephemeral replica disk. Terraform cannot detect this; check the revision the image was built from. On Container Apps, lucent_file_storage_provider = \"local\" is ephemeral and per-replica, not durable storage.",
    "6. Reach the blob data plane from inside the VNet, from the account's own region (Azure ignores IP network rules for same-region traffic), or from an address listed in storage_allowed_ip_ranges: the storage account's firewall denies the public endpoint, and that is what storage_allowed_ip_ranges is for. Note that Terraform's own apply and refresh are not governed by it — azurerm_storage_container is a control-plane resource in this provider version — so this matters for data-plane tooling (Storage Explorer, az storage --auth-mode login, AzCopy) and for any data-plane storage resource added later.",
    var.sandbox_backend == "aci" ? "7. Build and push the sandbox bridge image to ${local.sandbox_bridge_image} (build files are under sandbox-images/; nothing in the repository builds or pushes it). ACI pulls it with the sandbox pull identity's AcrPull grant, and it must exist before the first sandbox is created. Override sandbox_bridge_image if you push somewhere else." : "",
    var.sandbox_backend == "aci" && var.sandbox_diagnostics_enabled ? "8. Sandbox container groups are created at runtime in ${local.sandbox_resource_group_name}. The diagnostic-settings policy applies when a group is created; to attach diagnostics to groups that already exist, run: az policy remediation create --name ${local.sandbox_policy_assignment_name}-remediation --policy-assignment ${azurerm_resource_group_policy_assignment.sandbox_diagnostics[0].id}" : "",
    var.sandbox_backend == "aci" ? "9. The application's own sandbox feature switch (LUCENT_SANDBOXES_ENABLED) is separate from LUCENT_SANDBOX_BACKEND=aci. This module wires the Azure infrastructure and injects the ACI variables; it does not enable the feature in the application. The deployed image must also contain src/lucent/sandbox/aci_backend.py and the azure-mgmt-containerinstance / azure-identity packages, and the ACI backend must attach LUCENT_SANDBOX_ACI_IDENTITY_ID to each container group for the ACR pull to succeed." : "",
  ])
}
