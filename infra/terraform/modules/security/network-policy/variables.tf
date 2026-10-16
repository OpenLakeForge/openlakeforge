variable "stage_namespaces" {
  description = "Stage name to namespace name; each gets default-deny plus the stage rows of the flow table."
  type        = map(string)
}

variable "flows" {
  description = "Rows of release/network-flows.yaml, as decoded by yamldecode. Rows with neither endpoint in a stage namespace are ignored here."
  type        = any
}

variable "apiserver_cidrs" {
  description = "Addresses (/32) of the kubernetes Service and the API server endpoints, for rows whose destination scope is apiserver (F20)."
  type        = list(string)
}
