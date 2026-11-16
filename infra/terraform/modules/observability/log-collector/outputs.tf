output "daemon_set_name" {
  description = "Name of the collector DaemonSet."
  value       = kubernetes_daemon_set_v1.collector.metadata[0].name
}

output "collected_namespaces" {
  description = "Namespaces whose container logs are collected, by stage label."
  value       = { for stage, route in var.routes : stage => route.namespace }
}
