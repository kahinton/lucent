###############################################################################
# PostgreSQL Flexible Server
#
# VNet-integrated, with no public endpoint. Two authentication modes are on at
# once, on purpose:
#
#   * Password auth — REQUIRED. Lucent builds its asyncpg pool from a plain
#     DATABASE_URL (src/lucent/db/pool.py) and has no Entra ID token provider,
#     so the application cannot connect with an AAD token today. The passwords
#     are generated here, stored in Key Vault, and never appear in the container
#     spec.
#   * Entra ID auth — for humans: interactive administration and psql access with
#     an Entra identity instead of the shared administrator password. It is NOT
#     what runs the provisioning DDL. bootstrap.tf's provisioning job connects
#     with the server administrator LOGIN (postgres_admin_login) and its
#     password, and on Flexible Server that login is a member of azure_pg_admin —
#     the role CREATE EXTENSION requires. The Entra administrator is also a
#     member of azure_pg_admin, but nothing in the provisioning path uses it.
#
# The design doc's headline is "Entra ID auth". It is worth being precise: that
# is achievable for the administrative path but not for the application path
# without an application change.
#
# What Terraform CANNOT do here, and the provisioning job covers instead:
#   * CREATE ROLE for the daemon (nothing in the migrations creates it; they
#     only GRANT to it behind IF EXISTS guards);
#   * ALTER DATABASE ... SET pg_trgm.similarity_threshold;
#   * ALTER DEFAULT PRIVILEGES FOR ROLE lucent;
#   * CREATE EXTENSION itself — Terraform can only allowlist the extension in
#     azure.extensions, it cannot run the DDL, and the DDL needs azure_pg_admin.
###############################################################################

resource "azurerm_postgresql_flexible_server" "this" {
  name                = local.postgres_server_name
  resource_group_name = local.resource_group.name
  location            = local.resource_group.location

  version    = var.postgres_version
  sku_name   = var.postgres_sku_name
  storage_mb = var.postgres_storage_mb
  zone       = var.postgres_zone

  auto_grow_enabled            = true
  backup_retention_days        = var.postgres_backup_retention_days
  geo_redundant_backup_enabled = false

  administrator_login    = var.postgres_admin_login
  administrator_password = local.postgres_admin_password

  # VNet integration. Azure requires a private DNS zone with a name ending in
  # `.postgres.database.azure.com` and requires public access to be off.
  delegated_subnet_id           = local.postgres_subnet_id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false

  authentication {
    active_directory_auth_enabled = var.entra_admin_object_id != null
    password_auth_enabled         = var.postgres_password_auth_enabled
    tenant_id                     = var.entra_admin_object_id != null ? var.tenant_id : null
  }

  dynamic "high_availability" {
    for_each = var.postgres_high_availability_enabled ? [1] : []

    content {
      mode = "ZoneRedundant"
    }
  }

  tags = local.tags

  lifecycle {
    # The server holds the only copy of the data. A mistaken `terraform destroy`
    # should fail loudly rather than quietly delete a beta's database.
    prevent_destroy = true

    # Azure may relocate the server after a maintenance event; tracking `zone`
    # would then try to move it back and fail.
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "this" {
  name      = var.postgres_database_name
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"

  lifecycle {
    prevent_destroy = true
  }
}

###############################################################################
# Server parameters
###############################################################################

# azure.extensions is the allowlist that lets CREATE EXTENSION work at all. It is
# NOT an argument on the server resource — the provider requires a separate
# configuration resource. The value is a comma-separated list; the provider's own
# examples use uppercase names.
resource "azurerm_postgresql_flexible_server_configuration" "azure_extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = join(",", var.postgres_extensions)
}

# Optional. Lucent's pool opens min 2 / max 10 connections per process, so a
# B1ms server's default of roughly 50 is consumed by three or four processes.
# Overriding is a real lever, but raising it costs memory — hence opt-in.
resource "azurerm_postgresql_flexible_server_configuration" "max_connections" {
  count = var.postgres_max_connections != null ? 1 : 0

  name      = "max_connections"
  server_id = azurerm_postgresql_flexible_server.this.id
  value     = tostring(var.postgres_max_connections)
}

###############################################################################
# Entra ID administrator
#
# Registered so humans can administer the server with Entra identities (psql, the
# portal) rather than the shared administrator password. On Flexible Server a
# member of this role is also a member of azure_pg_admin, so it *could* run
# CREATE EXTENSION — but it is not what the provisioning job does: that connects
# as the server administrator LOGIN (see bootstrap.tf), which is itself a member
# of azure_pg_admin. The Entra administrator's value here is human access and
# leaver management, not the DDL. A group is strongly preferred over an individual
# user so that leavers do not take the deployment's administrative access with
# them.
###############################################################################

resource "azurerm_postgresql_flexible_server_active_directory_administrator" "this" {
  count = var.entra_admin_object_id != null ? 1 : 0

  server_name         = azurerm_postgresql_flexible_server.this.name
  resource_group_name = local.resource_group.name
  tenant_id           = var.tenant_id
  object_id           = var.entra_admin_object_id
  principal_name      = coalesce(var.entra_admin_principal_name, var.entra_admin_object_id)
  principal_type      = var.entra_admin_principal_type
}
