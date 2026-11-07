variable "bucket_name_prefix" {
  description = "Prefix for generated AWS S3 buckets. A short random suffix is appended for global-uniqueness."
  type        = string
  default     = "openlakeforge-poc"
}

variable "region" {
  description = "AWS region used by S3 clients."
  type        = string
}

variable "ops_bucket_name" {
  description = "Optional explicit operational artifact bucket name. Null uses bucket_name_prefix with a random suffix."
  type        = string
  default     = null
}

variable "stage_buckets" {
  description = "Stage-owned medallion bucket bindings, one entry per enabled stage. DEV may preserve a v0.2 physical bucket name while every later stage gets a suffix-qualified name unique across S3."
  type = map(object({
    bronze_bucket_name    = optional(string)
    silver_bucket_name    = optional(string)
    gold_bucket_name      = optional(string)
    preserve_legacy_names = optional(bool, false)
  }))
  default = {}
}

variable "force_destroy" {
  description = "Whether Terraform may delete non-empty POC buckets."
  type        = bool
  default     = true
}
