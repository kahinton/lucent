terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 5.10.0, < 6.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0, < 4.0.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.2.0, < 4.0.0"
    }
  }
}

# The module never configures the provider itself — features and subscription
# selection belong to the root module so this can be composed into an existing
# landing zone. See examples/basic for a minimal root configuration.
#
# One provider argument IS a requirement of this module, and no resource can
# assert it for you: `storage_use_azuread = true` (see storage.tf). The file
# storage account denies shared-key access, so storage data-plane operations have
# to be authorized with Entra ID rather than an account key. This module's own
# resources currently go through the ARM control plane (which never used a shared
# key), so the flag is a requirement for anything data-plane added later or run
# against the account — and the provider's storage_account documentation asks for
# it once shared-key access is off. examples/basic sets it.
