variable "namespace" {
  description = "Kubernetes namespace where Dagster is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "dagster"
}

variable "chart_repository" {
  description = "Dagster Helm chart repository."
  type        = string
  default     = "https://dagster-io.github.io/helm"
}

variable "chart_version" {
  description = "Dagster Helm chart version. Keep this aligned with the project-code Dagster Python package."
  type        = string
  default     = "1.13.7"
}

variable "chart_package_path" {
  description = "Optional local Dagster Helm chart package path."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "project_code_image_repository" {
  description = "Project-code image repository used by the Dagster code server and run pods."
  type        = string
  default     = "ghcr.io/openlakeforge/project-code"
}

variable "project_code_image_tag" {
  description = "Project-code image tag used by the Dagster code server and run pods."
  type        = string
  default     = "local"
}

variable "project_code_image_pull_policy" {
  description = "Project-code image pull policy used by the Dagster code server and run pods."
  type        = string
  default     = "IfNotPresent"
}

variable "project_code_image_revision" {
  description = "Project-code image revision used to force Dagster pod rollouts when the tag is reused."
  type        = string
  default     = "manual"
}

variable "kubernetes_log_archive_schedule" {
  description = "Cron schedule for archiving Kubernetes pod logs to the artifact bucket."
  type        = string
  default     = "*/15 * * * *"
}

variable "code_locations" {
  description = "Dagster user-code deployments and Python modules exposing Definitions. A merged location saves pods but a load failure affects every domain; split locations isolate load and restart failures at one pod per domain."
  type = list(object({
    name               = string
    definitions_module = string
  }))
  default = [
    {
      name               = "openlakeforge-dagster"
      definitions_module = "lakehouse_code.definitions"
    },
  ]
}

variable "floe_manifest_base_uri" {
  description = "S3 base URI containing generated domain Floe manifests loaded by Dagster."
  type        = string
}

variable "floe_manifest_access_mode" {
  description = "How Floe runner pods access generated manifests. Use remote for Kubernetes runner pods and local only for same-container local-process execution."
  type        = string
  default     = "remote"

  validation {
    condition     = contains(["remote", "local"], var.floe_manifest_access_mode)
    error_message = "floe_manifest_access_mode must be either remote or local."
  }
}

variable "floe_manifest_revision" {
  description = "Content revision of generated domain Floe manifests used to force Dagster code-server rollouts."
  type        = string
  default     = "manual"
}

variable "artifact_bucket_name" {
  description = "S3-compatible operational artifact bucket used for manifests, logs, reports, and run artifacts."
  type        = string
}

variable "artifact_base_uri" {
  description = "S3 base URI of the operational artifact bucket."
  type        = string
}

variable "floe_report_base_uri" {
  description = "S3 base URI where Floe run reports are written."
  type        = string
}

variable "log_base_uri" {
  description = "S3 base URI where platform logs are archived."
  type        = string
}

variable "run_artifact_base_uri" {
  description = "S3 base URI where tool run artifacts are archived."
  type        = string
}

variable "storage_contract" {
  description = "S3-compatible storage contract consumed by Dagster and run pods."
  type = object({
    endpoint                = optional(string)
    region                  = string
    bucket_name             = string
    bronze_bucket_name      = optional(string)
    silver_bucket_name      = optional(string)
    gold_bucket_name        = optional(string)
    ops_bucket_name         = optional(string)
    path_style_access       = optional(bool)
    credentials_secret_name = optional(string)
    access_key_id_key       = optional(string)
    secret_access_key_key   = optional(string)
    provider                = optional(string)
    implementation          = optional(string)
    auth_mode               = optional(string)
    ssl_mode                = optional(string)
    ingress_mode            = optional(string)
  })
}

variable "catalog_contract" {
  description = "Iceberg catalog contract consumed by Dagster and run pods. Current local uses REST/Polaris; future provider profiles may use Glue."
  type = object({
    rest_uri                     = optional(string)
    token_uri                    = optional(string)
    warehouse                    = optional(string)
    oauth_scope                  = optional(string)
    floe_credentials_secret_name = optional(string)
    floe_client_id_key           = optional(string)
    floe_client_secret_key       = optional(string)
    catalog_type                 = optional(string)
    catalog_provider             = optional(string)
    catalog_name                 = optional(string)
    runtime_profile              = optional(string)
    trino_catalog_name           = optional(string)
    default_warehouse_location   = optional(string)
    glue_catalog_id              = optional(string)
    glue_region                  = optional(string)
    glue_rest_uri                = optional(string)
    glue_rest_warehouse          = optional(string)
    glue_database                = optional(string)
    glue_warehouse_prefix        = optional(string)
    provider                     = optional(string)
    implementation               = optional(string)
    auth_mode                    = optional(string)
    ssl_mode                     = optional(string)
    endpoint                     = optional(string)
    ingress_mode                 = optional(string)
  })
}

variable "query_contract" {
  description = "Query-engine contract consumed by dbt and the runtime. `service_namespace` is where the shared query service runs, which is not this instance's namespace on a stage-aware cluster."
  type = object({
    service_name               = string
    http_port                  = number
    catalog_name               = string
    service_namespace          = optional(string)
    endpoint                   = optional(string)
    runtime_identity_principal = optional(string)
  })
}

variable "postgresql_contract" {
  description = "Metadata PostgreSQL contract consumed by Dagster."
  type = object({
    host                            = string
    port                            = number
    dagster_db_name                 = string
    dagster_db_user                 = string
    dagster_credentials_secret_name = string
    provider                        = optional(string)
    implementation                  = optional(string)
    auth_mode                       = optional(string)
    ssl_mode                        = optional(string)
    endpoint                        = optional(string)
  })
}

variable "postgresql_ssl_mode" {
  description = "PostgreSQL sslmode advertised by the active metadata database contract."
  type        = string
  default     = "disable"
}

variable "governance_contract" {
  description = "Optional OpenMetadata governance contract passed from the environment contract."
  type = object({
    enabled                   = optional(bool, true)
    service_name              = optional(string)
    http_port                 = optional(number)
    ingestion_bot_secret_name = optional(string)
    ingestion_bot_jwt_key     = optional(string)
    provider                  = optional(string)
    implementation            = optional(string)
    auth_mode                 = optional(string)
    endpoint                  = optional(string)
    ingress_mode              = optional(string)
  })
}

variable "service_account_annotations" {
  description = "Optional annotations for Dagster service accounts, used by AWS workload identity integrations."
  type        = map(string)
  default     = {}
}

variable "manage_user_deployments" {
  description = "Whether Terraform owns the Dagster user-code deployments. True is the deprecated single-stage `olf deploy` lifecycle, where the subchart carries user code and the workspace is derived from it. False is the #115 split: `olf project deploy` owns a separate openlakeforge-project release, and this root only publishes a stable workspace endpoint pointing at it."
  type        = bool
  default     = true
}
