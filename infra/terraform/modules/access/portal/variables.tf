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
  nullable    = false # a caller passing null (the local root's unset portal_image) gets the pin below
  default     = "ghcr.io/openlakeforge/openlakeforge-portal:0.7.6@sha256:b5f43c15e9fb1f1e1c5403e4c04dac879d9494be57f90897ed8bc81410f3e856"
}

variable "links" {
  description = "Routes the landing page links to, keyed by contract ref: the group heading (stage name; refs outside stage/ list under Shared), the service name and its URL."
  type = map(object({
    stage = string
    name  = string
    url   = string
  }))
}

variable "sign_out_url" {
  description = "The identity perimeter's sign-out address, listed as the portal's Sign out button. null (authentication off) shows no button."
  type        = string
  default     = null
}

variable "account_url" {
  description = "Where admins manage members and teams, listed as the portal's Admin link. Set only when the issuer has an account page of its own (GitHub); Dex has none. null shows no link."
  type        = string
  default     = null
}
