output "ingress_class_name" {
  description = "IngressClass Traefik serves, available once the release is ready."
  # The chart names its IngressClass after the release.
  value = helm_release.traefik.name
}

output "test_edge_service_name" {
  description = "Service of the e2e edge that answers HTTPS for the base domain; null unless test_edge is set."
  value       = one(helm_release.edge[*].name)
}
