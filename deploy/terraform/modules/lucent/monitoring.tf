###############################################################################
# Log Analytics
#
# The Container Apps environment sends container stdout/stderr here. This is not
# optional bookkeeping for Lucent: the Container Apps executions API does not
# return job output, so worker logs reach an operator only through this
# workspace. The first 5 GB per month are free, which a small beta will not
# exceed.
###############################################################################

resource "azurerm_log_analytics_workspace" "this" {
  count = var.create_log_analytics_workspace ? 1 : 0

  name                = local.log_analytics_workspace_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_analytics_retention_days
  tags                = local.tags
}

###############################################################################
# Sandbox container-group logs
#
# The application creates and destroys container groups at runtime, so there is
# no container-group resource in this configuration to attach an
# azurerm_monitor_diagnostic_setting to. The only infrastructure-level way to
# get every future group's output into the existing workspace is an Azure
# Policy assignment with a DeployIfNotExists effect.
#
# The built-in definition used here is
#   "Configure diagnostic settings for container groups to Log Analytics workspace"
#   /providers/Microsoft.Authorization/policyDefinitions/41ebf9df-66cb-48e9-a8d0-98afb4e150ce
# It deploys a diagnostic setting on each container group at creation, with both
# of the only two categories Microsoft.ContainerInstance/containerGroups
# supports:
#   ContainerInstanceLog  — the container's stdout/stderr
#   ContainerEvent        — container lifecycle events
# There is no separate per-category switch in the policy: logsEnabled toggles
# both.
#
# The assignment is scoped to the dedicated sandbox resource group, so it
# applies to groups the application creates there and to nothing else. Its
# system-assigned identity needs the roles listed in the policy definition
# granted explicitly — Terraform-created DINE assignments do not get the
# portal's automatic grant — which rbac.tf does.
#
# Two operational consequences, both Microsoft-documented:
#   * ACI's diagnostic-settings integration is in PUBLIC PREVIEW. The policy
#     itself is GA, but the underlying feature is preview; a region or
#     subscription that rejects the diagnostic settings resource will make the
#     policy non-compliant rather than break the container group, because
#     DeployIfNotExists records a failed deployment and the group still runs.
#   * Applying a diagnostic setting restarts the container group when it is
#     added after creation. At creation (the normal path here) that is moot.
#     Groups created before this policy existed are not modified until they are
#     recreated or a remediation task runs; the next_steps output prints the
#     remediation command.
###############################################################################

resource "azurerm_resource_group_policy_assignment" "sandbox_diagnostics" {
  count = local.sandbox_enabled && var.sandbox_diagnostics_enabled ? 1 : 0

  name                 = local.sandbox_policy_assignment_name
  display_name         = "Lucent sandbox container-group logs to Log Analytics"
  description          = "Deploys a diagnostic setting on every ACI container group in the sandbox resource group, sending ContainerInstanceLog and ContainerEvent to the deployment's Log Analytics workspace."
  resource_group_id    = local.sandbox_resource_group_id
  policy_definition_id = "/providers/Microsoft.Authorization/policyDefinitions/41ebf9df-66cb-48e9-a8d0-98afb4e150ce"
  location             = local.sandbox_location
  enforce              = true

  identity {
    type = "SystemAssigned"
  }

  parameters = jsonencode({
    effect = {
      value = "DeployIfNotExists"
    }
    profileName = {
      # The diagnostic setting's own name on each group.
      value = "containerInstanceDiagnosticsLogsToWorkspace"
    }
    logAnalytics = {
      value = local.log_analytics_workspace_id
    }
    logsEnabled = {
      value = true
    }
  })
}
