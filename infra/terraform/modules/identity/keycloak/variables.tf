variable "namespace" {
  description = "Kubernetes namespace where Keycloak and its realm Job are deployed."
  type        = string
}

variable "release_name" {
  description = "Name of the Keycloak Deployment and Service."
  type        = string
  default     = "keycloak"
}

variable "image" {
  description = "Keycloak image, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "quay.io/keycloak/keycloak:26.6.4@sha256:0aae0de7fca85525f727d3354df17896092de8bb26ae4c12d89c77e5df8cbce4"
}

variable "config_cli_image" {
  description = "keycloak-config-cli image that applies the realm, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "adorsys/keycloak-config-cli:6.5.1-26.5.5@sha256:0955d98c8a341898b7aa177477edf8a1e90569ae50bbe7598141c1270b773274"
}

variable "hostname" {
  description = "Browser-facing host of the issuer (auth.<base_domain>). Keycloak pins every URL it emits, including the token `iss`, to https://<hostname>."
  type        = string
}

variable "realm_name" {
  description = "Realm holding the OpenLakeForge groups and clients."
  type        = string
  default     = "openlakeforge"
}

variable "role_claim" {
  description = "Token claim that carries the user's canonical roles."
  type        = string
  default     = "groups"
}

variable "roles" {
  description = "Canonical role names (release/identity-roles.yaml precedence). Each becomes a group of the same name."
  type        = list(string)
}

variable "clients" {
  description = "OIDC clients keyed by consumer (perimeter, superset, openmetadata, trino) with the redirect URIs each may return to. The client id is the key."
  type = map(object({
    redirect_uris = list(string)
  }))
}

variable "postgresql_contract" {
  description = "Shared PostgreSQL contract: host, port, and the keycloak entry of `databases` (db_name, db_user, credentials_secret_name)."
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
  description = "Key of Keycloak's entry in postgresql_contract.databases."
  type        = string
  default     = "keycloak"
}
