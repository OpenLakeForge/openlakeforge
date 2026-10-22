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
  description = "openlakeforge-portal image, pinned by digest (release/component-catalog.yaml, portal)."
  type        = string
  default     = "ghcr.io/openlakeforge/openlakeforge-portal:0.2.0@sha256:516cef1b03421f8f407899e534d18374233d5391fda841a7775d5b29666c2eca"
}

variable "links" {
  description = "Routes the landing page links to, keyed by contract ref: the group heading (stage name; refs outside stage/ list under Shared), the service name and its URL."
  type = map(object({
    stage = string
    name  = string
    url   = string
  }))
}

variable "account_url" {
  description = "Where admins manage members and teams, listed as the portal's Admin link. Set only when the issuer has an account page of its own (GitHub); Dex has none. null shows no link."
  type        = string
  default     = null
}
