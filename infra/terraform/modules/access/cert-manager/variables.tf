variable "namespace" {
  description = "Kubernetes namespace where cert-manager and the local CA Secret live."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "cert-manager"
}

variable "chart_repository" {
  description = "cert-manager Helm chart repository."
  type        = string
  default     = "https://charts.jetstack.io"
}

variable "chart_version" {
  description = "cert-manager Helm chart version."
  type        = string
  default     = "v1.21.2"
}

variable "chart_package_path" {
  description = "Optional local cert-manager Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "base_domain" {
  description = "Deployment Profile spec.access.base_domain; the probe certificate is issued for probe.<base_domain>."
  type        = string
}
