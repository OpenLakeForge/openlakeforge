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

variable "routes" {
  description = "Routes keyed by contract ref: the host, the wildcard tls_host covering it, the backend Service in its namespace, the path prefix it answers, and the access group whose middlewares gate it (empty: none)."
  type = map(object({
    host         = string
    tls_host     = string
    namespace    = string
    service_name = string
    service_port = number
    path         = optional(string, "/")
    group        = optional(string, "")
  }))
}

variable "group_middlewares" {
  description = "Access group to the Traefik middlewares, in order, that gate its routes (modules/access/perimeter). Every non-empty route group must appear."
  type        = map(list(string))
  default     = {}
}

variable "dynamic_config" {
  description = "Traefik file-provider document defining the middlewares group_middlewares names, or null for none."
  type        = any
  default     = null
}

variable "cluster_issuer_name" {
  description = "cert-manager ClusterIssuer that signs each namespace's wildcard route certificate."
  type        = string
}

variable "backend_transports" {
  description = "Transports for backends that speak HTTPS, keyed by name: the Secret whose ca.crt signed the backend and the server name to verify. A Service selects one with the traefik.ingress.kubernetes.io/service.serverstransport annotation (<name>@file)."
  type = map(object({
    ca_secret   = string
    server_name = string
  }))
  default = {}
}
