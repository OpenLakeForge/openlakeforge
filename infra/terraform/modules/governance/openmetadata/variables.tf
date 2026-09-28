variable "namespace" {
  description = "Kubernetes namespace where OpenMetadata is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "openmetadata"
}

variable "chart_repository" {
  description = "OpenMetadata Helm chart repository."
  type        = string
  default     = "https://helm.open-metadata.org"
}

variable "chart_version" {
  description = "OpenMetadata Helm chart version."
  type        = string
  default     = "1.12.10"
}

variable "chart_package_path" {
  description = "Optional local OpenMetadata Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file for the openmetadata chart."
  type        = string
}

variable "deps_values_file" {
  description = "Path to the Helm values file for the openmetadata-dependencies chart (OpenSearch only; MySQL and Airflow disabled)."
  type        = string
}

variable "deps_chart_version" {
  description = "openmetadata-dependencies Helm chart version. Should match chart_version."
  type        = string
  default     = "1.12.10"
}

variable "deps_chart_package_path" {
  description = "Optional local openmetadata-dependencies Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "postgresql_contract" {
  description = "Metadata PostgreSQL contract consumed by OpenMetadata."
  type = object({
    host                                 = string
    port                                 = number
    openmetadata_db_name                 = string
    openmetadata_db_user                 = string
    openmetadata_credentials_secret_name = string
    provider                             = optional(string)
    implementation                       = optional(string)
    auth_mode                            = optional(string)
    ssl_mode                             = optional(string)
    endpoint                             = optional(string)
  })
}

variable "postgresql_ssl_mode" {
  description = "PostgreSQL sslmode used by OpenMetadata metadata connections."
  type        = string
  default     = "disable"
}

variable "bootstrap_job_image" {
  description = "Image used by the OpenMetadata bootstrap Kubernetes Job."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "admin_email" {
  description = "OpenMetadata admin user email."
  type        = string
  default     = "admin@open-metadata.org"
}

variable "admin_password" {
  description = "OpenMetadata admin user password."
  type        = string
  default     = "admin"
  sensitive   = true
}

variable "om_http_port" {
  description = "OpenMetadata HTTP service port."
  type        = number
  default     = 8585
}

variable "workload_namespaces" {
  description = "Namespaces the bootstrap job replicates the ingestion-bot Secret into, for workloads that run outside this module's namespace."
  type        = list(string)
  default     = []
}

variable "revoked_namespaces" {
  description = "Namespaces that must not keep a copy of the ingestion-bot Secret. A stage that turns governance off keeps its namespace, so ending replication is not enough: the already-replicated JWT stays valid until the bootstrap job next rotates it, which a topology change does not do."
  type        = list(string)
  default     = []
}

variable "trino_lineage_namespace" {
  description = "OpenLineage namespace the query engine reports itself as. Must match the host:port runtime clients actually connect to, or lineage events land under an unmapped namespace."
  type        = string
  default     = "trino://trino:8080"
}

variable "ingestion_bot_secret_name" {
  description = "Kubernetes Secret written by the bootstrap job containing the ingestion-bot JWT."
  type        = string
  default     = "openmetadata-ingestion-bot"
}

variable "ingestion_bot_jwt_key" {
  description = "Key within the ingestion-bot secret that holds the JWT token."
  type        = string
  default     = "OPENMETADATA_INGESTION_BOT_JWT"
}

variable "catalog_contract" {
  description = "Iceberg catalog contract consumed by OpenMetadata. This module currently uses the REST/Polaris fields for catalog discovery."
  type = object({
    rest_uri                   = optional(string)
    token_uri                  = optional(string)
    warehouse                  = optional(string)
    oauth_scope                = optional(string)
    om_credentials_secret_name = optional(string)
    om_client_id_key           = optional(string)
    om_client_secret_key       = optional(string)
    catalog_type               = optional(string)
    catalog_provider           = optional(string)
    catalog_name               = optional(string)
    runtime_profile            = optional(string)
    trino_catalog_name         = optional(string)
    default_warehouse_location = optional(string)
    glue_catalog_id            = optional(string)
    glue_region                = optional(string)
    glue_rest_uri              = optional(string)
    glue_rest_warehouse        = optional(string)
    provider                   = optional(string)
    implementation             = optional(string)
    auth_mode                  = optional(string)
    ssl_mode                   = optional(string)
    endpoint                   = optional(string)
    ingress_mode               = optional(string)
  })
}

variable "storage_contract" {
  description = "S3-compatible storage contract consumed by OpenMetadata."
  type = object({
    virtual_host_endpoint   = optional(string)
    region                  = string
    bucket_name             = string
    credentials_secret_name = optional(string)
    access_key_id_key       = optional(string)
    secret_access_key_key   = optional(string)
    endpoint                = optional(string)
    path_style_access       = optional(bool)
    provider                = optional(string)
    implementation          = optional(string)
    auth_mode               = optional(string)
    ssl_mode                = optional(string)
    ingress_mode            = optional(string)
  })
}

variable "stages" {
  description = "Governed stages by name. Each gets its own catalog database entity and stage-qualified pipeline and dashboard services; the dashboard pair is null for a stage without analytics."
  type = map(object({
    catalog_database_name  = string
    pipeline_service_name  = string
    dagster_webserver_url  = string
    dashboard_service_name = optional(string)
    superset_url           = optional(string)
  }))
}

variable "canonical_stage" {
  description = "Key of `stages` whose catalog the single Iceberg connection crawls and whose pipeline service receives OpenLineage pipelines."
  type        = string
}

variable "catalog_schema_names" {
  description = "OpenMetadata database schema names to seed before catalog refresh."
  type        = list(string)
  default     = ["silver", "gold"]
}

variable "catalog_refresh_schedule" {
  description = "Cron schedule for the independent OpenMetadata Polaris catalog refresh job."
  type        = string
  default     = "0 * * * *"
}

variable "catalog_refresh_enabled" {
  description = "Whether to run an independent scheduled OpenMetadata catalog refresh for Polaris."
  type        = bool
  default     = true
}

variable "superset_username" {
  description = "Superset API username used by OpenMetadata dashboard metadata ingestion."
  type        = string
  default     = "admin"
}

variable "superset_password" {
  description = "Superset API password used by OpenMetadata dashboard metadata ingestion."
  type        = string
  default     = "admin"
  sensitive   = true
}

variable "superset_auth_provider" {
  description = "Superset API authentication provider used by OpenMetadata dashboard metadata ingestion."
  type        = string
  default     = "db"
}

variable "superset_verify_ssl" {
  description = "Superset SSL verification mode used by OpenMetadata dashboard metadata ingestion."
  type        = string
  default     = "no-ssl"
}

variable "service_account_annotations" {
  description = "Optional annotations for OpenMetadata-related service accounts, used by AWS workload identity integrations."
  type        = map(string)
  default     = {}
}
