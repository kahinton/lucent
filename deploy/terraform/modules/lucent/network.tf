###############################################################################
# Resource group
###############################################################################

resource "azurerm_resource_group" "this" {
  count = var.create_resource_group ? 1 : 0

  name     = local.resource_group_name
  location = var.location
  tags     = local.tags
}

data "azurerm_resource_group" "this" {
  count = var.create_resource_group ? 0 : 1

  name = local.resource_group_name
}

###############################################################################
# Sandbox resource group
#
# A dedicated resource group for ACI container groups, so the application
# identity's custom role can be assigned at a scope that contains nothing else.
# The role's `containerGroups/write` is, by nature, permission to run arbitrary
# images and commands; scoping it here bounds the blast radius to groups the
# application creates rather than to the deployment's database, vault, storage
# and Container Apps.
#
# The group is only created (or adopted) when sandbox_backend = "aci". It must
# be in the same region as the delegated subnet, because an ACI container group
# must be created in its subnet's region.
#
# A user-assigned identity is not a network resource; it lives in identity.tf.
###############################################################################

resource "azurerm_resource_group" "sandbox" {
  count = local.sandbox_enabled && var.create_sandbox_resource_group ? 1 : 0

  name     = local.sandbox_resource_group_name
  location = var.location
  tags     = local.tags
}

data "azurerm_resource_group" "sandbox" {
  count = local.sandbox_enabled && !var.create_sandbox_resource_group ? 1 : 0

  name = local.sandbox_resource_group_name
}

###############################################################################
# Virtual network
#
# Three subnets are always present, plus a fourth when the ACI sandbox backend
# is enabled (sandbox_backend = "aci"):
#   snet-cae   — the Container Apps environment. Azure requires /23 or larger
#                for a Consumption-only environment. It must NOT be delegated:
#                delegation to Microsoft.App/environments is only for
#                workload-profiles environments.
#   snet-postgres — delegated to Microsoft.DBforPostgreSQL/flexibleServers for
#                VNet integration. Azure requires at least /28.
#   snet-pe    — private endpoints for Key Vault, Container Registry and Blob
#                Storage.
#   snet-sandbox — delegated to Microsoft.ContainerInstance/containerGroups for
#                sandbox container groups (see below). Created only for ACI.
#
# The database is reached through VNet integration rather than a private
# endpoint. Both give a private-only database; integration is the simpler path
# and is what the design doc's "VNet-integrated" wording describes.
###############################################################################

resource "azurerm_virtual_network" "this" {
  count = var.create_vnet ? 1 : 0

  name                = local.vnet_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  address_space       = var.vnet_address_space
  tags                = local.tags
}

resource "azurerm_subnet" "container_apps" {
  count = var.create_vnet ? 1 : 0

  name                 = local.subnet_container_apps
  resource_group_name  = local.resource_group.name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.container_apps_subnet_prefix]

  # No delegation and no service endpoints: Container Apps requires an
  # otherwise-idle subnet.
}

resource "azurerm_subnet" "postgres" {
  count = var.create_vnet ? 1 : 0

  name                 = local.subnet_postgres
  resource_group_name  = local.resource_group.name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.postgres_subnet_prefix]

  delegation {
    name = "postgres-flexible-server"

    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "private_endpoints" {
  count = var.create_vnet ? 1 : 0

  name                 = local.subnet_private_endpoints
  resource_group_name  = local.resource_group.name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.private_endpoint_subnet_prefix]

  private_endpoint_network_policies = "Disabled"
}

###############################################################################
# Sandbox subnet (Azure Container Instances)
#
# ACI container groups deployed into a virtual network require a subnet that is
# DELEGATED to Microsoft.ContainerInstance/containerGroups. Once delegated the
# subnet can contain only container groups — no VMs, no private endpoints, no
# other resource types.
#
# Sizing. ACI allocates one private IP per container group from this subnet, and
# Microsoft documents that it cannot release IP mappings quickly enough for
# reuse. That is why Microsoft's current guidance is a /24 MINIMUM, which is the
# default here: a /24 gives 256 addresses, of which Azure reserves five, so
# roughly 250 concurrent container groups. Microsoft also asks for scale steps
# of about 1,000 groups per subnet (hard maximum 3,000) and recommends spreading
# production workloads across subnets. The variable validation rejects anything
# smaller than /24 for this reason — small subnets produce "subnet full"
# failures and groups stuck transitioning.
#
# No service endpoints: the workloads reach Key Vault, ACR and Blob Storage
# through the private endpoints in snet-pe, which the NSG's outbound rule
# admits (scoped to that subnet's CIDR when the module owns the VNet). A
# service endpoint would only matter if a
# sandbox itself talked to a Storage Account directly; it does not (sandboxes
# hold no storage identity), and the application reaches Blob Storage through
# the private endpoint from the Container Apps subnet.
#
# default_outbound_access_enabled = false. Azure is retiring the implicit
# default outbound path, and an ACI VNet group's only supported outbound
# configuration is a NAT gateway. Turning the implicit path off means egress is
# exactly what the NSG and NAT gateway below define, with no undocumented
# fallback that bypasses the allow list.
###############################################################################

resource "azurerm_subnet" "sandbox" {
  count = local.sandbox_enabled && var.create_vnet ? 1 : 0

  name                 = local.sandbox_subnet_name
  resource_group_name  = local.resource_group.name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.sandbox_subnet_prefix]

  default_outbound_access_enabled = false

  delegation {
    name = "aci-container-groups"

    service_delegation {
      name    = "Microsoft.ContainerInstance/containerGroups"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

###############################################################################
# Sandbox egress: NSG, NAT gateway
#
# The security model, stated plainly:
#
#   * ACI container groups are hypervisor-isolated from each other, but the
#     platform does NOT give a group its own network policy. An NSG is attached
#     to the SUBNET, so every group in the subnet gets the identical policy.
#     There is no per-sandbox allow list to enforce; the application's
#     "allowlist" mode cannot be implemented by this infrastructure. NSG rules
#     can only say "all groups may reach X" or "no group may reach X".
#   * Groups in the same subnet can reach each other by default (Microsoft
#     documents "direct communication between container groups in the same
#     subnet" as a supported scenario). The deny-inbound rule below removes
#     that general path, but it is a subnet-wide switch, not per-group
#     isolation: it also blocks every other inbound flow from the VNet, and any
#     exception added later applies to every sandbox at once.
#   * The only inbound exception is TCP 19390, and it is scoped: Microsoft
#     documents 19390 as the port used for PORTAL access (logs/shell) to
#     VNet-deployed container groups and as the data channel `az container
#     exec` may use. It is not a path ACI needs in order to keep a group
#     healthy, and blocking it does not leave groups stuck in "Waiting"; the
#     earlier claim to that effect was wrong. The rule's source is
#     local.sandbox_management_source_prefixes: the module-created Container
#     Apps subnet by default (the application's exec path originates there),
#     an operator-supplied list when one is set, and NO rule when an existing
#     VNet is adopted without one. Portal access from an operator workstation
#     therefore requires that list to be set explicitly. It is not an
#     application port, and container groups created by the application
#     declare only the API's placeholder port, no public IP and no DNS label.
#   * Outbound is default-deny. The explicit allows are the private-endpoint
#     subnet (when the module owns the VNet) or the whole VNet (when adopting
#     one — see the README), Azure's DNS resolver on 168.63.129.16:53, and —
#     when sandbox_internet_egress_enabled is true — TCP 443 to the Internet. That
#     last rule is deliberately broad: an NSG cannot match on FQDN, so "443 to
#     Internet" means any host on 443, not just the Lucent server the bridge
#     calls or the package registries a sandbox may need. FQDN allow lists
#     require an Azure Firewall plus a user-defined route, which this module
#     does not create; with a firewall in place, set
#     sandbox_internet_egress_enabled = false (or narrow this rule) so traffic
#     is forced through it.
#   * The NAT gateway is the outbound path ACI supports for VNet groups
#     (Microsoft: "the only supported configuration"). It gives egress a stable
#     public IP that an operator can put in a downstream allow list, and it is
#     where SNAT happens. The NAT gateway's public IP is for EGRESS ONLY; the
#     container groups themselves have no public IP, which is also an ACI
#     limitation for VNet-deployed groups.
###############################################################################

resource "azurerm_network_security_group" "sandbox" {
  count = local.sandbox_enabled ? 1 : 0

  name                = local.sandbox_nsg_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  tags                = local.tags

  # Private endpoints (Key Vault, ACR, Blob) plus any in-VNet DNS server. The
  # rule is outbound only; the deny-inbound rule below prevents this from
  # becoming group-to-group reachability.
  #
  # Destination scope: when this module owns the VNet the destination is the
  # private-endpoint subnet CIDR, so sandbox groups cannot reach the Container
  # Apps subnet, the PostgreSQL subnet or anything else in the VNet. When an
  # existing VNet is adopted its private-endpoint subnet CIDR is not known
  # here, so the destination is the VirtualNetwork tag — the whole VNet,
  # including the PostgreSQL subnet. That is broader than the module-created
  # case and is documented in the README.
  security_rule {
    name                         = "AllowVnetOutbound"
    description                  = "Private endpoints and in-VNet DNS: private-endpoint subnet when owned, whole adopted VNet otherwise."
    priority                     = 100
    direction                    = "Outbound"
    access                       = "Allow"
    protocol                     = "*"
    source_port_range            = "*"
    destination_port_range       = "*"
    source_address_prefix        = "*"
    destination_address_prefixes = local.sandbox_vnet_outbound_prefixes
  }

  # Azure's internal DNS resolver. It is a platform virtual IP, not part of the
  # VNet address space, so VirtualNetwork does not cover it.
  security_rule {
    name                       = "AllowAzureDnsOutbound"
    description                = "Azure-provided DNS at the platform virtual IP. Required to resolve the ACR private endpoint and the Lucent server."
    priority                   = 110
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "53"
    source_address_prefix      = "*"
    destination_address_prefix = "168.63.129.16"
  }

  # Broad by necessity: NSGs have no FQDN matching. See the header comment.
  dynamic "security_rule" {
    for_each = var.sandbox_internet_egress_enabled ? [1] : []

    content {
      name                       = "AllowInternetHttpsOutbound"
      description                = "HTTPS to the Internet: the bridge calls the Lucent server, and sandboxes may need package registries. NSGs cannot restrict this by hostname."
      priority                   = 120
      direction                  = "Outbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "443"
      source_address_prefix      = "*"
      destination_address_prefix = "Internet"
    }
  }

  # The one inbound exception, scoped to what actually needs it. Microsoft
  # documents TCP 19390 for PORTAL access (logs/shell) to VNet-deployed
  # container groups and as a channel container exec may use; it is not a
  # management path ACI requires to keep a group healthy. The source is
  # local.sandbox_management_source_prefixes — by default the module-created
  # Container Apps subnet, never the whole VNet. No rule is created when the
  # list is empty (the adopted-VNet default), so opt in explicitly for portal
  # or exec access from outside the Container Apps subnet.
  dynamic "security_rule" {
    for_each = local.sandbox_enabled && length(local.sandbox_management_source_prefixes) > 0 ? [1] : []

    content {
      name                       = "AllowAciManagementInbound"
      description                = "TCP 19390 for portal/exec access from allowed ranges. Not an application port."
      priority                   = 200
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "19390"
      source_address_prefixes    = local.sandbox_management_source_prefixes
      destination_address_prefix = "*"
    }
  }

  security_rule {
    name                       = "DenyAllInbound"
    description                = "Default deny. Removes ACI's documented same-subnet group-to-group path, and blocks every other inbound flow from the VNet and the Internet."
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyAllOutbound"
    description                = "Default deny. Everything not explicitly allowed above (including port 25, which ACI does not support) is dropped."
    priority                   = 4010
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "sandbox" {
  count = local.sandbox_enabled ? 1 : 0

  subnet_id                 = local.sandbox_subnet_id
  network_security_group_id = azurerm_network_security_group.sandbox[0].id

  lifecycle {
    precondition {
      condition     = var.create_vnet || var.existing_sandbox_subnet_id != null
      error_message = "sandbox_backend = \"aci\" with create_vnet = false requires existing_sandbox_subnet_id. Supply the resource ID of a subnet that is already delegated to Microsoft.ContainerInstance/containerGroups and contains no other resource type."
    }
  }
}

# Standard SKU for both, because a NAT gateway requires a Standard static public
# IP. The address is static so an operator can add it to a downstream allow
# list; it is egress only, and nothing listens on it.
resource "azurerm_public_ip" "sandbox_nat" {
  count = local.sandbox_enabled && var.sandbox_nat_gateway_enabled ? 1 : 0

  name                = local.sandbox_nat_public_ip_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_nat_gateway" "sandbox" {
  count = local.sandbox_enabled && var.sandbox_nat_gateway_enabled ? 1 : 0

  name                    = local.sandbox_nat_gateway_name
  resource_group_name     = local.resource_group.name
  location                = local.resource_group.location
  sku_name                = "Standard"
  idle_timeout_in_minutes = 10
  tags                    = local.tags
}

resource "azurerm_nat_gateway_public_ip_association" "sandbox" {
  count = local.sandbox_enabled && var.sandbox_nat_gateway_enabled ? 1 : 0

  nat_gateway_id       = azurerm_nat_gateway.sandbox[0].id
  public_ip_address_id = azurerm_public_ip.sandbox_nat[0].id
}

resource "azurerm_subnet_nat_gateway_association" "sandbox" {
  count = local.sandbox_enabled && var.sandbox_nat_gateway_enabled ? 1 : 0

  subnet_id      = local.sandbox_subnet_id
  nat_gateway_id = azurerm_nat_gateway.sandbox[0].id
}

###############################################################################
# Private DNS zones
#
# Two naming schemes, deliberately different:
#
#  * The PostgreSQL server is VNet-INTEGRATED, so its zone must end in
#    `.postgres.database.azure.com`. Azure rejects any other suffix, and rejects
#    a link to a zone named `privatelink.postgres.database.azure.com`.
#  * Key Vault, Container Registry and Blob Storage are reached through PRIVATE
#    ENDPOINTS, so their zones use the standard `privatelink.*` names.
#
# Each zone is linked to the VNet so the Container Apps environment resolves the
# private addresses.
#
# The three PRIVATE-LINK zones have fixed, product-wide names
# (`privatelink.vaultcore.azure.net`, `privatelink.azurecr.io`,
# `privatelink.blob.core.windows.net`). Azure rejects linking two zones with the
# same name to one VNet, so composing this module into an existing VNet that
# already owns one of those zones fails the apply if the module creates its own.
# That is the normal case when the VNet already has another Key Vault, registry
# or storage account behind a private endpoint. Each of the three therefore has
# a create/existing pair: set `create_<service>_private_dns_zone = false` and
# supply `existing_<service>_private_dns_zone_id` to attach the private endpoint
# to the zone that already exists. In that mode the module does not create a
# zone or a VNet link — the existing zone is expected to be linked to the VNet
# already (which is what the duplicate-zone failure implies).
#
# The PostgreSQL zone does not need this treatment: its name embeds
# name_prefix/environment, so it is deployment-specific.
###############################################################################

resource "azurerm_private_dns_zone" "postgres" {
  name                = local.postgres_dns_zone_name
  resource_group_name = local.resource_group.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone" "key_vault" {
  count = var.create_key_vault_private_dns_zone ? 1 : 0

  name                = local.key_vault_dns_zone
  resource_group_name = local.resource_group.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone" "acr" {
  count = var.create_acr_private_dns_zone ? 1 : 0

  name                = local.acr_dns_zone
  resource_group_name = local.resource_group.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone" "blob" {
  count = var.create_blob_private_dns_zone ? 1 : 0

  name                = local.blob_dns_zone
  resource_group_name = local.resource_group.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                 = "${local.base}-pg-dns-link"
  private_dns_zone_id  = azurerm_private_dns_zone.postgres.id
  virtual_network_id   = local.vnet_id
  registration_enabled = false
  tags                 = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "key_vault" {
  count = var.create_key_vault_private_dns_zone ? 1 : 0

  name                 = "${local.base}-kv-dns-link"
  private_dns_zone_id  = azurerm_private_dns_zone.key_vault[0].id
  virtual_network_id   = local.vnet_id
  registration_enabled = false
  tags                 = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "acr" {
  count = var.create_acr_private_dns_zone ? 1 : 0

  name                 = "${local.base}-acr-dns-link"
  private_dns_zone_id  = azurerm_private_dns_zone.acr[0].id
  virtual_network_id   = local.vnet_id
  registration_enabled = false
  tags                 = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  count = var.create_blob_private_dns_zone ? 1 : 0

  name                 = "${local.base}-blob-dns-link"
  private_dns_zone_id  = azurerm_private_dns_zone.blob[0].id
  virtual_network_id   = local.vnet_id
  registration_enabled = false
  tags                 = local.tags
}
