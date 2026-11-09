variable "namespace" {
  description = "Kubernetes namespace where Trino is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "trino"
}

variable "chart_repository" {
  description = "Trino Helm chart repository."
  type        = string
  default     = "https://trinodb.github.io/charts"
}

variable "chart_version" {
  description = "Trino Helm chart version."
  type        = string
  default     = "1.42.2"
}

variable "chart_package_path" {
  description = "Optional local Trino Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "query_sizing" {
  description = "Trino capacity for spec.query.size, rendered by olf from release/query-sizes.yaml. Null deploys the base values file unchanged."
  type = object({
    size                    = string
    workers                 = number
    schedule_on_coordinator = bool
    max_memory              = string
    max_memory_per_node     = string
    max_stage_count         = number
    coordinator = object({
      heap           = string
      cpu_request    = string
      memory_request = string
      memory_limit   = string
    })
    worker = optional(object({
      heap           = string
      cpu_request    = string
      memory_request = string
      memory_limit   = string
    }))
  })
  default = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "storage_contract" {
  description = "S3-compatible storage contract consumed by Trino."
  type = object({
    endpoint                = optional(string)
    region                  = string
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

variable "service_account_annotations" {
  description = "Optional annotations for the Trino service account, used by AWS workload identity integrations."
  type        = map(string)
  default     = {}
}

variable "service_account_name" {
  description = "Name of the Trino service account to create and use. Set this for workload-identity models that bind by service account name and need no annotation (e.g. EKS Pod Identity). When empty, a service account is created only if annotations are provided (IRSA-style)."
  type        = string
  default     = ""
}

variable "catalog_contract" {
  description = "Iceberg catalog contract consumed by Trino. Current local uses REST/Polaris; future provider profiles may use Glue."
  type = object({
    rest_uri                      = optional(string)
    token_uri                     = optional(string)
    warehouse                     = optional(string)
    oauth_scope                   = optional(string)
    trino_credentials_secret_name = optional(string)
    trino_client_id_key           = optional(string)
    trino_client_secret_key       = optional(string)
    catalog_type                  = optional(string)
    catalog_provider              = optional(string)
    catalog_name                  = optional(string)
    runtime_profile               = optional(string)
    trino_catalog_name            = optional(string)
    default_warehouse_location    = optional(string)
    glue_catalog_id               = optional(string)
    glue_region                   = optional(string)
    provider                      = optional(string)
    implementation                = optional(string)
    auth_mode                     = optional(string)
    ssl_mode                      = optional(string)
    endpoint                      = optional(string)
    ingress_mode                  = optional(string)
  })
}

variable "stage_catalog_contracts" {
  description = "Stage-indexed catalog bindings. When present, Trino renders one catalog properties file per stage instead of a single compatibility catalog."
  # Declared with the exact same schema as catalog_contract above (not
  # map(any)): the fallback branch in locals.stage_catalog_contracts merges
  # this map with catalog_contract in a single ternary, and Terraform's
  # implicit object-type conversion at this variable boundary would otherwise
  # drop catalog_contract's non-declared attributes (e.g. "adapter") while
  # leaving this map's caller-supplied attributes untouched, producing two
  # branches with mismatched attribute sets ("Inconsistent conditional result
  # types") that only surfaces once real values are known at apply time.
  type = map(object({
    rest_uri                      = optional(string)
    token_uri                     = optional(string)
    warehouse                     = optional(string)
    oauth_scope                   = optional(string)
    trino_credentials_secret_name = optional(string)
    trino_client_id_key           = optional(string)
    trino_client_secret_key       = optional(string)
    catalog_type                  = optional(string)
    catalog_provider              = optional(string)
    catalog_name                  = optional(string)
    runtime_profile               = optional(string)
    trino_catalog_name            = optional(string)
    default_warehouse_location    = optional(string)
    glue_catalog_id               = optional(string)
    glue_region                   = optional(string)
    provider                      = optional(string)
    implementation                = optional(string)
    auth_mode                     = optional(string)
    ssl_mode                      = optional(string)
    endpoint                      = optional(string)
    ingress_mode                  = optional(string)
  }))
  default = {}
}

variable "web_ui_fixed_user" {
  description = "When set, the Web UI signs in as this user (web-ui.authentication.type=fixed) instead of showing its form: Trino has no authentication of its own, and the perimeter guards the route."
  type        = string
  default     = null
}

