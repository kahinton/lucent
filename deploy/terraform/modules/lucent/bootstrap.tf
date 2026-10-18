###############################################################################
# Database provisioning job
#
# THE HONEST SUMMARY OF WHAT TERRAFORM CAN AND CANNOT DO HERE
#
# Can: create the server, the database, the azure.extensions allowlist, the
# Entra administrator, and the network. That is where Terraform stops.
#
# Cannot: create roles, run CREATE EXTENSION, run ALTER DATABASE ... SET, or run
# ALTER DEFAULT PRIVILEGES. There is no first-class azurerm resource for any of
# them and there is no way to avoid the need:
#
#   * the `lucent_daemon` role is created only by docker/postgres-init.sh, never
#     by a migration — the migrations merely GRANT to it behind IF EXISTS
#     guards, so on a fresh server every one of those grants silently no-ops;
#   * CREATE EXTENSION requires azure_pg_admin; allowlisting the extension in
#     azure.extensions is necessary but not sufficient;
#   * ALTER DEFAULT PRIVILEGES FOR ROLE lucent requires actually being (or being
#     a member of) that role.
#
# A null_resource with a local-exec psql would therefore not work anyway: this
# server has NO public endpoint, so an operator workstation cannot reach it. The
# bootstrap has to run inside the VNet, which makes a Container Apps job the
# right vehicle rather than a fallback. Terraform creates the job; the operator
# starts it once (see the README) or sets trigger_provisioning_job_after_apply.
#
# The SQL is delivered as base64-encoded environment variables rather than as
# heredocs inside the command, so the psql quoting stays sane. The role passwords
# are NOT passed on the psql command line: psql would put a `-v name=password`
# value into the process argument list, where anything that can read /proc in the
# container can see it. Instead the SQL file starts by pulling each password out
# of the environment with psql's own `\getenv` meta-command — the same
# Container Apps secret-backed environment variables the job already has — and
# then interpolates it with `:'name'`. The password never appears in argv. (This
# is why bootstrap_image must ship psql 15 or later: `\getenv` is new in 15.)
###############################################################################

locals {
  # Runs as the server administrator. Creates the extensions, the database
  # settings, and the two application roles, then grants each role exactly what
  # it needs on schema public — without transferring ownership, which would need
  # role membership the administrator does not have.
  #
  # The two \getenv lines bind psql variables from the job's environment so the
  # ALTER ROLE statements below can interpolate the passwords with :'...' without
  # any password being passed on the command line.
  provisioning_sql = <<-SQL
    \getenv app_password APP_DB_PASSWORD
    \getenv daemon_password DAEMON_DB_PASSWORD

    ${join("\n", [for ext in var.postgres_extensions : "CREATE EXTENSION IF NOT EXISTS \"${lower(ext)}\";"])}

    ALTER DATABASE "${var.postgres_database_name}"
      SET pg_trgm.similarity_threshold = ${var.postgres_similarity_threshold};

    DO $$
    BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${var.postgres_app_role}') THEN
        CREATE ROLE ${var.postgres_app_role} WITH LOGIN;
      END IF;
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${var.postgres_daemon_role}') THEN
        CREATE ROLE ${var.postgres_daemon_role} WITH LOGIN;
      END IF;
    END
    $$;

    ALTER ROLE ${var.postgres_app_role} WITH LOGIN PASSWORD :'app_password';
    ALTER ROLE ${var.postgres_daemon_role} WITH LOGIN PASSWORD :'daemon_password';

    GRANT CONNECT ON DATABASE "${var.postgres_database_name}" TO ${var.postgres_app_role};
    GRANT CONNECT ON DATABASE "${var.postgres_database_name}" TO ${var.postgres_daemon_role};

    -- The application role needs CREATE on schema public to run its own
    -- migrations, and the daemon role needs USAGE on it. Both are granted by the
    -- administrator rather than by transferring schema ownership: on PostgreSQL
    -- 15+ schema public is owned by the implicit pg_database_owner role, which
    -- the server administrator already is, so the grants succeed without any
    -- role-membership dance.
    GRANT USAGE, CREATE ON SCHEMA public TO ${var.postgres_app_role};
    GRANT USAGE ON SCHEMA public TO ${var.postgres_daemon_role};
  SQL

  # Runs as the application role, because ALTER DEFAULT PRIVILEGES applies to the
  # current role unless FOR ROLE says otherwise — and only that role's own
  # defaults matter, since it is the role that creates the tables.
  # These are the same grants docker/postgres-init.sh relies on.
  app_privilege_sql = <<-SQL
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${var.postgres_daemon_role};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE, SELECT ON SEQUENCES TO ${var.postgres_daemon_role};

    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ${var.postgres_daemon_role};
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ${var.postgres_daemon_role};
  SQL

  # The SQL travels base64-encoded and is decoded to a file, then executed with
  # `psql -f`. The encoding keeps a multi-line script out of a plain environment
  # variable, where newlines and `$$` dollar-quoting are easy to mangle. The
  # passwords are read inside the SQL file with `\getenv` (see provisioning_sql),
  # so the psql invocations below carry no password on their command line.
  provisioning_script = <<-SH
    set -eu
    printf '%s' "$PROVISION_SQL_B64" | base64 -d > /tmp/provision.sql
    printf '%s' "$APP_PRIVILEGE_SQL_B64" | base64 -d > /tmp/app-privileges.sql

    echo "[provision] extensions, settings and roles, as the server administrator"
    psql -v ON_ERROR_STOP=1 -f /tmp/provision.sql

    echo "[provision] default privileges, as the application role"
    PGUSER="$APP_DB_ROLE" PGPASSWORD="$APP_DB_PASSWORD" \
      psql -v ON_ERROR_STOP=1 -f /tmp/app-privileges.sql

    echo "[provision] done -- the application schema itself is created by the app's migrations on first boot"
  SH
}

###############################################################################
# Passwords the provisioning job needs in raw form.
#
# These are separate Key Vault secrets from DATABASE_URL/DAEMON_DATABASE_URL, and
# they are read only by the provisioner identity that the provisioning and
# bootstrap jobs run as — not by the app or controller identities. That is
# enforced by the per-secret role assignment in rbac.tf, not merely by which
# container references them: the web app and the controller have no Key Vault
# read on these secrets at all, so neither the internet-facing app nor the
# controller can read the administrator credential even out of band.
###############################################################################

resource "azurerm_key_vault_secret" "postgres_admin_password" {
  name         = "POSTGRES-ADMIN-PASSWORD"
  value        = local.postgres_admin_password
  key_vault_id = azurerm_key_vault.this.id
  tags         = local.tags
}

resource "azurerm_key_vault_secret" "postgres_app_password" {
  name         = "POSTGRES-APP-PASSWORD"
  value        = local.postgres_app_password
  key_vault_id = azurerm_key_vault.this.id
  tags         = local.tags
}

resource "azurerm_key_vault_secret" "postgres_daemon_password" {
  name         = "POSTGRES-DAEMON-PASSWORD"
  value        = local.postgres_daemon_password
  key_vault_id = azurerm_key_vault.this.id
  tags         = local.tags
}

# Only created when the application bootstrap is switched on. There is no
# Terraform resource for the first user, so the password has to come from
# somewhere: it is supplied as a variable and stored here so the job can read it
# as a Key Vault-backed secret reference rather than a plain environment value.
resource "azurerm_key_vault_secret" "bootstrap_admin_password" {
  count = var.enable_app_bootstrap ? 1 : 0

  name         = "LUCENT-BOOTSTRAP-ADMIN-PASSWORD"
  value        = var.bootstrap_admin_password
  key_vault_id = azurerm_key_vault.this.id
  tags         = local.tags
}

resource "azurerm_container_app_job" "provisioning" {
  count = var.enable_provisioning_job ? 1 : 0

  name                         = local.provisioning_job_name
  resource_group_name          = local.resource_group.name
  location                     = local.resource_group.location
  container_app_environment_id = azurerm_container_app_environment.this.id

  replica_timeout_in_seconds = 1800
  # Provisioning is idempotent, but a partial failure is easier to diagnose from
  # a clean re-run than from a retry loop that hides the first error.
  replica_retry_limit = 0

  tags = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.provisioner.id]
  }

  registry {
    server   = local.container_registry_login_server
    identity = azurerm_user_assigned_identity.provisioner.id
  }

  # Container Apps secret names are kept short and lower-case deliberately.
  secret {
    name                = "pg-admin-pw"
    identity            = azurerm_user_assigned_identity.provisioner.id
    key_vault_secret_id = azurerm_key_vault_secret.postgres_admin_password.versionless_id
  }

  secret {
    name                = "pg-app-pw"
    identity            = azurerm_user_assigned_identity.provisioner.id
    key_vault_secret_id = azurerm_key_vault_secret.postgres_app_password.versionless_id
  }

  secret {
    name                = "pg-daemon-pw"
    identity            = azurerm_user_assigned_identity.provisioner.id
    key_vault_secret_id = azurerm_key_vault_secret.postgres_daemon_password.versionless_id
  }

  manual_trigger_config {
    parallelism = 1
  }

  template {
    container {
      name    = "provision"
      image   = var.bootstrap_image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["/bin/sh", "-c"]
      args    = [local.provisioning_script]

      env {
        name  = "PGHOST"
        value = azurerm_postgresql_flexible_server.this.fqdn
      }

      env {
        name  = "PGPORT"
        value = "5432"
      }

      env {
        name  = "PGDATABASE"
        value = var.postgres_database_name
      }

      env {
        name  = "PGUSER"
        value = var.postgres_admin_login
      }

      env {
        name  = "PGSSLMODE"
        value = "require"
      }

      env {
        name        = "PGPASSWORD"
        secret_name = "pg-admin-pw"
      }

      env {
        name  = "APP_DB_ROLE"
        value = var.postgres_app_role
      }

      env {
        name        = "APP_DB_PASSWORD"
        secret_name = "pg-app-pw"
      }

      env {
        name        = "DAEMON_DB_PASSWORD"
        secret_name = "pg-daemon-pw"
      }

      env {
        name  = "PROVISION_SQL_B64"
        value = base64encode(local.provisioning_sql)
      }

      env {
        name  = "APP_PRIVILEGE_SQL_B64"
        value = base64encode(local.app_privilege_sql)
      }
    }
  }

  depends_on = [
    azurerm_postgresql_flexible_server_database.this,
    azurerm_postgresql_flexible_server_configuration.azure_extensions,
    azurerm_role_assignment.provisioner_key_vault_secrets_user,
    azurerm_role_assignment.provisioner_acr_pull,
  ]
}

###############################################################################
# Application bootstrap job  —  gated, and off by default
#
# This closes design-doc §5.1 step 3, and it depends on application code that
# landed alongside this module:
#
#   * `python -m lucent.migrate` applies the schema and exits. It reuses the same
#     runner the server uses, and the runner now takes a session-level advisory
#     lock, so §4.2's "migrations must not race" is addressed for this path. It
#     also runs the prerequisite step first, which reports extensions and the
#     daemon role as already `present` once the provisioning job above has made
#     them — the two steps are complementary, not redundant.
#   * `python -m lucent.bootstrap` provisions the first organisation, owner and
#     API key non-interactively and idempotently, and is careful about the trap
#     that an *enabled* model is not a *usable* one (usage is clearance-driven
#     and default-deny).
#
# It is off by default, for two reasons rather than one. It creates a real owner
# account, so it needs an email and a password; and that password is then present
# in Terraform state and in the job's spec-derived resources. Turn it on when you
# want a repeatable provisioned instance, and rotate the password after first
# sign-in.
#
# Ordering matters and cannot be expressed by Terraform: this job needs the
# application roles, which the provisioning job creates. Provision first, then
# bootstrap. `enable_app_bootstrap` without `enable_provisioning_job` is almost
# certainly a mistake, so it is rejected below.
###############################################################################

resource "azurerm_container_app_job" "app_bootstrap" {
  count = var.enable_app_bootstrap ? 1 : 0

  name                         = "${local.base}-bootstrap"
  resource_group_name          = local.resource_group.name
  location                     = local.resource_group.location
  container_app_environment_id = azurerm_container_app_environment.this.id

  replica_timeout_in_seconds = 1800
  replica_retry_limit        = 0

  tags = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.provisioner.id]
  }

  registry {
    server   = local.container_registry_login_server
    identity = azurerm_user_assigned_identity.provisioner.id
  }

  dynamic "secret" {
    for_each = local.application_secret_keys

    content {
      name                = local.container_secret_name[secret.key]
      identity            = azurerm_user_assigned_identity.provisioner.id
      key_vault_secret_id = azurerm_key_vault_secret.application[secret.key].versionless_id
    }
  }

  secret {
    name                = "bootstrap-admin-pw"
    identity            = azurerm_user_assigned_identity.provisioner.id
    key_vault_secret_id = azurerm_key_vault_secret.bootstrap_admin_password[0].versionless_id
  }

  # The prerequisite step inside lucent.migrate reads DAEMON_DB_PASSWORD to
  # create (or verify) the lucent_daemon role. Without it the step still runs but
  # reports the role as "missing-no-password" rather than confirming it, so the
  # job would pass while leaving the daemon unable to authenticate.
  secret {
    name                = "pg-daemon-pw"
    identity            = azurerm_user_assigned_identity.provisioner.id
    key_vault_secret_id = azurerm_key_vault_secret.postgres_daemon_password.versionless_id
  }

  manual_trigger_config {
    parallelism = 1
  }

  template {
    container {
      name    = "bootstrap"
      image   = var.image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["/bin/sh", "-c"]
      args = [join("; ", [
        "set -eu",
        "echo '[bootstrap] applying migrations'",
        "python -m lucent.migrate",
        "echo '[bootstrap] provisioning the first organisation, owner and API key'",
        "python -m lucent.bootstrap",
      ])]

      # Dedicated environment, NOT local.web_environment: this job runs as the
      # provisioner identity, and web_environment carries the app identity's
      # AZURE_CLIENT_ID plus the file-storage configuration the provisioner is
      # not granted. See bootstrap_environment in locals.tf.
      dynamic "env" {
        for_each = local.bootstrap_environment

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
        name        = "DAEMON_DB_PASSWORD"
        secret_name = "pg-daemon-pw"
      }

      # The bootstrap CLI is env-driven so the password never has to appear on
      # argv, where any process on the host could read it from the process list.
      env {
        name  = "LUCENT_BOOTSTRAP_EMAIL"
        value = var.bootstrap_admin_email
      }

      env {
        name  = "LUCENT_BOOTSTRAP_DISPLAY_NAME"
        value = var.bootstrap_admin_display_name
      }

      env {
        name        = "LUCENT_BOOTSTRAP_PASSWORD"
        secret_name = "bootstrap-admin-pw"
      }

      dynamic "env" {
        for_each = var.bootstrap_org_name != null ? { LUCENT_BOOTSTRAP_ORG_NAME = var.bootstrap_org_name } : {}

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.bootstrap_model != null ? { LUCENT_BOOTSTRAP_MODEL = var.bootstrap_model } : {}

        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [
    azurerm_container_app_job.provisioning,
    azurerm_role_assignment.provisioner_key_vault_secrets_user,
    azurerm_role_assignment.provisioner_acr_pull,
  ]

  lifecycle {
    # The bootstrap job consumes the roles the provisioning job creates. With
    # provisioning disabled it fails on the first statement that needs them —
    # and the failure is a SQL privilege error, which is a poor way to learn
    # about a configuration mistake.
    precondition {
      condition     = var.enable_provisioning_job
      error_message = "enable_app_bootstrap requires enable_provisioning_job: the bootstrap connects as the application role, which the provisioning job creates."
    }

    precondition {
      condition     = var.bootstrap_admin_email != null && var.bootstrap_admin_password != null
      error_message = "enable_app_bootstrap requires bootstrap_admin_email and bootstrap_admin_password."
    }
  }
}

###############################################################################
# Optional: start the provisioning job during apply
#
# Off by default. Terraform has no native way to run a Container Apps execution —
# starting is an ARM action — so the only option is shelling out to the Azure
# CLI. That makes apply non-hermetic and requires an authenticated az on PATH, so
# it is opt-in. The documented alternative is one command after apply:
#
#   az containerapp job start -g <rg> -n <job> --subscription <sub>
###############################################################################

resource "null_resource" "start_provisioning" {
  count = var.enable_provisioning_job && var.trigger_provisioning_job_after_apply ? 1 : 0

  triggers = {
    job_id = azurerm_container_app_job.provisioning[0].id
  }

  provisioner "local-exec" {
    command = <<-CMD
      az containerapp job start \
        --resource-group "${local.resource_group.name}" \
        --name "${local.provisioning_job_name}" \
        --subscription "${local.subscription_id}"
    CMD
  }

  depends_on = [azurerm_container_app_job.provisioning]
}
