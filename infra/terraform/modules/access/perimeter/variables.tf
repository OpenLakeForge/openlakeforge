variable "namespace" {
  description = "Kubernetes namespace of the oauth2-proxy instances, their cookie Secret and the Traefik Service they sit behind."
  type        = string
}

variable "release_name" {
  description = "Name prefix of the Deployments and Services (one per role floor, suffixed with the role)."
  type        = string
  default     = "oauth2-proxy"
}

variable "image" {
  description = "oauth2-proxy image, pinned by digest (release/component-catalog.yaml, oauth2_proxy)."
  type        = string
  default     = "quay.io/oauth2-proxy/oauth2-proxy:v7.15.5@sha256:8498b0d0ef0a7b29686414000a08aee467f02d0299c9ed1e006a8f33fc017916"
}

variable "bootstrap_job_image" {
  description = "Image with kubectl, used by the Job that creates the cookie secret (release/component-catalog.yaml, k8s_bootstrap)."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "base_domain" {
  description = "Deployment Profile spec.access.base_domain. The session cookie is set on .<base_domain> so one login covers every route."
  type        = string
}

variable "callback_host" {
  description = "Host that receives every login callback (served by the proxy, not behind it). One host keeps the issuer client to one redirect URI."
  type        = string
}

variable "portal_url" {
  description = "Where the role-restricted page sends the user back to."
  type        = string
}

variable "ca_configmap_name" {
  description = "ConfigMap in `namespace` whose ca.crt signs the issuer's certificate (modules/access/in-cluster-resolution). Null when the issuer's certificate chains to a public root."
  type        = string
  default     = null
}

variable "identity_roles" {
  description = "release/identity-roles.yaml: `precedence` (highest role first) and `grants` (route service name to role to in-service role). The only place route grants live."
  type = object({
    precedence = list(string)
    grants     = map(map(string))
  })
}

variable "role_mapping" {
  description = "identity.oidc role_mapping: canonical role to the claim values that carry it."
  type        = map(list(string))
}

variable "role_claim" {
  description = "identity.oidc role_claim: the token claim holding the role values."
  type        = string
}

variable "issuer_url" {
  description = "identity.oidc issuer_url."
  type        = string
}

variable "client_id" {
  description = "identity.oidc clients.perimeter.client_id."
  type        = string
}

variable "client_secret_ref" {
  description = "identity.oidc clients.perimeter.secret_ref: the Secret and key in `namespace` holding the client secret."
  type = object({
    name = string
    key  = string
  })
}

variable "protected_refs" {
  description = "Contract refs of the routes behind the perimeter. Each needs a grant row in identity_roles, except the portal, which any recognised role may open."
  type        = list(string)
}
