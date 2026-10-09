variable "namespace" {
  description = "Namespace of the Traefik Service and of the local CA Secret."
  type        = string
}

variable "base_domain" {
  description = "Deployment Profile spec.access.base_domain: this name and everything under it is rewritten to the ingress inside the cluster."
  type        = string
}

variable "ingress_service_name" {
  description = "Traefik Service that terminates TLS for the routes."
  type        = string
  default     = "traefik"
}

variable "ca_secret_name" {
  description = "Secret holding the local CA root in `tls.crt` (modules/access/cert-manager)."
  type        = string
  default     = "local-ca"
}

variable "trust_namespaces" {
  description = "Namespaces that receive the `olf-local-ca` ConfigMap, for pods that call a routed service by its public URL."
  type        = list(string)
}

variable "bootstrap_job_image" {
  description = "Image with kubectl used by the CA publishing Job."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}
