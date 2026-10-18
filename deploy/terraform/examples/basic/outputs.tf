output "application_url" {
  description = "Public HTTPS URL of the Lucent web/API app."
  value       = module.lucent.application_url
}

output "container_registry_login_server" {
  description = "Push the application image here before the workloads can start."
  value       = module.lucent.container_registry_login_server
}

output "resource_group_name" {
  description = "Resource group holding the deployment."
  value       = module.lucent.resource_group_name
}

output "key_vault_name" {
  description = "Key Vault holding the application secrets."
  value       = module.lucent.key_vault_name
}

output "postgres_fqdn" {
  description = "Private FQDN of the PostgreSQL server. Reachable only from inside the VNet."
  value       = module.lucent.postgres_fqdn
}

output "web_app_name" {
  description = "Name of the web/API container app."
  value       = module.lucent.web_app_name
}

output "controller_app_name" {
  description = "Name of the daemon controller container app."
  value       = module.lucent.controller_app_name
}

output "workers_job_name" {
  description = "Name of the workers Container Apps job."
  value       = module.lucent.workers_job_name
}

output "next_steps" {
  description = "What still has to be done by hand after apply."
  value       = module.lucent.next_steps
}
