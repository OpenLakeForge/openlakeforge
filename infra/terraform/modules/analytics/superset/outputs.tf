output "namespace" {
  description = "Namespace this Superset instance is deployed in."
  value       = var.namespace
}

output "contract" {
  description = "Superset reporting contract consumed by local scripts and future modules."
  value = {
    service_name = var.release_name
    http_port    = var.http_port
  }

  depends_on = [
    helm_release.superset,
  ]
}

output "service_name" {
  description = "Superset web service name."
  value       = var.release_name
}

output "http_port" {
  description = "Superset HTTP service port."
  value       = var.http_port
}
