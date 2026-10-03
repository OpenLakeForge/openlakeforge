variable "namespace" {
  description = "Kubernetes namespace where Traefik is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "traefik"
}

variable "chart_repository" {
  description = "Traefik Helm chart repository."
  type        = string
  default     = "https://traefik.github.io/charts"
}

variable "chart_version" {
  description = "Traefik Helm chart version."
  type        = string
  default     = "41.6.1"
}

variable "chart_package_path" {
  description = "Optional local Traefik Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}
