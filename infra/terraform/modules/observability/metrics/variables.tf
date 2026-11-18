variable "namespace" {
  description = "Namespace of Prometheus (the platform namespace, olf-system); its series carry stage = platform."
  type        = string
}

variable "release_name" {
  description = "Name of the Deployment, Service, ServiceAccount, ConfigMap and PVC; the pods' app.kubernetes.io/name, which release/network-flows.yaml selects on."
  type        = string
  default     = "prometheus"
}

variable "image" {
  description = "Prometheus image, pinned by digest (release/component-catalog.yaml, prometheus)."
  type        = string
  nullable    = false
  default     = "prom/prometheus:v3.13.5@sha256:aafa9d694e6a883c02db516d9f6e5b84e14195f49a2a6456a4cca815edda72b9"
}

variable "kube_state_metrics_image" {
  description = "kube-state-metrics image, pinned by digest (release/component-catalog.yaml, kube_state_metrics)."
  type        = string
  nullable    = false
  default     = "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0@sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b"
}

variable "retention_days" {
  description = "spec.observability.metrics.retention_days: blocks older than this are deleted."
  type        = number

  validation {
    condition     = var.retention_days >= 1 && floor(var.retention_days) == var.retention_days
    error_message = "retention_days is a whole number of days, at least 1."
  }
}

variable "volume_size" {
  description = "Request of the data PVC: the `metrics.volume` of the release/storage-sizes.yaml row spec.storage.size picks."
  type        = string
}

variable "retention_size" {
  description = "TSDB retention.size, in Prometheus units such as 3GB: the row's `metrics.retention_size`, below the volume because compaction can briefly exceed it."
  type        = string

  validation {
    condition     = can(regex("^[1-9][0-9]*(KB|MB|GB|TB)$", var.retention_size))
    error_message = "retention_size is a positive size such as 3GB; 0 would leave the volume to fill."
  }
}

variable "stage_namespaces" {
  description = "Stage name to namespace. A series from one of these namespaces is labelled with its stage, and kube-state-metrics watches each of them through a Role there."
  type        = map(string)
}

variable "trino_targets" {
  description = "Stage name to its Trino coordinator's host:port; scraped at /metrics with a user name and no password."
  type        = map(string)

  validation {
    condition     = length(var.trino_targets) <= 3
    error_message = "At most three coordinators, one per stage (ADR 0024, Decision 3: target_limit 3)."
  }
}

variable "object_store_targets" {
  description = "SeaweedFS process (master, volume, filer, s3) to its metrics host:port."
  type        = map(string)

  validation {
    condition     = length(var.object_store_targets) <= 4 && alltrue([for service in keys(var.object_store_targets) : contains(["master", "volume", "filer", "s3"], service)])
    error_message = "object_store_targets names only master, volume, filer and s3 (ADR 0024, Decision 3: the service label)."
  }
}

variable "bucket_stages" {
  description = "Bucket name to the stage its SeaweedFS series are labelled with: each stage's buckets to the stage, the ops bucket to platform."
  type        = map(string)
}

variable "kubelet_insecure_skip_verify" {
  description = "Skip the kubelet certificate check. Only for kind, whose kubelets serve self-signed certificates (ADR 0024, Decision 5 and open item 8); every other provider verifies."
  type        = bool
  default     = false
}
