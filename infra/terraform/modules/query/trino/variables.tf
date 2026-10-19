variable "namespace" {
  description = "Kubernetes namespace where Trino is deployed."
  type        = string
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "trino"
}

variable "chart_repository" {
  description = "Trino Helm chart repository."
  type        = string
  default     = "https://trinodb.github.io/charts"
}

variable "chart_version" {
  description = "Trino Helm chart version."
  type        = string
  default     = "1.42.2"
}

variable "chart_package_path" {
  description = "Optional local Trino Helm chart package path. When set, Terraform installs this package instead of downloading from chart_repository."
  type        = string
  default     = null
}

variable "base_values_file" {
  description = "Path to the non-secret base Helm values file."
  type        = string
}

variable "storage_contract" {
  description = "S3-compatible storage contract consumed by Trino."
  type = object({
    endpoint                = optional(string)
    region                  = string
    path_style_access       = optional(bool)
    credentials_secret_name = optional(string)
    provider                = optional(string)
    implementation          = optional(string)
    auth_mode               = optional(string)
    ssl_mode                = optional(string)
    ingress_mode            = optional(string)
  })
}

variable "service_account_annotations" {
  description = "Optional annotations for the Trino service account, used by AWS workload identity integrations."
  type        = map(string)
  default     = {}
}

variable "service_account_name" {
  description = "Name of the Trino service account to create and use. Set this for workload-identity models that bind by service account name and need no annotation (e.g. EKS Pod Identity). When empty, a service account is created only if annotations are provided (IRSA-style)."
  type        = string
  default     = ""
}

variable "catalog_contract" {
  description = "Iceberg catalog contract consumed by Trino. Current local uses REST/Polaris; future provider profiles may use Glue."
  type = object({
    rest_uri                      = optional(string)
    token_uri                     = optional(string)
    warehouse                     = optional(string)
    oauth_scope                   = optional(string)
    trino_credentials_secret_name = optional(string)
    trino_client_id_key           = optional(string)
    trino_client_secret_key       = optional(string)
    bootstrap_run_id              = optional(string)
    catalog_type                  = optional(string)
    catalog_provider              = optional(string)
    catalog_name                  = optional(string)
    runtime_profile               = optional(string)
    trino_catalog_name            = optional(string)
    default_warehouse_location    = optional(string)
    glue_catalog_id               = optional(string)
    glue_region                   = optional(string)
    provider                      = optional(string)
    implementation                = optional(string)
    auth_mode                     = optional(string)
    ssl_mode                      = optional(string)
    endpoint                      = optional(string)
    ingress_mode                  = optional(string)
  })
}

variable "stage_catalog_contracts" {
  description = "Stage-indexed catalog bindings. When present, Trino renders one catalog properties file per stage instead of a single compatibility catalog."
  # Declared with the exact same schema as catalog_contract above (not
  # map(any)): the fallback branch in locals.stage_catalog_contracts merges
  # this map with catalog_contract in a single ternary, and Terraform's
  # implicit object-type conversion at this variable boundary would otherwise
  # drop catalog_contract's non-declared attributes (e.g. "adapter") while
  # leaving this map's caller-supplied attributes untouched, producing two
  # branches with mismatched attribute sets ("Inconsistent conditional result
  # types") that only surfaces once real values are known at apply time.
  type = map(object({
    rest_uri                      = optional(string)
    token_uri                     = optional(string)
    warehouse                     = optional(string)
    oauth_scope                   = optional(string)
    trino_credentials_secret_name = optional(string)
    trino_client_id_key           = optional(string)
    trino_client_secret_key       = optional(string)
    bootstrap_run_id              = optional(string)
    catalog_type                  = optional(string)
    catalog_provider              = optional(string)
    catalog_name                  = optional(string)
    runtime_profile               = optional(string)
    trino_catalog_name            = optional(string)
    default_warehouse_location    = optional(string)
    glue_catalog_id               = optional(string)
    glue_region                   = optional(string)
    provider                      = optional(string)
    implementation                = optional(string)
    auth_mode                     = optional(string)
    ssl_mode                      = optional(string)
    endpoint                      = optional(string)
    ingress_mode                  = optional(string)
  }))
  default = {}
}

variable "catalog_bootstrap_revision" {
  description = "Content revision used to force Trino pod rollouts when Polaris bootstrap credentials or grants change."
  type        = string
}

variable "stage_namespaces" {
  description = "Stage name to stage namespace. When non-empty, a bootstrap Job creates the Trino workload credentials (ADR 0016) and copies each stage's own principal into its namespace. Empty creates nothing."
  type        = map(string)
  default     = {}
}

variable "bootstrap_job_image" {
  description = "Image of the Job that creates the Trino workload credentials when missing, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}

variable "workload_authentication" {
  description = "How far ADR 0016 is applied. off: nothing changes. tls: an HTTPS listener and certificate beside the HTTP one, callers still unauthenticated, so they can move to HTTPS and prove the CA first. password: PASSWORD authentication on an HTTPS-only coordinator with the principals rules; every caller must already hold its credential."
  type        = string
  default     = "off"

  validation {
    condition     = contains(["off", "tls", "password"], var.workload_authentication)
    error_message = "workload_authentication must be off, tls or password."
  }

  validation {
    condition     = var.workload_authentication == "off" || var.tls_issuer_name != null
    error_message = "workload_authentication tls and password need tls_issuer_name."
  }

  validation {
    condition     = var.workload_authentication != "password" || length(var.stage_namespaces) > 0
    error_message = "workload_authentication password needs stage_namespaces: without the credential Job there is no password for any caller."
  }
}

variable "tls_issuer_name" {
  description = "cert-manager ClusterIssuer that signs the Trino serving certificate. Only read when workload_authentication is not off."
  type        = string
  default     = null
}

variable "web_ui_oidc" {
  description = "Sign the Web UI in through this Dex issuer (identity.oidc contract fields: issuer_url, the trino client_id and the Secret reference of its secret, in this namespace). null keeps the UI's own login. Needs HTTPS at the ingress and workload_authentication tls or password."
  type = object({
    issuer_url = string
    client_id  = string
    secret_ref = object({ name = string, key = string })
  })
  default = null

  validation {
    condition     = var.web_ui_oidc == null || var.workload_authentication != "off"
    error_message = "web_ui_oidc needs workload_authentication tls or password: Trino OAuth2 needs the HTTPS listener and its CA."
  }
}
