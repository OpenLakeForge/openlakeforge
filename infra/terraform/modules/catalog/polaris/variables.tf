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
  default     = "1.4.1"
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

variable "workload_namespaces" {
  description = "Namespaces the bootstrap job replicates principal credential Secrets into, for workloads that run outside this module's namespace."
  type        = list(string)
  default     = []
}

variable "replicate_credentials" {
  description = "Whether every principal Secret is copied to every workload namespace. Stage isolation uses direct stage-targeted Secrets instead."
  type        = bool
  default     = true
}

variable "stage_catalogs" {
  description = "Stage-owned Polaris catalogs, principals, and target namespaces. Empty keeps the legacy single-catalog interface for v0.2 migration."
  type = map(object({
    namespace                        = string
    catalog_name                     = string
    bronze_bucket_name               = string
    silver_bucket_name               = string
    gold_bucket_name                 = string
    trino_principal_name             = string
    trino_credentials_secret_name    = string
    floe_principal_name              = string
    floe_credentials_secret_name     = string
    deployer_principal_name          = string
    deployer_credentials_secret_name = string
  }))
  default = {}
}

variable "catalog_name" {
  description = "Polaris catalog name."
  type        = string
}

variable "principal_name" {
  description = "Polaris service principal name for Trino."
  type        = string
}

variable "principal_role" {
  description = "Polaris principal role granted to Trino."
  type        = string
}

variable "catalog_role" {
  description = "Polaris catalog role granted to the Trino principal role."
  type        = string
}

variable "floe_principal_name" {
  description = "Polaris service principal name for Floe."
  type        = string
  default     = "floe"
}

variable "floe_principal_role" {
  description = "Polaris principal role granted to Floe."
  type        = string
  default     = "data-writer"
}

variable "floe_catalog_role" {
  description = "Polaris catalog role granted to the Floe principal role."
  type        = string
  default     = "catalog-writer"
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
    # Set by a storage adapter whose STS can vend table-scoped credentials.
    sts_role_arn = optional(string)
    sts_endpoint = optional(string)
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
  description = "Kubernetes Secret written by the bootstrap job with Trino OAuth credentials."
  type        = string
  default     = "polaris-trino-creds"
}

variable "floe_credentials_secret_name" {
  description = "Kubernetes Secret written by the bootstrap job with Floe OAuth credentials."
  type        = string
  default     = "polaris-floe-creds"
}

variable "bootstrap_job_image" {
  description = "Image used by the Polaris bootstrap Kubernetes Job."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "metastore_bootstrap_job_image" {
  description = "Version-matched Polaris Admin Tool image used to initialize the relational metastore."
  type        = string
  default     = "apache/polaris-admin-tool:1.4.0@sha256:7ef7557b528964e792caeaef3908434bd99c7d2f994caa654da1d77c6b428a80"
}

variable "om_principal_name" {
  description = "Polaris service principal name for OpenMetadata (read-only catalog discovery)."
  type        = string
  default     = "openmetadata"
}

variable "om_principal_role" {
  description = "Polaris principal role granted to the OpenMetadata principal."
  type        = string
  default     = "data-reader"
}

variable "om_catalog_role" {
  description = "Polaris catalog role granted to the OpenMetadata principal role."
  type        = string
  default     = "catalog-reader"
}

variable "om_credentials_secret_name" {
  description = "Kubernetes Secret written by the bootstrap job with OpenMetadata OAuth credentials."
  type        = string
  default     = "polaris-om-creds"
}

variable "deployer_principal_name" {
  description = "Polaris service principal used by `olf catalog sync-namespaces` in Phase 2."
  type        = string
  default     = "deployer"
}

variable "deployer_principal_role" {
  description = "Polaris principal role granted to the Phase 2 deployer principal."
  type        = string
  default     = "namespace-admin"
}

variable "deployer_catalog_role" {
  description = "Polaris catalog role granted to the deployer principal role."
  type        = string
  default     = "catalog-namespace-admin"
}

variable "deployer_credentials_secret_name" {
  description = "Kubernetes Secret written by the bootstrap job with deployer OAuth credentials."
  type        = string
  default     = "polaris-deployer-creds"
}

variable "bootstrap_revision" {
  description = "Revision of the Polaris bootstrap script used to replace the bootstrap job when the script changes."
  type        = string
  default     = "manual"
}
