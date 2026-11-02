variable "profile_name" {
  description = "Deployment Profile name, reported as provider_contracts.deployment.profile_name."
  type        = string
}

variable "provider_name" {
  description = "Provider identifier (local, aws, azure), reported as provider_contracts.deployment.provider."
  type        = string
}

variable "region" {
  description = "Cloud region reported on the deployment; null where the provider has none."
  type        = string
  default     = null
}

variable "environment_label" {
  description = "Human-readable environment name used in check messages, for example `AWS POC`."
  type        = string
}

variable "shared_namespace" {
  description = "Kubernetes namespace owning the shared platform services; no stage may reuse it."
  type        = string
}

variable "stage_namespaces" {
  description = "Enabled stage name to the namespace that stage owns."
  type        = map(string)
}

variable "kube_context" {
  description = "Kube context the platform contract resolved."
  type        = string
}

variable "foundation_kube_context" {
  description = "Kube context the foundation contract published; the platform context must equal it."
  type        = string
}

variable "implementations" {
  description = "Implementation identifier each shared contract binds, keyed by capability."
  type = object({
    foundation          = string
    kubernetes_platform = string
    metadata_database   = string
    secrets             = string
    observability       = string
    artifact_registry   = string
  })
}

variable "query" {
  description = "Shared query service binding; its endpoint is also what every stage's query block points at."
  type = object({
    implementation = string
    endpoint       = string
  })
}

variable "catalog_service" {
  description = "Shared catalog service binding, or null where the catalog is stage-scoped and has no shared service (Glue)."
  type = object({
    implementation = string
    endpoint       = string
  })
  default = null
}

variable "catalog" {
  description = "Active catalog's declared consumer support, which the catalog_contract_consumer_support check compares against its catalog_type."
  type = object({
    catalog_type         = string
    trino_support        = list(string)
    dagster_support      = list(string)
    floe_support         = list(string)
    dbt_support          = list(string)
    openmetadata_support = list(string)
  })
}

variable "ops_storage" {
  description = "Shared operations bucket binding. The credential fields are null where the provider authenticates by workload identity."
  type = object({
    implementation           = string
    bucket_name              = string
    artifact_base_uri        = string
    access_mode              = string
    local_upload_access_mode = string
    credentials_secret_name  = string
    access_key_id_key        = string
    secret_access_key_key    = string
  })
}

variable "identity" {
  description = "Shared identity binding. `extra` holds provider-specific fields merged after ref, implementation and roles (the OIDC issuer and clients)."
  type = object({
    implementation = string
    roles          = any
    extra          = optional(any, {})
  })
}

variable "access" {
  description = "Shared access binding. `extra` holds the ingress fields merged after ref and implementation; empty for a port-forward contract (ADR 0013)."
  type = object({
    implementation = string
    extra          = optional(any, {})
  })
}

variable "portal_implementation" {
  description = "Implementation of the shared portal binding; null omits the binding."
  type        = string
  default     = null
}

variable "governance_service" {
  description = "Shared governance service binding, or null when no stage is governed."
  type = object({
    implementation = string
    endpoint       = string
  })
  default = null
}

variable "code_locations" {
  description = "Dagster code locations rendered into every stage's orchestration block."
  type = list(object({
    name               = string
    definitions_module = string
  }))
}

variable "metadata_database" {
  description = "Shared metadata database contract that every stage's own database block is merged onto."
  type        = any
}

variable "stage_databases" {
  description = "Service databases keyed `dagster_<stage>`, `superset_<stage>` and the shared `openmetadata`; every entry names its database, user and credentials Secret."
  type = map(object({
    db_name                 = string
    db_user                 = string
    credentials_secret_name = string
  }))
}

variable "stage_service_namespaces" {
  description = "Stage and namespace of every deployed stage-scoped service instance (Dagster, Superset), checked against the namespace its stage owns."
  type = list(object({
    stage     = string
    namespace = string
  }))
}

variable "stages" {
  description = "Enabled stages. `storage` and `catalog` are the provider-specific blocks of the stage contract, passed through unchanged; `query_extra` is merged into the stage query block; the two service names are omitted from the contract when null."
  type = map(object({
    namespace              = string
    analytics              = bool
    governance             = bool
    service_account        = string
    catalog_endpoint_ref   = string
    query_catalog_name     = string
    query_extra            = optional(map(string), {})
    pipeline_service_name  = optional(string)
    dashboard_service_name = optional(string)
    storage                = any
    catalog                = any
  }))
}
