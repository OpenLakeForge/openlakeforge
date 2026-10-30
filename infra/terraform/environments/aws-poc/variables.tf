variable "profile_name" {
  description = "Deployment Profile name resolved by `olf profile resolve`. Identifies this deployment across its stages."
  type        = string
  default     = "openlakeforge"
}

variable "shared_namespace" {
  description = "Kubernetes namespace owning the shared platform services: Trino and OpenMetadata."
  type        = string
  default     = "olf-system"
}

variable "query_sizing" {
  description = "Trino capacity for spec.query.size, rendered by olf from release/query-sizes.yaml. Null deploys the base values file unchanged."
  type = object({
    size                    = string
    workers                 = number
    schedule_on_coordinator = bool
    max_memory              = string
    max_memory_per_node     = string
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

variable "stages" {
  description = "Resolved deployment topology: one entry per stage the resolver knows about, with its enabled flag and capabilities. Disabled stages stay in the map so the root can report what an apply would remove."
  type = map(object({
    enabled    = bool
    analytics  = bool
    governance = bool
  }))
  default = {
    dev = {
      enabled    = true
      analytics  = true
      governance = true
    }
  }
}

variable "aws_region" {
  description = "AWS region for provider resources."
  type        = string
  default     = "eu-west-1"
}

variable "default_tags" {
  description = "Tags applied to every taggable resource via the provider default_tags block. Account-mandated tags (Project/Owner/Requester/Env/IaC) are supplied via a .tfvars file. Casing is significant."
  type        = map(string)
  default     = {}
}

variable "kubeconfig_path" {
  description = "Optional kubeconfig path. Defaults to the path emitted by the AWS foundation root."
  type        = string
  default     = null
}

variable "helm_repository_cache_path" {
  description = "Optional Helm provider repository cache directory. Defaults to the repository-local .tmp/helm/aws/repository-cache; an installed distribution's payload is read-only, so this must be overridden to a writable path under OLF_HOME."
  type        = string
  default     = null
}

variable "helm_repository_config_path" {
  description = "Optional Helm provider repository config file. Defaults to the repository-local .tmp/helm/aws/repositories.yaml; an installed distribution's payload is read-only, so this must be overridden to a writable path under OLF_HOME."
  type        = string
  default     = null
}

variable "kube_context" {
  description = "Fallback kubeconfig context for the AWS EKS foundation cluster."
  type        = string
  default     = "eks-openlakeforge-poc"
}

variable "foundation_state_path" {
  description = "Local Terraform state path for the AWS EKS foundation root."
  type        = string
  default     = null
}

variable "bucket_name_prefix" {
  description = "Prefix used for generated AWS S3 buckets."
  type        = string
  default     = "openlakeforge-poc"
}

variable "bronze_bucket_name" {
  description = "Optional existing DEV Bronze S3 bucket name from the v0.2 AWS POC. Null retains its generated legacy name."
  type        = string
  default     = null
}

variable "silver_bucket_name" {
  description = "Optional existing DEV Silver S3 bucket name from the v0.2 AWS POC. Null retains its generated legacy name."
  type        = string
  default     = null
}

variable "gold_bucket_name" {
  description = "Optional existing DEV Gold S3 bucket name from the v0.2 AWS POC. Null retains its generated legacy name."
  type        = string
  default     = null
}

variable "ops_bucket_name" {
  description = "Optional explicit operational artifact S3 bucket name."
  type        = string
  default     = null
}

variable "project_code_image_repository" {
  description = "Project-code image repository used by the Dagster code server and run pods."
  type        = string
  default     = "ghcr.io/openlakeforge/openlakeforge/project-code"
}

variable "project_code_image_tag" {
  description = "Project-code image tag used by the Dagster code server and run pods."
  type        = string
  default     = "aws-poc"
}

variable "project_code_image_pull_policy" {
  description = "Project-code image pull policy used by the Dagster code server and run pods."
  type        = string
  default     = "Always"
}

variable "project_code_image_revision" {
  description = "Project-code image revision used to force Dagster pod rollouts when the tag is reused."
  type        = string
  default     = "manual"
}

variable "trino_chart_package_path" {
  description = "Optional local Trino Helm chart package used by aws-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "dagster_chart_package_path" {
  description = "Optional local Dagster Helm chart package used by aws-up to avoid remote schema validation failures."
  type        = string
  default     = null
}

variable "openmetadata_chart_package_path" {
  description = "Optional local OpenMetadata Helm chart package used by aws-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "openmetadata_deps_chart_package_path" {
  description = "Optional local openmetadata-dependencies Helm chart package used by aws-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "superset_chart_package_path" {
  description = "Optional local Superset Helm chart package used by aws-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "rds_instance_class" {
  description = "RDS PostgreSQL instance class for the AWS POC."
  type        = string
  default     = "db.t4g.micro"
}

variable "manage_user_deployments" {
  description = "Whether Terraform owns the Dagster user-code deployments. `olf deploy` (deprecated, single stage) leaves this true; `olf platform apply` sets it false so `olf project deploy` owns the openlakeforge-project release instead."
  type        = bool
  default     = true
}
