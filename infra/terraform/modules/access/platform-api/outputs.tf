output "service_name" {
  description = "Service that fronts the platform API."
  value       = kubernetes_service_v1.platform_api.metadata[0].name
}

output "service_port" {
  description = "Port the platform API Service listens on."
  value       = local.port
}

output "path" {
  description = "Path prefix every route of the API answers under, on the portal's host."
  value       = "/api"
}
