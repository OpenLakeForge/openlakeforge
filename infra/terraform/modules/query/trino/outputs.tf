output "service_name" {
  description = "Trino coordinator service name, available once the release is ready."
  # From the release rather than the variable, so a consumer waits for Trino.
  value = helm_release.trino.name
}

output "http_port" {
  description = "Trino HTTP service port, which callers and the ingress route use with or without workload authentication."
  value       = 8080
}

output "credential_secret_name" {
  description = "Secret each stage namespace holds with its own Trino username and password (ADR 0016); null unless password authentication is on."
  value       = local.auth_enabled ? "trino-credential" : null
}
