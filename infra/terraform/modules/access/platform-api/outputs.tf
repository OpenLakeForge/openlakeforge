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

# Read through this output, Superset's init script runs after the Job that puts
# the password in its namespace.
output "superset_user" {
  description = "The Superset service user each stage's init script creates: its username and the Secret in the stage namespace that holds its password; null without superset_service_users."
  value = length(var.superset_service_users) == 0 ? null : {
    username    = local.superset_username
    secret_name = local.superset_stage_secret
  }
  depends_on = [kubernetes_job_v1.superset_users]
}
