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
  description = "Optional kubeconfig path. Defaults to the repository-local .tmp/kubeconfigs/local.yaml."
  type        = string
  default     = null
}

variable "helm_repository_cache_path" {
  description = "Optional Helm provider repository cache directory. Defaults to the repository-local .tmp/helm/local/repository-cache; an installed distribution's payload is read-only, so this must be overridden to a writable path under OLF_HOME."
  type        = string
  default     = null
}

variable "helm_repository_config_path" {
  description = "Optional Helm provider repository config file. Defaults to the repository-local .tmp/helm/local/repositories.yaml; an installed distribution's payload is read-only, so this must be overridden to a writable path under OLF_HOME."
  type        = string
  default     = null
}

variable "kube_context" {
  description = "Fallback kubeconfig context for the local foundation cluster when the foundation state is not inspected by wrapper scripts."
  type        = string
  default     = "kind-openlakeforge-local"
}

variable "foundation_state_path" {
  description = "Local Terraform state path for the local cluster foundation root."
  type        = string
  default     = null
}

variable "bronze_bucket_name" {
  description = "Existing DEV Bronze bucket retained from the v0.2 local profile."
  type        = string
  default     = "lakehouse-bronze"
}

variable "silver_bucket_name" {
  description = "Existing DEV Silver bucket retained from the v0.2 local profile."
  type        = string
  default     = "lakehouse-silver"
}

variable "gold_bucket_name" {
  description = "Existing DEV Gold bucket retained from the v0.2 local profile."
  type        = string
  default     = "lakehouse-gold"
}

variable "ops_bucket_name" {
  description = "S3 bucket used for local operational artifacts: manifests, logs, reports, and run artifacts."
  type        = string
  default     = "openlakeforge-ops"
}

variable "s3_region" {
  description = "S3 region used by local S3-compatible storage clients."
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
  default     = "local"
}

variable "project_code_image_pull_policy" {
  description = "Project-code image pull policy used by the Dagster code server and run pods."
  type        = string
  default     = "Never"
}

variable "project_code_image_revision" {
  description = "Local project-code image revision used to force Dagster pod rollouts when the tag is reused."
  type        = string
  default     = "manual"
}

variable "superset_image_repository" {
  description = "Superset image repository used by the local Superset Helm release."
  type        = string
  default     = "ghcr.io/openlakeforge/superset"
}

variable "superset_image_tag" {
  description = "Superset image tag used by the local Superset Helm release."
  type        = string
  default     = "local"
}

variable "superset_image_pull_policy" {
  description = "Superset image pull policy used by the local Superset Helm release."
  type        = string
  default     = "Never"
}

variable "trino_chart_package_path" {
  description = "Optional local Trino Helm chart package used by local-up to avoid transient GitHub chart download failures."
  type        = string
  default     = null
}

variable "dagster_chart_package_path" {
  description = "Optional local Dagster Helm chart package used by local-up to avoid transient GitHub chart download failures."
  type        = string
  default     = null
}

variable "seaweedfs_chart_package_path" {
  description = "Optional local SeaweedFS Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "polaris_chart_package_path" {
  description = "Optional local Polaris Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "openmetadata_chart_package_path" {
  description = "Optional local OpenMetadata Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "openmetadata_deps_chart_package_path" {
  description = "Optional local openmetadata-dependencies Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "superset_chart_package_path" {
  description = "Optional local Superset Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "traefik_chart_package_path" {
  description = "Optional local Traefik Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "cert_manager_chart_package_path" {
  description = "Optional local cert-manager Helm chart package used by local-up to avoid transient chart download failures."
  type        = string
  default     = null
}

variable "access_base_domain" {
  description = "Deployment Profile spec.access.base_domain (ADR 0013): the domain every route host sits under."
  type        = string
  default     = "olf.localhost"
}

variable "access_issuer" {
  description = "Deployment Profile spec.access.issuer (ADR 0013)."
  type        = string
  default     = "local-ca"

  validation {
    condition     = var.access_issuer == "local-ca"
    error_message = "The local provider ships only the local-ca issuer; set spec.access.issuer to local-ca."
  }
}

variable "manage_user_deployments" {
  description = "Whether Terraform owns the Dagster user-code deployments. `olf deploy` (deprecated, single stage) leaves this true; `olf platform apply` sets it false so `olf project deploy` owns the openlakeforge-project release instead."
  type        = bool
  default     = true
}

variable "identity_issuer" {
  description = "Deployment Profile spec.identity.issuer (ADR 0014): `keycloak` deploys the adapter, `external` takes the contract from identity_external."
  type        = string
  default     = "keycloak"

  validation {
    condition     = contains(["keycloak", "external"], var.identity_issuer)
    error_message = "spec.identity.issuer must be keycloak or external."
  }
}

variable "identity_external" {
  description = "Deployment Profile spec.identity issuer_url, role_claim and role_mapping for an existing issuer. Required when identity_issuer is external."
  type = object({
    issuer_url   = string
    role_claim   = string
    role_mapping = map(list(string))
  })
  default = null
}
