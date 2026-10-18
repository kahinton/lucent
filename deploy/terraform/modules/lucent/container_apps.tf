###############################################################################
# Container Apps environment
#
# Consumption-only: no `workload_profile` blocks are declared, which is what
# makes this the legacy Consumption environment type. Two consequences, both
# wanted here:
#
#   * the subnet must be at least a /23 and must NOT be delegated;
#   * no Dedicated Plan Management charge is triggered. That meter (~$0.10/hour)
#     fires for dedicated workload profiles, private endpoints INTO Container
#     Apps, and planned maintenance. This module deliberately creates none of
#     those: the web app is reached over its public managed-certificate endpoint,
#     not a private endpoint, so the charge stays off.
#
# The trade-off is that a Consumption-only environment cannot use UDRs, Azure
# NAT Gateway, or private endpoints into the environment itself. If those are
# required, switch to a workload-profiles environment, move to a /27 subnet,
# delegate it to Microsoft.App/environments, and expect the management charge.
###############################################################################

resource "azurerm_container_app_environment" "this" {
  name                       = local.container_app_environment_name
  resource_group_name        = local.resource_group.name
  location                   = local.resource_group.location
  log_analytics_workspace_id = local.log_analytics_workspace_id

  infrastructure_subnet_id = local.container_apps_subnet_id

  # External (not internal-LB) so the environment gets a public ingress endpoint
  # and Azure's free managed certificate gives TLS without fronting it with
  # Front Door or Application Gateway, which the design doc defers past beta.
  internal_load_balancer_enabled = false

  tags = local.tags
}

###############################################################################
# Web / API
#
# Serves the UI, the REST API and MCP on a single port (8766). Scale-to-zero is
# the default and is the largest single cost lever in the design: inbound HTTP
# wakes it.
#
# Known limit, from the design doc: /api/health is a liveness probe only — it
# returns a static payload with no database check and there is no readiness
# endpoint, so a replica can pass its probe while still running migrations.
# Nothing here can fix that; it is an application change.
###############################################################################

resource "azurerm_container_app" "web" {
  name                         = local.web_app_name
  resource_group_name          = local.resource_group.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  tags = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  # Pull with the managed identity. `admin_enabled = false` on the registry means
  # there is no admin password to fall back on.
  registry {
    server   = local.container_registry_login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  # Secrets are Key Vault references, never literal values. The URI is
  # version-less on purpose: Container Apps re-reads the latest version within
  # 30 minutes and restarts the revision that references it, which is what makes
  # rotation a vault operation rather than a redeploy.
  dynamic "secret" {
    for_each = local.web_secret_keys

    content {
      name                = local.container_secret_name[secret.key]
      identity            = azurerm_user_assigned_identity.app.id
      key_vault_secret_id = azurerm_key_vault_secret.application[secret.key].versionless_id
    }
  }

  ingress {
    external_enabled           = true
    target_port                = 8766
    allow_insecure_connections = false

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = var.web_min_replicas
    max_replicas = var.web_max_replicas

    container {
      name   = "lucent-web"
      image  = var.image
      cpu    = var.web_cpu
      memory = var.web_memory

      dynamic "env" {
        for_each = local.web_environment

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.web_secret_keys

        content {
          name        = env.key
          secret_name = local.container_secret_name[env.key]
        }
      }

      # The image's own HEALTHCHECK is not used by Container Apps; the probe has
      # to be declared. Path and port match the Dockerfile.
      liveness_probe {
        transport = "HTTP"
        port      = 8766
        path      = "/api/health"
      }

      # Generous on purpose. Migrations run during startup, before the server
      # serves, and there is no readiness endpoint to distinguish "still
      # migrating" from "broken" — so the startup probe has to tolerate a slow
      # first boot rather than let Container Apps kill the replica mid-migration.
      startup_probe {
        transport               = "HTTP"
        port                    = 8766
        path                    = "/api/health"
        interval_seconds        = 10
        failure_count_threshold = 30
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.app_acr_pull,
    azurerm_role_assignment.app_key_vault_secrets_user,
    # Entra ID role assignments are eventually consistent, so the blob grant is
    # ordered ahead of the container that will use it rather than being raced by
    # the first file write.
    azurerm_role_assignment.app_storage_blob_data_contributor,
    # The app identity holds the sandbox grants (container groups, subnet
    # join, identity assign), and the pull identity's AcrPull. The dispatcher
    # that creates groups runs in a per-org worker, which uses the same app
    # identity, but the web app is the other holder of that identity and can
    # serve the same sandbox tool surface — so order the grants ahead of it
    # too. Counted resources with count 0 are no-ops in the docker case.
    azurerm_role_assignment.app_sandbox_operator,
    azurerm_role_assignment.app_sandbox_subnet_join,
    azurerm_role_assignment.app_sandbox_identity_assign,
    azurerm_role_assignment.sandbox_pull_acr_pull,
    # The private endpoint and its DNS link must exist before the first file
    # operation, or the app's first store_user_file resolves the public
    # <account>.blob.core.windows.net name and fails while /api/health is green.
    # The link resource is counted; when create_blob_private_dns_zone = false the
    # caller's existing zone is expected to be linked already.
    azurerm_private_endpoint.blob,
    azurerm_private_dns_zone_virtual_network_link.blob,
  ]

  lifecycle {
    # Container Apps rejects a spec with two environment variables of the same
    # name. The plain variables and the secret-backed ones come from separate
    # maps (extra_environment and extra_secrets), so an operator can easily put
    # the same key in both — catch it here rather than mid-rollout.
    precondition {
      condition     = length(setintersection(keys(local.web_environment), local.web_secret_keys)) == 0
      error_message = "An environment variable name appears in both extra_environment and extra_secrets. Use extra_secrets for anything sensitive and extra_environment for the rest."
    }
  }
}

###############################################################################
# Daemon controller
#
# The one place where a missing argument is load-bearing. This app has NO
# `ingress` block, which is how Container Apps disables ingress — there is no
# `enabled = false` flag. A no-ingress app with no scale rule scales to zero and
# then can never come back, because nothing can send it traffic. So min_replicas
# and max_replicas are both pinned to 1. The controller is a singleton anyway:
# two controllers would duplicate worker supervision.
#
# Pinning to exactly 1 also means Azure bills it at the cheaper idle CPU rate.
###############################################################################

resource "azurerm_container_app" "controller" {
  name                         = local.controller_app_name
  resource_group_name          = local.resource_group.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  tags = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.controller.id]
  }

  registry {
    server   = local.container_registry_login_server
    identity = azurerm_user_assigned_identity.controller.id
  }

  dynamic "secret" {
    for_each = local.application_secret_keys

    content {
      name                = local.container_secret_name[secret.key]
      identity            = azurerm_user_assigned_identity.controller.id
      key_vault_secret_id = azurerm_key_vault_secret.application[secret.key].versionless_id
    }
  }

  # NOTE: no `ingress` block. That is the ingress-disable switch.

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "lucent-controller"
      image  = var.image
      cpu    = var.controller_cpu
      memory = var.controller_memory

      # The image's default entrypoint is `lucent` (the web server). The
      # controller is the separate console script.
      command = ["lucent-daemon"]

      dynamic "env" {
        for_each = local.controller_environment

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.application_secret_keys

        content {
          name        = env.key
          secret_name = local.container_secret_name[env.key]
        }
      }

      env {
        name  = "LUCENT_DAEMON_ROLES"
        value = "all"
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.controller_acr_pull,
    azurerm_role_assignment.controller_key_vault_secrets_user,
    azurerm_role_assignment.controller_job_operator,
  ]

  lifecycle {
    precondition {
      condition     = length(setintersection(keys(local.controller_environment), local.application_secret_keys)) == 0
      error_message = "An environment variable name appears in both extra_environment and extra_secrets. Use extra_secrets for anything sensitive and extra_environment for the rest."
    }
  }
}

###############################################################################
# Workers job
#
# Manual trigger: the controller starts an execution per organization through the
# Container Apps start API, holding only the custom role defined in rbac.tf.
#
# The Azure worker launcher this job depends on lives in the application:
# daemon/launcher/azure_launcher.py implements the `azure` backend and
# get_launcher dispatches to it on LUCENT_WORKER_LAUNCHER=azure, which the
# controller is configured with below. It starts executions through the same ARM
# `.../jobs/<name>/start` action the custom role grants, using
# DefaultAzureCredential. So the controller CAN start this job — there is nothing
# left for Terraform to wire up beyond the job, the RBAC and the environment
# variables, all of which are here.
#
# One thing Terraform genuinely cannot do: azurerm exposes no way to start an
# execution, and starting is an ARM action, so it is the launcher's job, not
# Terraform's.
#
# The ``io.lucent.worker.managed`` tag below is the one static identifier Azure
# can express for this job. It is set HERE rather than by the controller on
# purpose: the controller's custom role (rbac.tf) grants no
# ``Microsoft.App/jobs/write``, so it cannot PATCH the job's tags, and it should
# not need to -- the tag describes the deployment, not a running controller. The
# launcher only reads it back (``jobs/read``) to confirm the job is one it
# manages. Per-execution identity is carried in the execution template's forced
# binding env; executions cannot be tagged at all.
###############################################################################

resource "azurerm_container_app_job" "workers" {
  name                         = local.workers_job_name
  resource_group_name          = local.resource_group.name
  location                     = local.resource_group.location
  container_app_environment_id = azurerm_container_app_environment.this.id

  replica_timeout_in_seconds = var.worker_replica_timeout_seconds
  replica_retry_limit        = var.worker_replica_retry_limit

  tags = merge(local.tags, { "io.lucent.worker.managed" = "true" })

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = local.container_registry_login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  dynamic "secret" {
    for_each = local.application_secret_keys

    content {
      name                = local.container_secret_name[secret.key]
      identity            = azurerm_user_assigned_identity.app.id
      key_vault_secret_id = azurerm_key_vault_secret.application[secret.key].versionless_id
    }
  }

  manual_trigger_config {
    parallelism              = var.worker_job_parallelism
    replica_completion_count = 1
  }

  template {
    container {
      name    = "lucent-worker"
      image   = var.image
      cpu     = var.worker_cpu
      memory  = var.worker_memory
      command = ["lucent-daemon"]

      dynamic "env" {
        for_each = local.daemon_environment

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.application_secret_keys

        content {
          name        = env.key
          secret_name = local.container_secret_name[env.key]
        }
      }

      # Defaults only. The real per-organization values are set by the launcher
      # when it starts an execution, and the start body REPLACES the whole
      # execution template rather than patching it — so the launcher must resend
      # every variable, including the secret references, not just the ones it
      # wants to change.
      env {
        name  = "LUCENT_DAEMON_ROLES"
        value = "dispatcher"
      }

      env {
        name  = "LUCENT_DAEMON_WORKER"
        value = "1"
      }
    }
  }

  # Same identity as the web app, so it needs the same pulls and reads — and, like
  # the web app, it deliberately does NOT hold the raw database passwords. The
  # depends_on makes the role assignments land before the first execution resolves
  # its Key Vault references.
  depends_on = [
    azurerm_role_assignment.app_acr_pull,
    azurerm_role_assignment.app_key_vault_secrets_user,
    # Entra ID role assignments are eventually consistent, so the blob grant is
    # ordered ahead of the container that will use it rather than being raced by
    # the first file write.
    azurerm_role_assignment.app_storage_blob_data_contributor,
    # The dispatcher running in this worker is the process that creates task
    # sandbox container groups (daemon/sandbox/lifecycle.py). Its grants and
    # the pull identity's AcrPull must be in place before the first worker
    # execution tries to create a group; without this the first group can race
    # role-assignment propagation. Count-0 resources are no-ops for docker.
    azurerm_role_assignment.app_sandbox_operator,
    azurerm_role_assignment.app_sandbox_subnet_join,
    azurerm_role_assignment.app_sandbox_identity_assign,
    azurerm_role_assignment.sandbox_pull_acr_pull,
  ]
}
