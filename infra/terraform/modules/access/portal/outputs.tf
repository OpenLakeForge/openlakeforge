output "service_name" {
  description = "Service that fronts the portal."
  value       = var.release_name
}

output "service_port" {
  description = "Port the portal Service listens on."
  value       = local.port
}
