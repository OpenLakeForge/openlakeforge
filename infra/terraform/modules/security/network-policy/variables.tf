variable "stage_namespaces" {
  description = "Stage name to namespace name; each gets default-deny plus the stage rows of the flow table."
  type        = map(string)
}

variable "flows" {
  description = "Rows of release/network-flows.yaml, as decoded by yamldecode. Rows reach a stage namespace or one of the workloads below; others are ignored here."
  type        = any
}

variable "apiserver_cidrs" {
  description = "Addresses (/32) of the kubernetes Service and the API server endpoints, for rows whose destination scope is apiserver (F20)."
  type        = list(string)
}

variable "shared_namespace" {
  description = "Namespace of the shared platform (olf-system); the workloads below live in it."
  type        = string
}

variable "workloads" {
  description = "Workload name to the pod labels that select it in the shared namespace; each gets a default-deny policy plus the flow rows that name it."
  type        = map(map(string))
}
