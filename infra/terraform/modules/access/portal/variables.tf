variable "namespace" {
  description = "Kubernetes namespace where the portal is deployed."
  type        = string
}

variable "release_name" {
  description = "Name of the portal Deployment, Service and ConfigMap."
  type        = string
  default     = "portal"
}

variable "image" {
  description = "Static file server image, pinned by digest (release/component-catalog.yaml, portal_static_server)."
  type        = string
  default     = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
}

variable "links" {
  description = "Routes the landing page links to, keyed by contract ref: the group heading (stage name or shared), the service name and its URL."
  type = map(object({
    stage = string
    name  = string
    url   = string
  }))
}
