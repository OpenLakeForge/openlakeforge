variable "cluster_name" {
  description = "Name of the local kind cluster managed by the foundation root."
  type        = string
  default     = "openlakeforge-local"
}

variable "cluster_config_path" {
  description = "Path to the kind cluster configuration file. Defaults to infra/kind/local/kind-cluster.yaml."
  type        = string
  default     = null
}

variable "kubeconfig_path" {
  description = "Kubeconfig path populated by kind. Defaults to the repository-local .tmp/kubeconfigs/local.yaml."
  type        = string
  default     = null
}

variable "kind_wait_timeout" {
  description = "Timeout passed to kind create cluster --wait."
  type        = string
  default     = "120s"
}

variable "reset_existing_cluster" {
  description = "When true during initial creation, delete any existing kind cluster with the same name before recreating it."
  type        = bool
  default     = false
}

variable "kind_executable_path" {
  description = "Absolute path to the kind executable the local-exec provisioner invokes. Empty resolves 'kind' from PATH (host mode, or direct 'terraform apply' outside olf); olf's managed toolchain (#127) always sets this explicitly."
  type        = string
  default     = ""
}

variable "kubectl_executable_path" {
  description = "Absolute path to the kubectl executable the local-exec provisioner invokes. Empty resolves 'kubectl' from PATH (host mode, or direct 'terraform apply' outside olf); olf's managed toolchain (#127) always sets this explicitly."
  type        = string
  default     = ""
}

variable "registry_port" {
  description = "Loopback port for the foundation-owned project image registry."
  type        = number
  default     = 5001
  validation {
    condition     = var.registry_port >= 1024 && var.registry_port <= 65535 && floor(var.registry_port) == var.registry_port
    error_message = "registry_port must be an integer between 1024 and 65535."
  }
}
