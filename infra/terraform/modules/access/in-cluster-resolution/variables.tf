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
  default     = "alpine/k8s:1.31.13@sha256:7a319b15cfc99c9aaa27ec9891d6b4ca24632b795908591c7b0ba89b03549e0a"
}

variable "trust_namespace_uids" {
  description = "UIDs of the namespaces in trust_namespaces. A namespace deleted and recreated under the same name changes its UID, which reruns the CA publishing Job into it."
  type        = list(string)
  default     = []
}

variable "rewrite_to_ingress" {
  description = "Whether CoreDNS rewrites base_domain to ingress_service_name. False for spec.access.tls external: the customer's edge answers the public name, and Traefik behind it serves no TLS for a pod to reach."
  type        = bool
  default     = true
}
