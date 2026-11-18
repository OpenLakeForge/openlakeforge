variable "namespace" {
  description = "Namespace of the reconciler CronJobs (the platform namespace, olf-system). The stages' key Secrets and the ingestion bot's Secret are read from here."
  type        = string
}

variable "release_name" {
  description = "Name of the ServiceAccount and the prefix of each stage's CronJob, `<release_name>-<stage>`."
  type        = string
  default     = "quality-reconciler"
}

variable "image" {
  description = "The platform API image (ADR 0023, decision 1): the CronJob overrides its command, so no second image exists."
  type        = string
  default     = null
}

variable "image_pull_policy" {
  description = "Pull policy of the reconciler image; the local root loads it into kind and passes Never."
  type        = string
  default     = "IfNotPresent"
}

variable "stages" {
  description = "One entry per governed stage; with none, the module creates nothing (ADR 0007: Slim has no reconciler). A stage names its activation prefix `s3://<ops bucket>/activations/<stage>/`, its database root `<catalog service>.lakehouse_<stage>` and the Secret holding its own ops-bucket key (the openlakeforge-quality-sync-<stage> identity). The schema FQN maps are optional: without them a table is looked up as `<database>.<domain>_silver.<table>`, which is the physical name on Polaris."
  type = map(object({
    artifact_base_uri       = string
    database_fqn            = string
    credentials_secret_name = string
    silver_schema_fqns      = optional(map(string), {})
    gold_schema_fqns        = optional(map(string), {})
  }))
  default = {}

  validation {
    condition     = alltrue([for stage, binding in var.stages : can(regex("^s3://[^/]+/activations/${stage}/$", binding.artifact_base_uri))])
    error_message = "A stage's artifact_base_uri is s3://<ops bucket>/activations/<stage>/, so the reconciler can address that stage's prefix and no other."
  }

  validation {
    condition     = alltrue([for stage, binding in var.stages : endswith(binding.database_fqn, ".lakehouse_${stage}") && length(split(".", binding.database_fqn)) == 2])
    error_message = "A stage's database_fqn is <catalog service>.lakehouse_<stage>, so the reconciler cannot name another stage's tables."
  }

  validation {
    condition     = alltrue([for stage in keys(var.stages) : can(regex("^[a-z0-9][a-z0-9-]*$", stage))])
    error_message = "A stage name is a plain lowercase name."
  }
}

variable "openmetadata_url" {
  description = "In-cluster base URL of OpenMetadata, http://<service>.<namespace>:<port> (the one egress the reconciler has besides the object store)."
  type        = string
}

variable "ingestion_bot_secret_name" {
  description = "Secret in `namespace` holding the ingestion bot's JWT, written by `olf infra openmetadata bootstrap` after the apply (the governance module's contract). Passed by reference: until the bootstrap writes it a run stays in CreateContainerConfigError."
  type        = string
}

variable "ingestion_bot_jwt_key" {
  description = "Key within the ingestion bot's Secret that holds the token."
  type        = string
}

variable "s3_endpoint_url" {
  description = "S3 endpoint of the object store, from its provider contract."
  type        = string
}

variable "s3_region" {
  description = "S3 region of the object store."
  type        = string
}

variable "schedule" {
  description = "Cron schedule of each stage's pass. Retries of a failed result back off from 1 minute to 1 hour inside the state, so a faster schedule only re-reads the bucket."
  type        = string
  default     = "*/5 * * * *"
}

variable "limit" {
  description = "Most runs one pass works on (1-200). The rest wait for the next pass, oldest first."
  type        = number
  default     = 25

  validation {
    condition     = var.limit >= 1 && var.limit <= 200
    error_message = "limit is between 1 and 200, the range of `olf infra openmetadata quality-sync --limit`."
  }
}

variable "warn_status" {
  description = "OpenMetadata status a WARN publishes as: Aborted (no incident) or Failed (opens an incident in OpenMetadata 1.13.6). Never Success."
  type        = string
  default     = "Aborted"

  validation {
    condition     = contains(["Aborted", "Failed"], var.warn_status)
    error_message = "warn_status is Aborted or Failed: a WARN is never published as Success."
  }
}
