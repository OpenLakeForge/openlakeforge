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
  default     = "ghcr.io/openlakeforge/openlakeforge-portal:0.4.1@sha256:a12027ed534ac293b49dcfef54b5a1f6b02657a8cfb0b6b97cf5ab37721efb88"
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
