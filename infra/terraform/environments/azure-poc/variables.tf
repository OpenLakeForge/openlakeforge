variable "profile_name" {
  description = "Deployment Profile name resolved by `olf profile resolve`. Identifies this deployment across its stages."
  type        = string
  default     = "openlakeforge"
}

variable "shared_namespace" {
  description = "Kubernetes namespace owning the shared platform services: PostgreSQL, SeaweedFS, Polaris, Trino, and OpenMetadata."
  type        = string
  default     = "olf-system"
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

variable "kubeconfig_path" {
  description = "Optional kubeconfig path. Defaults to the path emitted by the Azure foundation root."
  type        = string
  default     = null
}

variable "helm_repository_cache_path" {
  description = "Optional Helm provider repository cache directory. Defaults to the repository-local .tmp/helm/azure/repository-cache; an installed distribution's payload is read-only, so this must be overridden to a writable path under OLF_HOME."
  type        = string
  default     = null
}

variable "helm_repository_config_path" {
  description = "Optional Helm provider repository config file. Defaults to the repository-local .tmp/helm/azure/repositories.yaml; an installed distribution's payload is read-only, so this must be overridden to a writable path under OLF_HOME."
  type        = string
  default     = null
}

variable "kube_context" {
  description = "Fallback kubeconfig context for the Azure AKS foundation cluster."
  type        = string
  default     = "aks-openlakeforge-poc"
}

variable "foundation_state_path" {
  description = "Local Terraform state path for the Azure AKS foundation root."
  type        = string
  default     = null
}

variable "ops_bucket_name" {
  description = "S3-compatible SeaweedFS bucket used for operational artifacts: manifests, logs, reports, and run artifacts."
  type        = string
  default     = "openlakeforge-ops"
}

variable "s3_region" {
  description = "S3 region used by SeaweedFS S3-compatible storage clients in the Azure POC."
  type        = string
  default     = "us-east-1"
}

variable "project_code_image_repository" {
  description = "Project-code image repository used by the Dagster code server and run pods."
  type        = string
  default     = "ghcr.io/openlakeforge/project-code"
}

variable "project_code_image_tag" {
  description = "Project-code image tag used by the Dagster code server and run pods."
  type        = string
  default     = "azure-poc"
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

variable "superset_image_repository" {
  description = "Superset image repository used by the Azure POC Superset Helm release."
  type        = string
  default     = "ghcr.io/openlakeforge/superset"
}

variable "superset_image_tag" {
  description = "Superset image tag used by the Azure POC Superset Helm release."
  type        = string
  default     = "azure-poc"
}

variable "superset_image_pull_policy" {
  description = "Superset image pull policy used by the Azure POC Superset Helm release."
  type        = string
  default     = "Always"
}

variable "trino_chart_package_path" {
  description = "Optional local Trino Helm chart package used by azure-up to avoid transient GitHub chart download failures."
  type        = string
  default     = null
}

variable "dagster_chart_package_path" {
  description = "Optional local Dagster Helm chart package used by azure-up to avoid remote schema validation failures."
  type        = string
  default     = null
}

variable "seaweedfs_chart_package_path" {
  description = "Optional local SeaweedFS Helm chart package used by azure-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "polaris_chart_package_path" {
  description = "Optional local Polaris Helm chart package used by azure-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "openmetadata_chart_package_path" {
  description = "Optional local OpenMetadata Helm chart package used by azure-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "openmetadata_deps_chart_package_path" {
  description = "Optional local openmetadata-dependencies Helm chart package used by azure-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "superset_chart_package_path" {
  description = "Optional local Superset Helm chart package used by azure-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "manage_user_deployments" {
  description = "Whether Terraform owns the Dagster user-code deployments. `olf deploy` (deprecated, single stage) leaves this true; `olf platform apply` sets it false so `olf project deploy` owns the openlakeforge-project release instead."
  type        = bool
  default     = true
}
