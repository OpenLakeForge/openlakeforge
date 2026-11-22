variable "namespace" {
  description = "Kubernetes namespace where SeaweedFS is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "seaweedfs"
}

variable "chart_repository" {
  description = "SeaweedFS Helm chart repository."
  type        = string
  default     = "https://seaweedfs.github.io/seaweedfs/helm"
}

variable "chart_version" {
  description = "SeaweedFS Helm chart version."
  type        = string
  default     = "4.48.0"
}

variable "chart_package_path" {
  description = "Optional local SeaweedFS Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "storage_size" {
  description = "Row of storage_sizes_file that sizes the master, filer and volume server PVCs (spec.storage.size)."
  type        = string
  default     = "small"
}

variable "storage_sizes_file" {
  description = "Path to release/storage-sizes.yaml."
  type        = string
}

variable "image_tag" {
  description = "SeaweedFS image tag used by the chart and bucket jobs."
  type        = string
  default     = "4.48@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d"
}

variable "bucket_names" {
  description = "S3 buckets to create in SeaweedFS."
  type        = list(string)
}

variable "region" {
  description = "S3 region advertised to clients."
  type        = string
}

variable "workload_namespaces" {
  description = "Extra namespaces the S3 credentials Secret is materialized in, for workloads that run outside this module's namespace."
  type        = list(string)
  default     = []
}

variable "credentials_secret_name" {
  description = "Kubernetes Secret name containing S3 credentials for downstream clients."
  type        = string
  default     = "seaweedfs-s3-creds"
}

variable "stage_credentials" {
  description = "Stage-owned S3 identities. Each identity is granted access only to its medallion buckets and the shared ops bucket."
  type = map(object({
    namespace               = string
    credentials_secret_name = string
    bronze_bucket_name      = string
    silver_bucket_name      = string
    gold_bucket_name        = string
    ops_bucket_name         = string
  }))
  default = {}
}

variable "log_reader_credentials" {
  description = "A read-only identity for the platform API's archived-logs route (ADR 0021): Read and List on each prefix of the ops bucket, nothing else. Null when the platform API is not deployed."
  type = object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
    prefixes                = list(string)
  })
  default = null

  validation {
    condition     = var.log_reader_credentials == null || alltrue([for prefix in try(var.log_reader_credentials.prefixes, []) : endswith(prefix, "/") && !startswith(prefix, "/")])
    error_message = "Each log reader prefix is a key prefix ending in '/', so the grant cannot reach a sibling whose name starts the same."
  }
}

variable "quality_reader_credentials" {
  description = "One read-only identity per stage for the platform API's quality summaries: Read and List on that stage's `activations/<stage>/run-artifacts/quality/` in the ops bucket, nothing else. Empty when the platform API is not deployed."
  type = map(object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
  }))
  default = {}

  validation {
    condition     = alltrue([for stage in keys(var.quality_reader_credentials) : can(regex("^[a-z0-9][a-z0-9-]*$", stage))])
    error_message = "A stage name is a plain lowercase name, so the grant's prefix cannot widen past its own stage."
  }
}

variable "quality_sync_credentials" {
  description = "One identity per governed stage for its quality reconciler (ADR 0007): Read and List on that stage's `activations/<stage>/run-artifacts/quality/`, Read, Write and List on its `activations/<stage>/quality-sync/`, nothing else. Empty when no stage has governance."
  type = map(object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
  }))
  default = {}

  validation {
    condition     = alltrue([for stage in keys(var.quality_sync_credentials) : can(regex("^[a-z0-9][a-z0-9-]*$", stage))])
    error_message = "A stage name is a plain lowercase name, so the grant's prefix cannot widen past its own stage."
  }
}

variable "audit_writer_credentials" {
  description = "A write-only identity for the platform API's audit records (ADR 0021, decision 3): Write on one prefix of the ops bucket, nothing else. Null when the platform API is not deployed."
  type = object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
    prefix                  = string
  })
  default = null

  validation {
    condition     = var.audit_writer_credentials == null || (endswith(try(var.audit_writer_credentials.prefix, ""), "/") && !startswith(try(var.audit_writer_credentials.prefix, "/"), "/"))
    error_message = "The audit prefix is a key prefix ending in '/', so the grant cannot reach a sibling whose name starts the same."
  }
}

variable "log_collector_credentials" {
  description = "A write-only identity for the log collector (ADR 0022, decision 3): Write on each v2 archive prefix of the ops bucket, no Read, no List. Null when the collector is not deployed."
  type = object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
    prefixes                = list(string)
  })
  default = null

  validation {
    condition     = var.log_collector_credentials == null || alltrue([for prefix in try(var.log_collector_credentials.prefixes, []) : endswith(prefix, "/") && !startswith(prefix, "/")])
    error_message = "Each collector prefix is a key prefix ending in '/', so the grant cannot reach a sibling whose name starts the same."
  }
}

variable "alerts_credentials" {
  description = "The alert evaluator's identity (ADR 0023, decision 3): Read, Write and List on one prefix of the ops bucket, where its state and history live, Read and List on each quality prefix, where the stages write run manifests, and Read on each policy stage's ACTIVE.json and activation records and on the frozen project revisions, where the alert policy is read. Null when no alert receiver is declared."
  type = object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
    prefix                  = string
    quality_prefixes        = list(string)
    policy_stages           = list(string)
  })
  default = null

  validation {
    condition = var.alerts_credentials == null || alltrue([
      for prefix in concat([try(var.alerts_credentials.prefix, "")], try(var.alerts_credentials.quality_prefixes, [])) :
      endswith(prefix, "/") && !startswith(prefix, "/")
    ])
    error_message = "Each alerts prefix is a key prefix ending in '/', so the grant cannot reach a sibling whose name starts the same."
  }
}

variable "alert_state_reader_credentials" {
  description = "A read-only identity for the platform API's alert incidents route (#1258): Read and List on the alert evaluator's state prefix of the ops bucket, `<alerts prefix>state/`, nothing else. Null when the platform API or the evaluator is not deployed."
  type = object({
    namespace               = string
    credentials_secret_name = string
    ops_bucket_name         = string
    prefix                  = string
  })
  default = null

  validation {
    condition     = var.alert_state_reader_credentials == null || (endswith(try(var.alert_state_reader_credentials.prefix, ""), "/") && !startswith(try(var.alert_state_reader_credentials.prefix, "/"), "/"))
    error_message = "The alert state prefix is a key prefix ending in '/', so the grant cannot reach a sibling whose name starts the same."
  }
}

variable "files_credentials" {
  description = "An identity for the platform API's files explorer (ADR 0021): Read, Write and List on each named bucket, the enabled stages' layer buckets and the ops bucket. Null when the platform API is not deployed."
  type = object({
    namespace               = string
    credentials_secret_name = string
    bucket_names            = list(string)
  })
  default = null

  validation {
    condition     = var.files_credentials == null || length(try(var.files_credentials.bucket_names, [])) > 0
    error_message = "The files identity names at least one bucket; an identity with no grant would authenticate and be allowed nothing."
  }
}

variable "object_retention" {
  description = "Expiry of archived objects under key prefixes of one bucket. Applied as a SeaweedFS filer path TTL (`fs.configure -ttl`), because the S3 lifecycle API stores a rule in 4.48 without ever creating the TTL (ADR 0022, decision 6). It acts on objects written after it is set, so it never shortens the life of what already exists. Above 255 days the TTL is whole weeks, months (30 days) or years (365 days), rounded up when the days do not divide. Null applies none."
  type = object({
    bucket   = string
    days     = number
    prefixes = list(string)
  })
  default = null

  validation {
    condition     = var.object_retention == null || (try(var.object_retention.days, 0) >= 1 && floor(try(var.object_retention.days, 0)) == try(var.object_retention.days, 0))
    error_message = "object_retention.days is a whole number of days, at least 1."
  }

  validation {
    condition     = var.object_retention == null || try(var.object_retention.days, 0) <= 3650
    error_message = "object_retention.days is at most 3650, the deployment profile's limit for retention_days."
  }

  validation {
    condition     = var.object_retention == null || (length(try(var.object_retention.prefixes, [])) > 0 && alltrue([for prefix in try(var.object_retention.prefixes, []) : endswith(prefix, "/") && !startswith(prefix, "/")]))
    error_message = "object_retention.prefixes is a non-empty list of key prefixes ending in '/', so a rule cannot reach a sibling whose name starts the same."
  }
}

variable "s3_port" {
  description = "SeaweedFS S3 service port."
  type        = number
  default     = 8333
}

variable "bootstrap_job_image" {
  description = "Image of the Job that creates the S3 key Secrets when missing, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "alpine/k8s:1.31.13@sha256:7a319b15cfc99c9aaa27ec9891d6b4ca24632b795908591c7b0ba89b03549e0a"
}
