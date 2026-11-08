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

variable "route_tls" {
  description = "Whether routes terminate TLS on the websecure entrypoint. False serves them over plain HTTP on web (spec.access.tls none): no certificate, no tls block."
  type        = bool
  default     = true
}

variable "trusted_proxies" {
  description = "CIDRs of the upstream proxy whose X-Forwarded-Proto and X-Forwarded-Host the web entrypoint trusts (spec.access.tls external: the customer's edge address range). Empty trusts no peer, and Traefik discards forwarded headers from every client."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.trusted_proxies : can(cidrhost(cidr, 0))])
    error_message = "trusted_proxies entries must be CIDR blocks such as 10.0.0.0/8."
  }
}

variable "cluster_issuer_name" {
  description = "cert-manager ClusterIssuer that signs the one wildcard route certificate. Null when route_tls is false."
  type        = string
  default     = null
}

variable "backend_transports" {
  description = "Transports for backends that speak HTTPS, keyed by name: the Secret whose ca.crt signed the backend and the server name to verify. A Service selects one with the traefik.ingress.kubernetes.io/service.serverstransport annotation (<name>@file)."
  type = map(object({
    ca_secret   = string
    server_name = string
  }))
  default = {}
}

variable "test_edge" {
  description = "End-to-end fixture for spec.access.tls external: a second Traefik from the same chart, standing in for the customer's HTTPS edge. It terminates TLS for base_domain with a certificate from cluster_issuer_name and forwards plain HTTP here. Null (always, outside the e2e) deploys nothing."
  type = object({
    base_domain         = string
    cluster_issuer_name = string
  })
  default = null
}
