###############################################################################
# Key Vault
#
# Every application secret lives here and reaches the containers as a Container
# Apps secret reference backed by the vault and read with a managed identity —
# never as a literal value in the container spec. `src/lucent/secrets/azure.py`
# is still an unimplemented stub, but none of that matters: Container Apps does
# the retrieval at the platform layer, so the application needs no change.
#
# RBAC authorization is on (required by azurerm 5.x), not access policies, so
# grants are ordinary role assignments.
#
# The vault is private by default: public network access is disabled and the
# trusted-services bypass is "None", so the vault is reachable only through its
# private endpoint. That is Microsoft's guidance for a private-endpoint vault,
# and it also removes the "AzureServices" bypass, which admits a broad set of
# Azure services rather than this deployment's VNet and is not suppressed by
# disabling public access. The consequence — Terraform cannot write secrets into
# a vault it cannot reach — is documented in the README and in the
# key_vault_public_network_access_enabled variable.
#
# Grants are per SECRET, not per vault, so each identity can read only the
# secrets it is actually given. See rbac.tf.
###############################################################################

resource "azurerm_key_vault" "this" {
  name                = local.key_vault_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  tenant_id           = var.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true

  purge_protection_enabled      = var.key_vault_purge_protection_enabled
  soft_delete_retention_days    = var.key_vault_soft_delete_retention_days
  public_network_access_enabled = var.key_vault_public_network_access_enabled

  network_acls {
    bypass         = var.key_vault_network_bypass
    default_action = var.key_vault_network_default_action
    ip_rules       = var.key_vault_allowed_ip_ranges
  }

  tags = local.tags
}

resource "azurerm_private_endpoint" "key_vault" {
  name                = "${local.base}-kv-pe"
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  subnet_id           = local.private_endpoint_subnet_id
  tags                = local.tags

  private_service_connection {
    name                           = "${local.base}-kv-psc"
    private_connection_resource_id = azurerm_key_vault.this.id
    is_manual_connection           = false
    subresource_names              = ["vault"]
  }

  private_dns_zone_group {
    name                 = "key-vault"
    private_dns_zone_ids = [local.key_vault_dns_zone_id]
  }

  lifecycle {
    precondition {
      condition     = var.create_key_vault_private_dns_zone || var.existing_key_vault_private_dns_zone_id != null
      error_message = "create_key_vault_private_dns_zone = false requires existing_key_vault_private_dns_zone_id: the private endpoint must be attached to a privatelink.vaultcore.azure.net zone that is already linked to the target VNet."
    }
  }
}

###############################################################################
# Secrets
#
# Key Vault secret names may contain only alphanumerics and hyphens, and
# Container Apps secret names are capped at 20 characters, so each environment
# variable name is mapped to a short, stable name for the container spec.
###############################################################################

locals {
  # Iterate the non-sensitive KEY SET, not application_secrets. Deriving this
  # map from the sensitive map taints the names, which in turn makes the
  # key_vault_application_secret_names output sensitive and breaks a consumer's
  # plain root output. The keys are environment-variable names; the values are
  # still read from application_secrets where they are actually needed.
  container_secret_name = {
    for env_name in local.application_secret_keys :
    env_name => length(replace(lower(env_name), "_", "-")) <= 20 ? replace(lower(env_name), "_", "-") : format(
      "%s-%s",
      trim(substr(replace(lower(env_name), "_", "-"), 0, 14), "-"),
      substr(md5(env_name), 0, 4),
    )
  }
}

resource "azurerm_key_vault_secret" "application" {
  # Keys are environment-variable names, not secret material; the value is
  # looked up here and remains sensitive. See application_secret_keys in
  # locals.tf for why the map itself cannot be the for_each argument.
  for_each = local.application_secret_keys

  name         = local.key_vault_secret_name[each.key]
  value        = local.application_secrets[each.key]
  key_vault_id = azurerm_key_vault.this.id

  tags = local.tags
}
