variable "namespace" {
  description = "Kubernetes namespace where Superset is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "superset"
}

variable "chart_repository" {
  description = "Superset Helm chart repository."
  type        = string
  default     = "https://apache.github.io/superset/"
}

variable "chart_version" {
  description = "Superset Helm chart version."
  type        = string
  default     = "0.15.5"
}

variable "chart_package_path" {
  description = "Optional local Superset Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "image_repository" {
  description = "Superset image repository with OpenLakeForge runtime drivers."
  type        = string
  default     = "ghcr.io/openlakeforge/superset"
}

variable "image_tag" {
  description = "Superset image tag."
  type        = string
  default     = "local"
}

variable "image_pull_policy" {
  description = "Superset image pull policy."
  type        = string
  default     = "Never"
}

variable "admin_username" {
  description = "Local Superset admin username."
  type        = string
  default     = "admin"
}

variable "admin_password" {
  description = "Local Superset admin password."
  type        = string
  default     = "admin"
  sensitive   = true
}

variable "admin_email" {
  description = "Local Superset admin email."
  type        = string
  default     = "admin@openlakeforge.local"
}

variable "http_port" {
  description = "Superset HTTP service port."
  type        = number
  default     = 8088
}

variable "postgresql_contract" {
  description = "Metadata PostgreSQL contract consumed by Superset."
  type = object({
    host                             = string
    port                             = number
    superset_db_name                 = string
    superset_db_user                 = string
    superset_credentials_secret_name = string
    provider                         = optional(string)
    implementation                   = optional(string)
    auth_mode                        = optional(string)
    ssl_mode                         = optional(string)
    endpoint                         = optional(string)
  })
}

variable "trino_ca_configmap_name" {
  description = "ConfigMap holding the CA that signs Trino's certificate. When set, it is mounted at /etc/olf/query-ca in every Superset pod so the Trino database connection can verify the HTTPS listener (ADR 0016). Null leaves the pods unchanged."
  type        = string
  default     = null
}

variable "postgresql_ssl_mode" {
  description = "PostgreSQL sslmode used by Superset metadata connections."
  type        = string
  default     = "disable"
}

variable "bootstrap_job_image" {
  description = "Image of the Job that copies the OIDC client Secret into this namespace, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "oidc" {
  description = "identity.oidc fields for this stage (ADR 0014): the issuer, role claim and mapping, the reporting grants of release/identity-roles.yaml, the scope to request, the superset client id, the client Secret reference and the namespace that holds it, and the ConfigMap with the CA that signs the issuer certificate. Null keeps the local Superset login."
  type = object({
    issuer_url        = string
    role_claim        = string
    role_mapping      = map(list(string))
    grants            = map(string)
    scope             = string
    client_id         = string
    secret_ref        = object({ name = string, key = string })
    secret_namespace  = string
    ca_configmap_name = string
  })
  default = null
}
