###############################################################################
# RBAC
#
# The grants, each the narrowest thing that works:
#
#   1. AcrPull on the registry — so the image is pulled with the identity rather
#      than a registry admin password.
#   2. Key Vault Secrets User PER SECRET — read-only access to the VALUE of one
#      secret, granted at that secret's own scope rather than at the vault. This
#      is the role Container Apps requires for a Key Vault secret reference, and
#      it cannot write or delete anything.
#   3. A custom role on the workers job resource only — start the job and read
#      its executions.
#   4. Storage Blob Data Contributor on the file container — read and write user
#      files, bounded to ONE container rather than the whole storage account.
#
# (2) is per-secret on purpose. Granting Key Vault Secrets User at the vault
# scope lets an identity read EVERY secret in the vault; granting it on each
# secret's own resource ID means an identity can read only the secrets it is
# actually handed. That is what keeps the raw PostgreSQL passwords and the
# bootstrap owner password out of reach of the web app (the process class that
# faces the internet) and of the controller: neither shares an identity with the
# jobs that read them. The provisioner identity — see identity.tf — is the only
# principal granted the raw password secrets, and only the one-shot provisioning
# and bootstrap jobs run as it.
#
# Microsoft's general guidance prefers one vault per application over per-object
# assignments, because object-scope grants do not isolate teams (vault-level
# administrative access still exposes object metadata) and do not scale past a
# subscription's role-assignment limit. Here we are separating workloads inside a
# single deployment, at a scale of a handful of secrets, so per-secret scope is
# the right fit and the object count stays trivial.
#
# (3) is worth explaining. The design doc asks for a narrow custom role rather
# than a built-in, and Microsoft's own documentation agrees: the built-in
# "Container Apps Jobs Operator" and "Container Apps Jobs Contributor" roles both
# grant `Microsoft.App/jobs/*/action`, a wildcard that also matches
# `Microsoft.App/jobs/listSecrets/action`. A custom role that lists the four
# actions below avoids that.
#
# It is important not to overclaim what this buys. Microsoft's docs are explicit
# that `Microsoft.App/jobs/start/action` alone is enough to reach a job's secrets
# and managed identities, because the start call accepts an execution template
# that can override images, commands and environment variables. So start
# permission is, functionally, permission to use the job's secrets. What this
# role does buy is that the permission is scoped to ONE job resource and carries
# no unrelated Container Apps access; the protection against the remaining risk
# is that only the controller — a no-ingress background process — holds it.
###############################################################################

resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = local.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "controller_acr_pull" {
  scope                = local.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "provisioner_acr_pull" {
  scope                = local.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.provisioner.principal_id
  principal_type       = "ServicePrincipal"
}

###############################################################################
# Key Vault read access, one grant per secret
#
# `scope` is the secret's ARM resource ID (`resource_versionless_id`) rather than
# the vault's, so each principal reads only the secrets below.
#
#   * app         — the application-configuration secrets the web app, the
#                   workers job and (through the shared identity) the bootstrap
#                   job are injected with. NOT the raw passwords.
#   * controller  — the same application-configuration secrets.
#   * provisioner — the application-configuration secrets the bootstrap job needs
#                   PLUS the raw PostgreSQL passwords and the bootstrap owner
#                   password, which no other identity can read.
#
# One honest residual: the web app and the workers job share the `app` identity
# (that split is deliberate — neither holds any Microsoft.App/jobs permission).
# Because the identity is shared, the app identity can technically read
# DAEMON_DATABASE_URL even though the web container is never injected with it.
# Separating that would mean giving the workers job its own identity; it is left
# as a known, documented consequence rather than silently claimed as isolation.
###############################################################################

locals {
  # Everything the provisioner jobs reference: the application secrets (the
  # bootstrap job runs the application image and uses them) plus the raw
  # credentials only those jobs need. The bootstrap password is created only when
  # enable_app_bootstrap is set; the ternary short-circuits so the index is never
  # evaluated against a count of zero.
  provisioner_secret_scopes = merge(
    { for k, s in azurerm_key_vault_secret.application : k => s.resource_versionless_id },
    {
      "POSTGRES-ADMIN-PASSWORD"  = azurerm_key_vault_secret.postgres_admin_password.resource_versionless_id
      "POSTGRES-APP-PASSWORD"    = azurerm_key_vault_secret.postgres_app_password.resource_versionless_id
      "POSTGRES-DAEMON-PASSWORD" = azurerm_key_vault_secret.postgres_daemon_password.resource_versionless_id
    },
    var.enable_app_bootstrap ? {
      "LUCENT-BOOTSTRAP-ADMIN-PASSWORD" = azurerm_key_vault_secret.bootstrap_admin_password[0].resource_versionless_id
    } : {},
  )
}

resource "azurerm_role_assignment" "app_key_vault_secrets_user" {
  for_each = azurerm_key_vault_secret.application

  scope                = each.value.resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "controller_key_vault_secrets_user" {
  for_each = azurerm_key_vault_secret.application

  scope                = each.value.resource_versionless_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "provisioner_key_vault_secrets_user" {
  for_each = local.provisioner_secret_scopes

  scope                = each.value
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.provisioner.principal_id
  principal_type       = "ServicePrincipal"
}

###############################################################################
# Custom role: start the workers job, read its executions
#
# The role DEFINITION is assignable at the resource-group scope, per Microsoft's
# guidance: AssignableScopes should be a management group, subscription or
# resource group, and a resource-instance scope is "not recommended" (it does not
# narrow anything — the ASSIGNMENT is what governs access — and it burns through
# the tenant's custom-role quota). What actually keeps the permission narrow is
# the assignment below, which is scoped to the single workers job.
#
# `assignable_scopes` is left unset; the provider adds `scope` to it automatically.
###############################################################################

resource "azurerm_role_definition" "controller_job_operator" {
  name        = "${local.base}-workers-job-operator"
  scope       = local.resource_group.id
  description = "Start the Lucent workers Container Apps job and read its executions. Deliberately narrower than Container Apps Jobs Operator, whose Microsoft.App/jobs/*/action wildcard also grants listSecrets."

  permissions {
    actions = var.controller_job_role_actions
  }
}

resource "azurerm_role_assignment" "controller_job_operator" {
  scope              = azurerm_container_app_job.workers.id
  role_definition_id = azurerm_role_definition.controller_job_operator.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.controller.principal_id
  principal_type     = "ServicePrincipal"
}

###############################################################################
# Storage Blob Data Contributor, on the container
#
# Two things about this grant are easy to get wrong.
#
# FIRST, it is a DATA-PLANE role, not a management role. The blob data plane and
# the ARM control plane have entirely separate authorization: "Storage Account
# Contributor" (and Owner, and Contributor) can manage the account — change its
# SKU, read its keys — and still read or write ZERO blobs. Only the
# `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/*` data
# actions, which the Storage Blob Data * roles carry, authorize blob reads and
# writes. So the app needs this role specifically; a management role would look
# sufficient and would not work.
#
# SECOND, `scope` is the CONTAINER's resource ID, not the account's. This is the
# narrower grant and the narrower grant is the point: an account-scoped
# assignment of the same role would let this identity read and write every
# container in the account, including any added later by someone else — the
# account is a namespace, so an account-scoped role is not the unit a file store
# wants to hand out. Container scope bounds the identity to the one container
# the application uses, which is exactly the surface it needs, and a second
# container created later is not silently granted.
#
# The principal is the `app` identity — the one the web app and the workers job
# run as (identity.tf). It is NOT granted to the controller identity: the
# controller supervises jobs and processes no user files, and its environment is
# deliberately not given the blob configuration to match (locals.tf). Note the
# honest residual, the same one the Key Vault grants have: because the web app
# and the workers job share this identity, both hold this blob access.
#
# One consequence of container scope worth stating: because the grant IS the
# container, deleting and recreating the container (a name change, say) moves
# the grant with it, whereas an account-scoped grant would have survived.
###############################################################################

resource "azurerm_role_assignment" "app_storage_blob_data_contributor" {
  scope                = azurerm_storage_container.files.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

###############################################################################
# Sandboxes on Azure Container Instances
#
# The application identity (`app`, shared by the web app and the workers job)
# creates, inspects, execs into and deletes container groups at runtime. The
# actual creator is the dispatcher in a per-org worker; the web app holds the
# same identity for its sandbox API surface. That is a powerful capability and
# it is granted as THREE narrow custom roles, each assigned at the smallest
# scope where its action is actionable:
#
#   1. sandbox-operator — container-group read/write/delete and containers/exec,
#      assigned at the DEDICATED SANDBOX RESOURCE GROUP. Why it must be
#      resource-group scoped and not subscription scoped:
#        * `containerGroups/write` lets the holder create a group from any
#          image with any command and any environment, i.e. run arbitrary code
#          on Azure's infrastructure and use whatever identity is attached.
#        * `containers/exec/action` is arbitrary code execution inside EVERY
#          group the assignment covers, in addition to read/write.
#      Both are intentional — they are the feature. The mitigation is scope:
#      the assignment covers one resource group that contains only sandbox
#      container groups, so it cannot reach the database, vault, storage
#      account, registry or Container Apps. A subscription-scoped grant would
#      let this identity do that to every container group in the subscription
#      and is explicitly not what this module does.
#
#   2. sandbox-subnet-join — `Microsoft.Network/virtualNetworks/subnets/join/action`,
#      assigned at the SANDBOX SUBNET only. ARM checks the caller's permission
#      on the subnet when a container group names it, so the app needs this to
#      deploy into the delegated subnet. It is one action, not Network
#      Contributor: the app cannot read, write, delete or re-delegate the
#      subnet, and cannot touch the NSG, route table or VNet.
#
#   3. sandbox-identity-assign — `Microsoft.ManagedIdentity/userAssignedIdentities/assign/action`,
#      assigned at the PULL IDENTITY only. Assigning a user-assigned identity to
#      a resource is itself an authorization check on that identity; without
#      this the create call fails. It is deliberately not the built-in "Managed
#      Identity Operator" role, which carries more identity permissions than
#      one assign action.
#
# The pull identity itself gets AcrPull on the registry and NOTHING else. It is
# attached to every sandbox group so ACI can pull the bridge image; it holds no
# application, Key Vault, storage or network role.
#
# Exact action names, verified against Microsoft's "Azure permissions for
# Containers" reference (the provider schema cannot validate role action
# strings — they are opaque to Terraform):
#   Microsoft.ContainerInstance/containerGroups/read
#   Microsoft.ContainerInstance/containerGroups/write
#   Microsoft.ContainerInstance/containerGroups/delete
#   Microsoft.ContainerInstance/containerGroups/containers/exec/action
#   Microsoft.ContainerInstance/containerGroups/providers/Microsoft.Insights/diagnosticSettings/read
#   Microsoft.Insights/diagnosticSettings/read
#   Microsoft.Network/virtualNetworks/subnets/join/action
#   Microsoft.ManagedIdentity/userAssignedIdentities/assign/action
#
# The two diagnosticSettings/read actions are read-only and exist only because
# a DeployIfNotExists policy is evaluated as the caller that creates the
# resource: the diagnostic-settings policy in monitoring.tf reads the group's
# diagnostic setting as the app identity, while the assignment's own identity
# writes it. Both the resource-provider-specific and generic forms are listed
# because authorization for the child resource can be checked against either.
# Without them the group still runs, but diagnostics may not be deployed.
#
# Deliberately NOT granted: containerGroups/restart|start|stop/action,
# containers/attach/action, containers/logs/read and containers/buildlogs/read.
# destroy is a delete, stop is implemented by the backend as a delete, and no
# other path needs them. A backend that calls stop/action or reads container
# logs will fail until the operator adds the action to
# sandbox_operator_role_actions.
#
# One honest residual: both holders of the app identity — the internet-facing
# web process and every worker — carry these grants. The application must treat
# the sandbox resource group as a security boundary of its own — a compromise
# of either process yields arbitrary code execution in the sandbox group and
# the ability to pull images from the registry, but not the database
# credentials, the vault, the file store or Container Apps administration.
###############################################################################

resource "azurerm_role_definition" "sandbox_operator" {
  count = local.sandbox_enabled ? 1 : 0

  name        = "${local.base}-sandbox-operator"
  scope       = local.sandbox_resource_group_id
  description = "Create, read, list, exec into and delete ACI container groups in the dedicated sandbox resource group. Intentionally powerful at this scope; it grants no access outside it."

  permissions {
    actions = var.sandbox_operator_role_actions
  }
}

resource "azurerm_role_assignment" "app_sandbox_operator" {
  count = local.sandbox_enabled ? 1 : 0

  scope              = local.sandbox_resource_group_id
  role_definition_id = azurerm_role_definition.sandbox_operator[0].role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.app.principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_definition" "sandbox_subnet_join" {
  count = local.sandbox_enabled ? 1 : 0

  name        = "${local.base}-sandbox-subnet-join"
  scope       = local.sandbox_subnet_role_scope
  description = "Join the ACI-delegated sandbox subnet when creating a container group. One action; no other network access."

  permissions {
    actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
  }
}

resource "azurerm_role_assignment" "app_sandbox_subnet_join" {
  count = local.sandbox_enabled ? 1 : 0

  scope              = local.sandbox_subnet_id
  role_definition_id = azurerm_role_definition.sandbox_subnet_join[0].role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.app.principal_id
  principal_type     = "ServicePrincipal"

  lifecycle {
    precondition {
      condition     = var.create_vnet || var.existing_sandbox_subnet_id != null
      error_message = "sandbox_backend = \"aci\" with create_vnet = false requires existing_sandbox_subnet_id. Supply the resource ID of a subnet that is already delegated to Microsoft.ContainerInstance/containerGroups and contains no other resource type."
    }

    precondition {
      condition     = var.create_vnet || var.existing_vnet_id != null
      error_message = "sandbox_backend = \"aci\" with create_vnet = false requires existing_vnet_id, which names the resource group the subnet-join role definition must be assignable in."
    }
  }
}

resource "azurerm_role_definition" "sandbox_identity_assign" {
  count = local.sandbox_enabled ? 1 : 0

  name        = "${local.base}-sandbox-identity-assign"
  scope       = local.resource_group.id
  description = "Attach the sandbox image-pull identity to a container group. Assign action only, on that one identity."

  permissions {
    actions = ["Microsoft.ManagedIdentity/userAssignedIdentities/assign/action"]
  }
}

resource "azurerm_role_assignment" "app_sandbox_identity_assign" {
  count = local.sandbox_enabled ? 1 : 0

  scope              = azurerm_user_assigned_identity.sandbox_pull[0].id
  role_definition_id = azurerm_role_definition.sandbox_identity_assign[0].role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.app.principal_id
  principal_type     = "ServicePrincipal"
}

# The sandbox groups' own identity: AcrPull on this deployment's registry, and
# nothing else. This is the identity the application attaches to each group so
# ACI can pull the bridge image from the private registry.
resource "azurerm_role_assignment" "sandbox_pull_acr_pull" {
  count = local.sandbox_enabled ? 1 : 0

  scope                = local.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.sandbox_pull[0].principal_id
  principal_type       = "ServicePrincipal"
}

###############################################################################
# Diagnostic-settings policy identity
#
# Terraform cannot create diagnostic settings for container groups that do not
# exist yet — the application creates them at runtime. monitoring.tf assigns the
# built-in policy "Configure diagnostic settings for container groups to Log
# Analytics workspace", whose DeployIfNotExists effect adds the diagnostic
# setting when a group is created.
#
# When a DINE assignment is created through the PORTAL, Azure Policy grants the
# assignment's identity the roles in the policy's roleDefinitionIds. When it is
# created through Terraform/ARM/CLI — as here — it does NOT: the roles have to be
# granted explicitly. The built-in policy lists two:
#   749f88d5-cbae-40b8-bcfc-e573ddc772fa  Monitoring Contributor
#   92aaf0da-9dab-42b6-94a3-d43ce8d16293  Log Analytics Contributor
# They are granted on the smallest scopes that make the deployment work: the
# sandbox resource group for the diagnostic setting on the group, and the
# workspace for writing into it. Both are scoped to this policy's identity, which
# exists only for this purpose.
###############################################################################

resource "azurerm_role_assignment" "sandbox_diagnostics_resource_group" {
  count = local.sandbox_enabled && var.sandbox_diagnostics_enabled ? 1 : 0

  scope                = local.sandbox_resource_group_id
  role_definition_name = "Monitoring Contributor"
  principal_id         = azurerm_resource_group_policy_assignment.sandbox_diagnostics[0].identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "sandbox_diagnostics_workspace" {
  count = local.sandbox_enabled && var.sandbox_diagnostics_enabled ? 1 : 0

  scope                = local.log_analytics_workspace_id
  role_definition_name = "Log Analytics Contributor"
  principal_id         = azurerm_resource_group_policy_assignment.sandbox_diagnostics[0].identity[0].principal_id
  principal_type       = "ServicePrincipal"
}
