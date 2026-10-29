variable "namespace" {
  description = "Kubernetes namespace where application DB password Secrets are created."
  type        = string
}

variable "name_prefix" {
  description = "Name prefix for RDS and Kubernetes resources."
  type        = string
  default     = "openlakeforge"
}

variable "vpc_id" {
  description = "VPC ID for the RDS security group."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the RDS subnet group."
  type        = list(string)
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed to connect to PostgreSQL."
  type        = list(string)
}

variable "engine_version" {
  description = "RDS PostgreSQL engine version. Pin the major version only (e.g. \"16\") so RDS selects the current default minor; AWS retires specific minors over time and the provider suppresses the partial-version diff."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "RDS allocated storage in GiB."
  type        = number
  default     = 20
}

variable "master_username" {
  description = "RDS master username used only by the bootstrap job."
  type        = string
  default     = "openlakeforge_admin"
}

variable "databases" {
  description = "Metadata databases to create, one entry per stage-scoped service instance. Each entry's credentials Secret is materialized in every namespace listed, plus this module's own, so a stage-scoped consumer mounts it from where it runs. Mirrors modules/storage/postgresql's interface so both roots share one contract shape."
  type = list(object({
    key                     = string
    db_name                 = string
    db_user                 = string
    credentials_secret_name = string
    namespaces              = list(string)
  }))
  default = []
}

variable "cluster_name" {
  description = "EKS cluster the bootstrap Job's service account is bound to for EKS Pod Identity."
  type        = string
}

variable "region" {
  description = "AWS region of the RDS-managed master password secret."
  type        = string
}

variable "bootstrap_job_image" {
  description = "Image that creates the database Secrets and reads the master password: needs kubectl, the AWS CLI and jq."
  type        = string
}
