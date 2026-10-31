variable "stage_namespaces" {
  description = "Stage name to namespace name; each gets default-deny plus the stage rows of the flow table."
  type        = map(string)
}

variable "flows" {
  description = "Rows of release/network-flows.yaml, as decoded by yamldecode, plus the profile-driven rows of the root. Rows reach a stage namespace or one of the workloads below; others are ignored here. A stage row that names pods may carry `stages`, the stage names it is rendered in."
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

variable "node_cidrs" {
  description = "Addresses (/32) of the cluster nodes, for rows whose peer scope is node (F21: the API server calls the cert-manager webhook from a node)."
  type        = list(string)
}

variable "internal_cidrs" {
  description = "Pod and Service ranges excluded from the any scope, so a row for external hosts cannot reach a pod by IP. kind's defaults, which infra/kind/local/kind-cluster.yaml does not override."
  type        = list(string)
  default     = ["10.244.0.0/16", "10.96.0.0/16"]
}
