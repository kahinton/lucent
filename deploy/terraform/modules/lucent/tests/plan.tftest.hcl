###############################################################################
# Mock-provider plan regression tests
#
# Run from this module directory:
#   terraform init -backend=false
#   terraform test
#
# No Azure credentials are used and no Azure API is called: mock_provider
# substitutes every azurerm/random/null provider. The tests exist so the plan
# shape cannot silently regress again — in particular that
#   * the default docker path still creates no sandbox resources,
#   * the ACI path carries the sandbox configuration into the controller and
#     the workers job (not only the web app), with the /api bridge base, and
#   * extra_secrets leaves the secret-NAME output non-sensitive.
#
# The variables below are the module's only required inputs. A fixed public URL
# keeps the derived bridge URL known at plan time.
###############################################################################

mock_provider "azurerm" {}
mock_provider "random" {}
mock_provider "null" {}

variables {
  tenant_id         = "00000000-0000-0000-0000-000000000000"
  image             = "example.azurecr.io/lucent:test"
  lucent_public_url = "https://lucent.example.com"
}

run "docker_default" {
  command = plan

  variables {
    sandbox_backend = "docker"
  }

  assert {
    condition     = output.sandbox_backend == "docker"
    error_message = "The default backend must be docker."
  }

  assert {
    condition     = output.sandbox_subnet_id == null
    error_message = "docker must create no ACI subnet."
  }

  assert {
    condition = !contains(
      [for env in azurerm_container_app.controller.template[0].container[0].env : env.name],
      "LUCENT_SANDBOX_BACKEND",
    )
    error_message = "docker must inject no sandbox selector into the controller."
  }

  assert {
    condition = !contains(
      [for env in azurerm_container_app_job.workers.template[0].container[0].env : env.name],
      "LUCENT_SANDBOX_BACKEND",
    )
    error_message = "docker must inject no sandbox selector into the workers job."
  }

  assert {
    condition = !contains(
      [for env in azurerm_container_app.web.template[0].container[0].env : env.name],
      "LUCENT_SANDBOX_BACKEND",
    )
    error_message = "docker must inject no sandbox selector into the web app."
  }
}

run "aci" {
  command = plan

  variables {
    sandbox_backend = "aci"
  }

  assert {
    condition     = output.sandbox_backend == "aci"
    error_message = "The ACI backend must be selected."
  }

  assert {
    condition     = output.sandbox_subnet_prefix == "10.42.4.0/24"
    error_message = "The default ACI subnet prefix must be planned."
  }

  # The controller is what builds worker environments through
  # WORKER_ENV_ALLOWLIST, so the sandbox settings must be present here.
  assert {
    condition = alltrue([
      for name in [
        "LUCENT_SANDBOX_BACKEND",
        "LUCENT_SANDBOX_ACI_RESOURCE_GROUP",
        "LUCENT_SANDBOX_ACI_SUBNET_ID",
        "LUCENT_SANDBOX_ACI_IDENTITY_ID",
        "LUCENT_SANDBOX_ACI_LOCATION",
        "LUCENT_SANDBOX_BRIDGE_IMAGE",
        "LUCENT_SANDBOX_BRIDGE_API_URL",
        "AZURE_SUBSCRIPTION_ID",
        "LUCENT_WORKER_AZURE_CLIENT_ID",
        ] : contains(
        [for env in azurerm_container_app.controller.template[0].container[0].env : env.name],
        name,
      )
    ])
    error_message = "The controller environment must carry the sandbox configuration and the worker app-identity client id."
  }

  # The controller's own AZURE_CLIENT_ID must stay the controller identity;
  # the app identity travels under LUCENT_WORKER_AZURE_CLIENT_ID.
  assert {
    condition = (
      contains(
        [for env in azurerm_container_app.controller.template[0].container[0].env : env.name],
        "AZURE_CLIENT_ID",
      )
      && contains(
        [for env in azurerm_container_app.controller.template[0].container[0].env : env.name],
        "LUCENT_WORKER_AZURE_CLIENT_ID",
      )
    )
    error_message = "The controller must keep its own AZURE_CLIENT_ID and expose the app identity separately."
  }

  # The workers job template is the base execution template the Azure launcher
  # copies, so it must carry the sandbox settings too.
  assert {
    condition = alltrue([
      for name in [
        "LUCENT_SANDBOX_BACKEND",
        "LUCENT_SANDBOX_ACI_RESOURCE_GROUP",
        "LUCENT_SANDBOX_ACI_SUBNET_ID",
        "LUCENT_SANDBOX_ACI_IDENTITY_ID",
        "LUCENT_SANDBOX_ACI_LOCATION",
        "LUCENT_SANDBOX_BRIDGE_IMAGE",
        "LUCENT_SANDBOX_BRIDGE_API_URL",
        "AZURE_SUBSCRIPTION_ID",
        "LUCENT_WORKER_AZURE_CLIENT_ID",
        ] : contains(
        [for env in azurerm_container_app_job.workers.template[0].container[0].env : env.name],
        name,
      )
    ])
    error_message = "The workers job template must carry the sandbox configuration."
  }

  # The bridge appends paths such as /memories to this base; the server mounts
  # them under /api, so a value without the suffix makes every call 404.
  assert {
    condition = alltrue([
      for env in azurerm_container_app.controller.template[0].container[0].env :
      env.value == "https://lucent.example.com/api"
      if env.name == "LUCENT_SANDBOX_BRIDGE_API_URL"
    ])
    error_message = "LUCENT_SANDBOX_BRIDGE_API_URL must end in /api."
  }

  assert {
    condition = alltrue([
      for env in azurerm_container_app.web.template[0].container[0].env :
      env.value == "https://lucent.example.com/api"
      if env.name == "LUCENT_SANDBOX_BRIDGE_API_URL"
    ])
    error_message = "The web app's LUCENT_SANDBOX_BRIDGE_API_URL must end in /api."
  }

  # Module-owned networking scopes outbound reach to the private-endpoint
  # subnet, and the 19390 exception to the Container Apps subnet.
  assert {
    condition = (
      [for rule in azurerm_network_security_group.sandbox[0].security_rule : rule.destination_address_prefixes
      if rule.name == "AllowVnetOutbound"][0]
      == toset(["10.42.3.0/24"])
    )
    error_message = "AllowVnetOutbound must be scoped to the private-endpoint subnet CIDR."
  }

  assert {
    condition = (
      [for rule in azurerm_network_security_group.sandbox[0].security_rule : rule.source_address_prefixes
      if rule.name == "AllowAciManagementInbound"][0]
      == toset(["10.42.0.0/23"])
    )
    error_message = "The 19390 exception must be scoped to the Container Apps subnet."
  }
}

run "aci_existing_vnet" {
  command = plan

  variables {
    create_vnet                         = false
    sandbox_backend                     = "aci"
    existing_vnet_id                    = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet"
    existing_container_apps_subnet_id   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-cae"
    existing_postgres_subnet_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-pg"
    existing_private_endpoint_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-pe"
    existing_sandbox_subnet_id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-sandbox"
  }

  # Adopting a VNet means the private-endpoint CIDR is unknown, so outbound is
  # the whole VirtualNetwork tag; document-visible breadth, not an accident.
  assert {
    condition = (
      [for rule in azurerm_network_security_group.sandbox[0].security_rule : rule.destination_address_prefixes
      if rule.name == "AllowVnetOutbound"][0]
      == toset(["VirtualNetwork"])
    )
    error_message = "An adopted VNet must fall back to the whole VirtualNetwork tag."
  }

  # No existing-subnet CIDR is known, so no 19390 rule is created by default.
  assert {
    condition = !contains(
      [for rule in azurerm_network_security_group.sandbox[0].security_rule : rule.name],
      "AllowAciManagementInbound",
    )
    error_message = "An adopted VNet must not open 19390 implicitly."
  }
}

run "malformed_existing_vnet_id" {
  command = plan

  variables {
    create_vnet                         = false
    sandbox_backend                     = "aci"
    existing_vnet_id                    = "not-a-resource-id"
    existing_container_apps_subnet_id   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-cae"
    existing_postgres_subnet_id         = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-pg"
    existing_private_endpoint_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-pe"
    existing_sandbox_subnet_id          = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/snet-sandbox"
  }

  expect_failures = [var.existing_vnet_id]
}

run "extra_secrets" {
  command = plan

  variables {
    sandbox_backend = "docker"
    extra_secrets = {
      OPERATOR_ADDED_SECRET = "operator-value"
    }
  }

  # This output is derived from the secret NAME set. If it were derived from
  # the sensitive map it would itself be sensitive and this assertion would
  # fail, which is the regression this run guards.
  assert {
    condition     = output.key_vault_application_secret_names["OPERATOR_ADDED_SECRET"] == "OPERATOR-ADDED-SECRET"
    error_message = "The secret-name output must expose the derived name (non-sensitive)."
  }

  assert {
    condition     = azurerm_key_vault_secret.application["OPERATOR_ADDED_SECRET"].name == "OPERATOR-ADDED-SECRET"
    error_message = "extra_secrets must be written to Key Vault under the underscore-to-hyphen name."
  }
}
