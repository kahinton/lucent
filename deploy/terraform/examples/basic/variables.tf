variable "subscription_id" {
  description = "Azure subscription to deploy into. Leave null to use the subscription selected by the Azure CLI or ARM_SUBSCRIPTION_ID."
  type        = string
  default     = null
}

variable "tenant_id" {
  description = "Microsoft Entra ID tenant ID."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "eastus2"
}

variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
  default     = "lucent"
}

variable "environment" {
  description = "Deployment environment label."
  type        = string
  default     = "beta"
}

variable "sandbox_backend" {
  description = "Sandbox backend. \"docker\" (the default) creates no ACI infrastructure and injects no sandbox configuration; \"aci\" provisions the Azure Container Instances path. See deploy/terraform/README.md, \"Task sandboxes on Azure Container Instances\"."
  type        = string
  default     = "docker"

  validation {
    condition     = contains(["docker", "kubernetes", "aci"], var.sandbox_backend)
    error_message = "sandbox_backend must be one of docker, kubernetes or aci."
  }
}

variable "image" {
  description = "Container image for Lucent, for example \"acrlucentbeta.azurecr.io/lucent:0.4.0\". The image must already exist."
  type        = string
}

variable "entra_admin_object_id" {
  description = "Object ID of the Entra ID group or user that becomes the PostgreSQL administrator, for human/interactive access with an Entra identity. Optional, and not required for CREATE EXTENSION: the provisioning job connects as the server administrator login, which Flexible Server places in azure_pg_admin."
  type        = string
  default     = null
}

variable "entra_admin_principal_name" {
  description = "Display name of the principal above."
  type        = string
  default     = null
}

variable "key_vault_allowed_ip_ranges" {
  description = "IP addresses or CIDRs allowed to reach the Key Vault's public endpoint, for example [\"203.0.113.7/32\"] for the machine running Terraform. Only effective together with key_vault_public_network_access_enabled = true: the vault is private by default and, while public access is disabled, IP rules are ignored, so this cannot by itself let a workstation reach the vault."
  type        = list(string)
  default     = []
}

variable "github_token" {
  description = "GitHub PAT with the copilot scope, used by Lucent's default engine. Optional."
  type        = string
  default     = null
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "Anthropic API key, used only by the LangChain engine. Optional."
  type        = string
  default     = null
  sensitive   = true
}

variable "tags" {
  description = "Additional tags for every resource."
  type        = map(string)
  default     = {}
}
