output "service_name" {
  description = "Trino coordinator service name, available once the release is ready."
  # From the release rather than the variable, so a consumer waits for Trino.
  value = helm_release.trino.name
}

output "http_port" {
  description = "Trino HTTP service port."
  value       = 8080
}
