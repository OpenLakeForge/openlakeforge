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

variable "s3_port" {
  description = "SeaweedFS S3 service port."
  type        = number
  default     = 8333
}

variable "bootstrap_job_image" {
  description = "Image of the Job that creates the S3 key Secrets when missing, pinned by digest (release/component-catalog.yaml)."
  type        = string
  default     = "alpine/k8s:1.30.0@sha256:bd01dae02676ce4cab62fc744e43443eee5bf660054e94d3496d23bfc35d384e"
}
