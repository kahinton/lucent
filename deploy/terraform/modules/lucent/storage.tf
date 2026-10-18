###############################################################################
# Azure Blob Storage for user files
#
# The application's file storage boundary is pluggable
# (src/lucent/storage/providers.py): `LocalFileStorageProvider` writes to
# LUCENT_FILE_STORAGE_PATH, and `AzureBlobStorageProvider` writes to a blob
# container through a managed identity. On Container Apps the local provider is
# the wrong default for a beta — replicas share no disk, so an upload lands on
# one replica's ephemeral layer, vanishes on restart, and is invisible to the
# other replica. This file creates the blob account and container that the
# application-side provider needs.
#
# Almost every security-relevant argument below is written EXPLICITLY, because
# several Azure defaults are permissive and differ from what is wanted here:
#
#   * shared_access_key_enabled   the provider schema defaults to TRUE. Set false,
#     which makes Azure reject every request authorized with the account access
#     key. That disables account SAS and service SAS outright (both are
#     Shared-Key signed) and leaves Entra ID as the only way in. The keys
#     themselves are NOT deleted: they still exist and a management-plane
#     principal can still retrieve them with listKeys. What the setting removes
#     is their usability for DATA-PLANE authentication — there is no *usable*
#     key for a workload to leak.
#   * allow_nested_items_to_be_public  azurerm 5.10.0 defaults this to false;
#     set explicitly anyway so no container may opt into anonymous public blob
#     access, now or later.
#   * min_tls_version / https_traffic_only_enabled  set explicitly so the
#     posture is readable here rather than inherited from account defaults.
#   * cross_tenant_replication_enabled  the azurerm 5.10.0 schema default is
#     false, and modern Azure also defaults new accounts to false; set
#     explicitly anyway so the posture does not depend on either default. False
#     forbids replicating this account's data into a DIFFERENT tenant.
#   * network rules  inline below, set as part of the account's own create, so
#     default Deny, no trusted-services bypass and no IP ranges are in force
#     from the moment the account exists. The public endpoint answers nothing;
#     the only route in is the private endpoint.
#
# Standard (non-HNS) storage, LRS by default: this is a beta file store, not an
# analytics data lake, so a hierarchical namespace would add a different API
# surface (Data Lake Gen2, its own `dfs` endpoint and its own private DNS zone)
# for no benefit, and ZRS/GRS would roughly double the cost.
#
# Soft delete is on for blobs and containers, and it has an erasure consequence
# that is documented in the README and in
# delete_retention_policy.permanent_delete_enabled: a deleted tenant file's
# bytes remain recoverable and billable for the retention window, and a
# soft-deleted BASE blob cannot be purged before that window expires without
# turning soft delete off account-wide. Overwrites likewise accumulate
# soft-deleted snapshots while versioning is off.
#
# Deliberately NOT set here:
#   * immutability_policy / legal hold — a WORM policy is exactly what must NOT
#     exist on a store that holds a tenant's data, because it makes genuine
#     deletion (offboarding, an erasure request) impossible until the period
#     expires. There is no variable for it on purpose; add it only if you also
#     accept that you can no longer delete a tenant's files.
#   * public_network_access — the provider deprecates
#     `public_network_access_enabled` in 5.x in favour of the `public_network_access`
#     string, and the inline network rules already deny the public endpoint.
#     Setting it to "Disabled" as well would additionally make
#     storage_allowed_ip_ranges useless (IP rules are ignored when public access
#     is off — the same trap the Key Vault has). Left alone so the IP-rule escape
#     hatch documented in the README still works.
###############################################################################

resource "azurerm_storage_account" "files" {
  name                = local.storage_account_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location

  account_tier             = "Standard"
  account_replication_type = var.storage_account_replication_type
  account_kind             = "StorageV2"
  is_hns_enabled           = false

  shared_access_key_enabled        = false
  allow_nested_items_to_be_public  = false
  min_tls_version                  = "TLS1_2"
  https_traffic_only_enabled       = true
  cross_tenant_replication_enabled = false

  # Soft delete for both blobs and containers. This is the cheap protection
  # against an accidental or malicious delete/overwrite: it is a per-account
  # setting with no per-operation cost, and it expires on its own.
  #
  # `permanent_delete_enabled` defaults to false, which means a soft-deleted
  # SNAPSHOT OR VERSION cannot be removed before its retention period expires.
  # Setting it true (storage_permanent_delete_enabled) lets a caller with data
  # permissions permanently delete those snapshots/versions early; it does not
  # by itself purge anything, is account-wide, and is therefore a deliberate
  # erasure-sensitive trade-off rather than a safe default.
  #
  # Versioning is a separate question and is OFF by default — see
  # storage_blob_versioning_enabled and the README. Versions have no expiry of
  # their own, so enabling this keeps every overwritten copy of a file until
  # every version is explicitly deleted, which is a standing retention
  # obligation and makes deleting a tenant's data harder, not easier.
  blob_properties {
    versioning_enabled = var.storage_blob_versioning_enabled

    delete_retention_policy {
      days                     = var.storage_soft_delete_retention_days
      permanent_delete_enabled = var.storage_permanent_delete_enabled
    }

    container_delete_retention_policy {
      days = var.storage_soft_delete_retention_days
    }
  }

  # The firewall is part of the account's own create rather than a separate
  # resource applied afterwards. Azure's default for a new account is public
  # access with no firewall; a standalone azurerm_storage_account_network_rules
  # is a second API call, so an apply that fails in between leaves the account
  # reachable from all networks. Setting the block inline means there is no such
  # window.
  #
  # This is safe in azurerm 5.10.0 because azurerm_storage_container is a
  # CONTROL-PLANE resource here (see the note on the container below): Azure's
  # storage firewall governs data-plane operations, so it does not block the
  # container's create or refresh. If a future provider moves that resource to
  # the data plane, this configuration fails loudly at the container rather than
  # opening the account — the opposite of the old split's failure mode.
  #
  # The provider's documentation warns against configuring network rules both
  # inline and via azurerm_storage_account_network_rules against the same
  # account ("Spurious changes will occur if both are used"), so it is one or the
  # other. This module uses the inline block only.
  network_rules {
    default_action = var.storage_network_default_action
    bypass         = ["None"]
    ip_rules       = var.storage_allowed_ip_ranges
  }

  tags = local.tags

  lifecycle {
    # The account holds every tenant file and its soft-deleted copies. A name
    # change (name_prefix or environment) or a mistaken destroy would otherwise
    # replace it and delete all of that data with no recovery path. The database
    # and vault have equivalent guards; this is the same rule for the file
    # store. There is deliberately no variable: lifting it is an edit here,
    # after reading the README's teardown note.
    prevent_destroy = true
  }
}

###############################################################################
# Container
#
# Private access: `blob` or `container` access would make every file readable by
# anyone with the URL, which is the opposite of what a per-tenant file store
# wants, and the account denies nested public access anyway.
#
# In azurerm 5.10.0 this resource is managed over the ARM CONTROL PLANE: it
# takes its client from the provider's resource-manager bundle
# (`Storage.ResourceManager.BlobContainers`, i.e.
# `Microsoft.Storage/storageAccounts/blobServices/containers`) and makes no
# data-plane request, so the account's inline firewall does not block it.
###############################################################################

resource "azurerm_storage_container" "files" {
  name                  = var.storage_container_name
  storage_account_id    = azurerm_storage_account.files.id
  container_access_type = "private"

  lifecycle {
    # The application stores only `provider` + `storage_key` on each file row —
    # never the container name. Renaming the container therefore strands every
    # row even though the bytes still exist under the old name. A change to
    # storage_container_name would replace this resource; block it and require
    # a deliberate edit, as with the account.
    prevent_destroy = true
  }
}

###############################################################################
# Private endpoint
#
# `blob` is the sub-resource (and privatelink.blob.core.windows.net the zone —
# see network.tf). The workload resolves <account>.blob.core.windows.net to the
# private address through that linked zone, using the unchanged endpoint host,
# so the application needs no `privatelink` hostname of its own.
#
# Traffic that arrives over this private endpoint is not subject to the network
# rules: Azure's storage firewall only controls the PUBLIC endpoint, and a
# private endpoint is admitted by its own consent/approval flow instead. So
# nothing needs to be added to `virtual_network_subnet_ids` for the apps to
# reach the account.
###############################################################################

resource "azurerm_private_endpoint" "blob" {
  name                = "${local.base}-blob-pe"
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location
  subnet_id           = local.private_endpoint_subnet_id
  tags                = local.tags

  private_service_connection {
    name                           = "${local.base}-blob-psc"
    private_connection_resource_id = azurerm_storage_account.files.id
    is_manual_connection           = false
    subresource_names              = ["blob"]
  }

  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [local.blob_dns_zone_id]
  }

  lifecycle {
    precondition {
      condition     = var.create_blob_private_dns_zone || var.existing_blob_private_dns_zone_id != null
      error_message = "create_blob_private_dns_zone = false requires existing_blob_private_dns_zone_id: the private endpoint must be attached to a privatelink.blob.core.windows.net zone that is already linked to the target VNet."
    }
  }
}
