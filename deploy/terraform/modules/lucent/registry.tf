###############################################################################
# Azure Container Registry
#
# The repository builds nothing and pushes nowhere today, so this module both
# creates the registry AND does nothing to put an image in it. Supplying a
# usable `var.image` is the operator's job — see the README's "Before you apply"
# section.
#
# SKU coherence is enforced here rather than left to fail mid-apply. Azure
# Container Registry supports a private endpoint AND disabling public network
# access only on the Premium SKU; Basic and Standard support neither (Microsoft:
# "Private link with private endpoints — N/A" for Basic and Standard, and public
# IP network rules are Premium-only). The defaults are therefore Premium + a
# private endpoint + public access disabled together. An operator who asks for a
# private registry on a SKU that cannot provide one is stopped by the
# preconditions below with a message that says so, instead of Azure rejecting the
# deployment resource-by-resource.
#
# Public network access defaults to disabled, which matches the design doc's
# private-networking posture. That has a consequence worth stating plainly: with
# public access off, an image cannot be pushed from an operator workstation. Push
# from a host inside the VNet, or temporarily set
# container_registry_public_network_access_enabled = true for the first push and
# turn it back off afterwards.
###############################################################################

resource "azurerm_container_registry" "this" {
  count = var.create_container_registry ? 1 : 0

  name                = local.container_registry_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  sku                 = var.container_registry_sku
  admin_enabled       = false

  public_network_access_enabled = var.container_registry_public_network_access_enabled

  tags = local.tags

  lifecycle {
    # A private endpoint requires Premium. Fail here, with a message that names
    # the variable to change, rather than letting the private endpoint below be
    # rejected by Azure.
    precondition {
      condition     = !var.container_registry_private_endpoint_enabled || var.container_registry_sku == "Premium"
      error_message = "A private endpoint for Azure Container Registry requires the Premium SKU (container_registry_sku = \"Premium\"). Basic and Standard registries do not support private endpoints. Set container_registry_private_endpoint_enabled = false to run a cheaper public registry, or move the SKU to Premium."
    }

    # Disabling public network access is likewise Premium-only.
    precondition {
      condition     = var.container_registry_public_network_access_enabled || var.container_registry_sku == "Premium"
      error_message = "Disabling public network access on Azure Container Registry requires the Premium SKU (container_registry_sku = \"Premium\"). Basic and Standard registries cannot have public access disabled. Either set container_registry_public_network_access_enabled = true, or move the SKU to Premium."
    }
  }
}

# Created only when a private endpoint is actually wanted. A Basic or Standard
# registry cannot have one, and the precondition on the registry above stops the
# contradictory configuration before this resource is reached.
resource "azurerm_private_endpoint" "acr" {
  count = var.create_container_registry && var.container_registry_private_endpoint_enabled ? 1 : 0

  name                = "${local.base}-acr-pe"
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  subnet_id           = local.private_endpoint_subnet_id
  tags                = local.tags

  private_service_connection {
    name                           = "${local.base}-acr-psc"
    private_connection_resource_id = azurerm_container_registry.this[0].id
    is_manual_connection           = false
    subresource_names              = ["registry"]
  }

  private_dns_zone_group {
    name                 = "acr"
    private_dns_zone_ids = [local.acr_dns_zone_id]
  }

  lifecycle {
    precondition {
      condition     = var.create_acr_private_dns_zone || var.existing_acr_private_dns_zone_id != null
      error_message = "create_acr_private_dns_zone = false requires existing_acr_private_dns_zone_id: the private endpoint must be attached to a privatelink.azurecr.io zone that is already linked to the target VNet."
    }
  }
}
