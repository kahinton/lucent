###############################################################################
# Managed identities
#
# Three identities, split along the privilege boundaries that actually matter.
#
#   * app         — the web/API app and the workers job. Pulls the image and
#                   reads the application secrets. Deliberately does NOT hold
#                   anything in the Microsoft.App/jobs plane: the web app that
#                   faces the internet must not be able to launch workloads, and
#                   it is NOT granted the raw database/administrator passwords.
#   * controller  — everything the app has, plus the ability to start the workers
#                   job and read its executions.
#   * provisioner — the one-shot provisioning and bootstrap jobs. It is the only
#                   identity that can read the raw PostgreSQL passwords and the
#                   bootstrap owner password, and it is deliberately separate from
#                   `app` so those secrets are not readable by the internet-facing
#                   web app or the controller. It holds no Container Apps job
#                   permissions and no long-running workload uses it.
#
# User-assigned rather than system-assigned so the identity survives a revision
# change and its role assignments can be made before the container exists.
###############################################################################

resource "azurerm_user_assigned_identity" "app" {
  name                = local.app_identity_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "controller" {
  name                = local.controller_identity_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "provisioner" {
  name                = local.provisioner_identity_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  tags                = local.tags
}

###############################################################################
# Sandbox image-pull identity (ACI)
#
# ACI requires a USER-ASSIGNED managed identity to pull from a private Azure
# Container Registry; a system-assigned identity is not supported for ACR pull
# on a container group. This identity exists for exactly that: it is attached to
# each sandbox container group by the application, and its only permission is
# AcrPull on this deployment's registry (rbac.tf).
#
# It deliberately has NO other role — no Key Vault read, no storage access, no
# application role. The consequence to keep in mind is that code running inside
# a sandbox can use the identity too, because the identity is attached to the
# group: the worst it can do is read (pull) the images in this registry, which
# is what the feature needs.
#
# It lives in the main resource group even when container groups live in the
# dedicated sandbox group; a user-assigned identity is a tenant/regional
# resource, and cross-resource-group attachment is supported. The application
# identity needs Microsoft.ManagedIdentity/userAssignedIdentities/assign/action
# on this identity to attach it (rbac.tf).
#
# It is created only when sandbox_backend = "aci".
###############################################################################

resource "azurerm_user_assigned_identity" "sandbox_pull" {
  count = local.sandbox_enabled ? 1 : 0

  name                = local.sandbox_pull_identity_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  tags                = local.tags
}
