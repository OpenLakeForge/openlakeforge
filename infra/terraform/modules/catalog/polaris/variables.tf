variable "namespace" {
  description = "Kubernetes namespace where Polaris is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "polaris"
}

variable "chart_repository" {
  description = "Polaris Helm chart repository."
  type        = string
  default     = "https://downloads.apache.org/polaris/helm-chart"
}

variable "chart_version" {
  description = "Polaris Helm chart version."
  type        = string
  default     = "1.8.0"
}

variable "chart_package_path" {
  description = "Optional local Polaris Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "stage_catalogs" {
  description = "Stage-owned Polaris catalogs and the Secrets olf's bootstrap writes their principals' credentials to. Empty keeps the legacy single-catalog interface for v0.2 migration."
  type = map(object({
    catalog_name                     = string
    trino_credentials_secret_name    = string
    floe_credentials_secret_name     = string
    deployer_credentials_secret_name = string
  }))
  default = {}
}

variable "catalog_name" {
  description = "Polaris catalog name."
  type        = string
}

variable "storage_contract" {
  description = "S3-compatible storage contract consumed by Polaris."
  type = object({
    endpoint                = string
    region                  = string
    bucket_name             = string
    path_style_access       = bool
    credentials_secret_name = string
    access_key_id_key       = string
    secret_access_key_key   = string
    bronze_bucket_name      = optional(string)
    silver_bucket_name      = optional(string)
    gold_bucket_name        = optional(string)
    provider                = optional(string)
    implementation          = optional(string)
    auth_mode               = optional(string)
    ssl_mode                = optional(string)
    ingress_mode            = optional(string)
  })
}

variable "postgresql_contract" {
  description = "Metadata PostgreSQL contract providing the Polaris relational JDBC Secret reference."
  type = object({
    polaris_credentials_secret_name = string
  })
}

variable "bootstrap_secret_name" {
  description = "Kubernetes Secret containing Polaris root bootstrap credentials."
  type        = string
  default     = "polaris-bootstrap-credentials"
}

variable "trino_credentials_secret_name" {
  description = "Kubernetes Secret olf's bootstrap writes with Trino OAuth credentials."
  type        = string
  default     = "polaris-trino-creds"
}

variable "floe_credentials_secret_name" {
  description = "Kubernetes Secret olf's bootstrap writes with Floe OAuth credentials."
  type        = string
  default     = "polaris-floe-creds"
}

variable "bootstrap_job_image" {
  description = "Image of the Job that creates the token-broker key and the root credentials when missing."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "metastore_bootstrap_job_image" {
  description = "Version-matched Polaris Admin Tool image used to initialize the relational metastore."
  type        = string
  default     = "apache/polaris-admin-tool:1.8.0@sha256:34dde19bb321aef5925a14ba90b69e26e982a5dcbda76a9d0cfc1aac1534ba4e"
}

variable "om_credentials_secret_name" {
  description = "Kubernetes Secret olf's bootstrap writes with OpenMetadata OAuth credentials."
  type        = string
  default     = "polaris-om-creds"
}

variable "deployer_credentials_secret_name" {
  description = "Kubernetes Secret olf's bootstrap writes with deployer OAuth credentials."
  type        = string
  default     = "polaris-deployer-creds"
}

variable "person_access" {
  description = "Accept people's Dex tokens (ADR 0019): issuer_url, the client_id tokens are issued to, principal_name (the Dex connector id every token names), role_mapping (canonical role -> claim values of the groups claim), data_access (release/identity-roles.yaml) and internal_url, the issuer's in-cluster base URL where Polaris fetches keys (issuer_url is compared with the token's iss as a string). null keeps internal authentication only."
  type = object({
    issuer_url     = string
    client_id      = string
    principal_name = string
    role_mapping   = map(list(string))
    data_access    = map(map(map(string)))
    internal_url   = string
  })
  default = null

  # The mapper matches each claim literally through character classes, which
  # cannot hold these four characters without a backslash (person_access.tf).
  validation {
    condition = var.person_access == null || alltrue([
      for claim in concat(flatten(values(var.person_access.role_mapping)), [var.person_access.principal_name]) :
      !can(regex("[][\\\\^]", claim))
    ])
    error_message = "A role_mapping claim or the connector id contains [, ], \\ or ^, which Polaris's role mapper cannot match literally."
  }
}
