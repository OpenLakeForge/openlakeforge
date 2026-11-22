variable "namespace" {
  description = "Kubernetes namespace where PostgreSQL is deployed."
  type        = string
}

variable "release_name" {
  description = "Name prefix for Kubernetes resources."
  type        = string
  default     = "postgresql"
}

variable "storage_size" {
  description = "Persistent volume size for PostgreSQL data."
  type        = string
  default     = "5Gi"
}

variable "storage_class_name" {
  description = "Optional Kubernetes StorageClass for PostgreSQL data. Null uses the cluster default."
  type        = string
  default     = null
}

variable "databases" {
  description = "Metadata databases to create, one entry per service instance. Each entry's credentials Secret is materialized in every namespace listed, plus this module's own, so a stage-scoped consumer mounts it from where it runs."
  type = list(object({
    key                     = string
    db_name                 = string
    db_user                 = string
    credentials_secret_name = string
    namespaces              = list(string)
  }))
  default = []
}

variable "polaris_db_name" {
  description = "PostgreSQL database name for the Polaris catalog metastore."
  type        = string
  default     = "polaris"
}

variable "polaris_db_user" {
  description = "PostgreSQL user for the Polaris catalog metastore."
  type        = string
  default     = "polaris"
}

variable "polaris_credentials_secret_name" {
  description = "Kubernetes Secret holding Polaris relational JDBC connection properties."
  type        = string
  default     = "postgresql-polaris-creds"
}

variable "bootstrap_job_image" {
  description = "Image of the Job that creates the credential Secrets when missing, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "alpine/k8s:1.31.13@sha256:7a319b15cfc99c9aaa27ec9891d6b4ca24632b795908591c7b0ba89b03549e0a"
}
