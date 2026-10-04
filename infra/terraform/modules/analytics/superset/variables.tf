variable "namespace" {
  description = "Kubernetes namespace where Superset is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "superset"
}

variable "chart_repository" {
  description = "Superset Helm chart repository."
  type        = string
  default     = "https://apache.github.io/superset/"
}

variable "chart_version" {
  description = "Superset Helm chart version."
  type        = string
  default     = "0.15.5"
}

variable "chart_package_path" {
  description = "Optional local Superset Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "image_repository" {
  description = "Superset image repository with OpenLakeForge runtime drivers."
  type        = string
  default     = "ghcr.io/openlakeforge/superset"
}

variable "image_tag" {
  description = "Superset image tag."
  type        = string
  default     = "local"
}

variable "image_pull_policy" {
  description = "Superset image pull policy."
  type        = string
  default     = "Never"
}

variable "admin_username" {
  description = "Local Superset admin username."
  type        = string
  default     = "admin"
}

variable "admin_password" {
  description = "Local Superset admin password."
  type        = string
  default     = "admin"
  sensitive   = true
}

variable "admin_email" {
  description = "Local Superset admin email."
  type        = string
  default     = "admin@openlakeforge.local"
}

variable "http_port" {
  description = "Superset HTTP service port."
  type        = number
  default     = 8088
}

variable "postgresql_contract" {
  description = "Metadata PostgreSQL contract consumed by Superset."
  type = object({
    host                             = string
    port                             = number
    superset_db_name                 = string
    superset_db_user                 = string
    superset_credentials_secret_name = string
    provider                         = optional(string)
    implementation                   = optional(string)
    auth_mode                        = optional(string)
    ssl_mode                         = optional(string)
    endpoint                         = optional(string)
  })
}

variable "postgresql_ssl_mode" {
  description = "PostgreSQL sslmode used by Superset metadata connections."
  type        = string
  default     = "disable"
}
