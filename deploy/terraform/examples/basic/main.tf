###############################################################################
# Minimal runnable example.
#
# This is a thin root configuration: it supplies provider credentials and the
# handful of values that cannot have a sensible default, then hands everything
# else to the module's own defaults. Copy it, then read
# deploy/terraform/README.md — in particular the "Before you apply" section,
# because `image` must point at an image that already exists in a registry the
# new Container Apps environment can reach.
###############################################################################

module "lucent" {
  source = "../../modules/lucent"

  # Identity and placement. tenant_id has no default because guessing it would
  # only produce a confusing failure at the Key Vault.
  tenant_id = var.tenant_id
  location  = var.location

  # Optional. The controller needs to know which subscription its workers job
  # lives in; leaving this null uses the caller's subscription.
  subscription_id = var.subscription_id

  # The image. Nothing in this repository builds or pushes one; see the README.
  image = var.image

  # The Entra ID principal (preferably a group) that becomes the PostgreSQL
  # administrator and can therefore run CREATE EXTENSION.
  entra_admin_object_id      = var.entra_admin_object_id
  entra_admin_principal_name = var.entra_admin_principal_name

  # The Key Vault is private by default (public network access disabled), and
  # Terraform has to reach its data plane to write and re-read the secrets. IP
  # rules do NOT get you in while public access is disabled — they are ignored —
  # so this only helps if you also set key_vault_public_network_access_enabled.
  # The intended path is to run Terraform from inside the VNet (or a host with
  # private connectivity). See the module README, "Writing secrets into a private
  # vault".
  key_vault_allowed_ip_ranges = var.key_vault_allowed_ip_ranges

  name_prefix = var.name_prefix
  environment = var.environment

  # Optional. "docker" (the default) creates no ACI infrastructure; "aci"
  # provisions the delegated subnet, egress path, image-pull identity, narrow
  # RBAC and logging policy for task sandboxes. See the README section
  # "Task sandboxes on Azure Container Instances".
  sandbox_backend = var.sandbox_backend

  # Optional provider credentials. Anything left null is simply not stored.
  github_token      = var.github_token
  anthropic_api_key = var.anthropic_api_key

  tags = var.tags
}
