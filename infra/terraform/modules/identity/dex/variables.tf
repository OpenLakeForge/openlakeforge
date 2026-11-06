variable "namespace" {
  description = "Kubernetes namespace where Dex and its credentials Job are deployed."
  type        = string
}

variable "release_name" {
  description = "Name of the Dex Deployment and Service."
  type        = string
  default     = "dex"
}

variable "image" {
  description = "Dex image, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "ghcr.io/dexidp/dex:v2.46.0@sha256:933fcd3f523338c847b88ef84a7145fb2fcb7b9917f08418612afeaaa2001777"
}

variable "bootstrap_job_image" {
  description = "Image of the Job that creates the client Secrets when missing, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "hostname" {
  description = "Browser-facing host of the issuer (auth.<base_domain>). Dex's issuer is https://<hostname>."
  type        = string
}

variable "github" {
  description = "spec.identity.github: the organisation, canonical role to team slugs (lowercase), and the operator-created Secret holding the GitHub OAuth App client id and secret (names and keys only)."
  type = object({
    organization = string
    host         = optional(string)
    teams        = map(list(string))
    oauth_app_secret_ref = object({
      name              = string
      client_id_key     = string
      client_secret_key = string
    })
  })
}

variable "clients" {
  description = "OIDC clients keyed by consumer (perimeter, superset, openmetadata, trino) with the redirect URIs each may return to. The client id is the key."
  type = map(object({
    redirect_uris = list(string)
  }))
}

variable "public_clients" {
  description = "Public OIDC clients (no secret), keyed by id. Dex then accepts loopback redirect URIs on any port, so none is registered, and issues refresh tokens without a secret. Dex does not enforce PKCE; the client must send it."
  type        = set(string)
  default     = []
}

variable "trusted_peers" {
  description = "Public client id to the client ids that may request a token whose audience is that public client (Dex `trustedPeers`, scope audience:server:client_id:<id>). Empty trusts no one."
  type        = map(list(string))
  default     = {}

  validation {
    condition     = alltrue([for id, peers in var.trusted_peers : contains(var.public_clients, id) && alltrue([for peer in peers : contains(keys(var.clients), peer)])])
    error_message = "trusted_peers must name a public client and, as its peers, clients defined in `clients`."
  }
}

variable "postgresql_contract" {
  description = "Shared PostgreSQL contract: host, port, and the dex entry of `databases` (db_name, db_user, credentials_secret_name)."
  type = object({
    host = string
    port = number
    databases = map(object({
      db_name                 = string
      db_user                 = string
      credentials_secret_name = string
    }))
  })
}

variable "database_key" {
  description = "Key of Dex's entry in postgresql_contract.databases."
  type        = string
  default     = "dex"
}

variable "scheme" {
  description = "http (spec.access.issuer none, plain HTTP on *.localhost) or https: the scheme of the issuer URL."
  type        = string
  default     = "https"
}

variable "ca_configmap_name" {
  description = "ConfigMap in the namespace with the public platform CA as ca.crt (olf-local-ca). Dex trusts it as rootCA for a GitHub Enterprise `github.host`; unused for github.com."
  type        = string
  default     = null

  validation {
    condition     = var.github.host == null || var.ca_configmap_name != null
    error_message = "A GitHub Enterprise github.host makes Dex read rootCA from the published platform CA, which exists only with spec.access.issuer local-ca. Set the issuer to local-ca or use github.com."
  }
}
