output "query_url" {
  description = "Base URL of the Prometheus HTTP API (prometheus-http-v1), for the provider contract's shared.observability.metrics.query_url."
  value       = "http://${kubernetes_service_v1.prometheus.metadata[0].name}.${var.namespace}.svc:${local.http_port}"
}

output "cluster_role_name" {
  description = "The ClusterRole bound to Prometheus's ServiceAccount (ADR 0017 D5)."
  value       = kubernetes_cluster_role_v1.prometheus.metadata[0].name
}

output "kube_state_metrics_role_namespaces" {
  description = "The namespaces holding kube-state-metrics' Role and RoleBinding (ADR 0017 D5): olf-system and each stage namespace."
  value       = sort([for role in kubernetes_role_v1.kube_state_metrics : role.metadata[0].namespace])
}
