variable "namespace" {
  description = "Namespace of the collector DaemonSet (the platform namespace, olf-system)."
  type        = string
}

variable "release_name" {
  description = "Name of the DaemonSet, ServiceAccount, ClusterRole and ConfigMap."
  type        = string
  default     = "log-collector"
}

variable "image" {
  description = "Fluent Bit image, pinned by digest (release/component-catalog.yaml, fluent_bit)."
  type        = string
  nullable    = false
  default     = "fluent/fluent-bit:5.2.0@sha256:a48190bf67679495a332fd61025421a46e7e4d168380185a8e16da2e9acc3126"
}

variable "routes" {
  description = "One entry per collected namespace (ADR 0022, decision 3), keyed by the stage label written into each record (dev, uat, prod, platform). key_prefix is the archive's v2 prefix inside the bucket and ends in '/'."
  type = map(object({
    namespace  = string
    key_prefix = string
  }))

  validation {
    condition     = alltrue([for route in values(var.routes) : endswith(route.key_prefix, "/") && !startswith(route.key_prefix, "/")])
    error_message = "Each key_prefix is a key prefix ending in '/', matching the write grant the object store holds for the collector."
  }
  validation {
    condition     = length(distinct([for route in values(var.routes) : route.namespace])) == length(var.routes)
    error_message = "A namespace is collected into one route only: the namespace picks the route (ADR 0022, decision 3)."
  }
}

variable "bucket" {
  description = "The ops bucket the archive is written to."
  type        = string
}

variable "s3_endpoint_url" {
  description = "S3 endpoint of the object store, from its provider contract."
  type        = string
}

variable "s3_region" {
  description = "S3 region of the object store."
  type        = string
}

variable "credentials_secret_name" {
  description = "Secret in the collector's namespace with AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, the write-only key scoped to the routes' prefixes. Passed to the pod by reference, never as a value."
  type        = string
}

variable "mem_buf_limit" {
  description = "Mem_Buf_Limit of each tail input: the memory its chunks may hold before the input pauses."
  type        = string
  default     = "16MB"
}

variable "max_chunks_up" {
  description = "storage.max_chunks_up: chunks the engine keeps in memory in total (about 1 MB each); the rest stay on disk."
  type        = number
  default     = 32
}

variable "chunk_limit_size" {
  description = "storage.total_limit_size of each output: disk the engine's queued chunks may use."
  type        = string
  default     = "256M"
}

variable "spool_limit_size" {
  description = "store_dir_limit_size of each S3 output: the S3 plugin's own spool, the real loss point of an object-store outage (ADR 0022, decision 2). Never 0, which is unlimited."
  type        = string
  default     = "512M"

  validation {
    condition     = can(regex("^[1-9][0-9]*[KMG]$", var.spool_limit_size))
    error_message = "spool_limit_size is a positive size such as 512M; 0 would leave the spool unbounded."
  }
}

variable "quarantine_limit_size" {
  description = "quarantine_dir_limit_size of each S3 output: disk for files whose upload retries are exhausted, kept for inspection. They leave the spool budget, so this bounds them separately; at the limit further exhausted files are deleted. Never 0, which is unlimited."
  type        = string
  default     = "128M"

  validation {
    condition     = can(regex("^[1-9][0-9]*[KMG]$", var.quarantine_limit_size))
    error_message = "quarantine_limit_size is a positive size such as 128M; 0 would leave the quarantine unbounded."
  }
}

variable "memory_limit" {
  description = "Memory limit of the collector container; must cover mem_buf_limit per route, the emitter buffers and max_chunks_up."
  type        = string
  default     = "256Mi"
}

variable "host_state_path" {
  description = "Node directory for tail offsets, engine chunks and the S3 spool, so a replaced pod resumes where it stopped."
  type        = string
  default     = "/var/lib/openlakeforge/log-collector"
}
