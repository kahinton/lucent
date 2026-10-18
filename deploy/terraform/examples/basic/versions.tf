terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 5.10.0, < 6.0.0"
    }
  }
}

provider "azurerm" {
  features {
    # Database provisioning in this module is done by a Container Apps job rather
    # than by Terraform, so the provider does not need to restart the server when
    # a configuration value changes.
    postgresql_flexible_server {
      restart_server_on_configuration_value_change = false
    }

    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
  }

  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id

  # REQUIRED by this module, not optional. The storage account rejects
  # shared-key (account-key) requests, so any storage operation Terraform makes
  # over the data plane — today none, since this module's container resource goes
  # through the ARM control plane, but the provider's own note says to set this
  # once shared-key access is off — has to be authorized with Entra ID.
  #
  # Two consequences to expect if you add data-plane storage resources or run
  # data-plane tooling against the account. The identity doing so needs a BLOB
  # DATA-PLANE role on the container or account (Storage Blob Data Contributor, or
  # Owner, which includes data actions) — a management role such as Storage
  # Account Contributor is not enough for the data plane. And it needs network
  # access to the blob data plane: see deploy/terraform/README.md, "Reaching the
  # blob data plane".
  storage_use_azuread = true
}
