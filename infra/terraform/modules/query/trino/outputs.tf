output "service_name" {
  description = "Trino coordinator service name, available once the release is ready."
  # From the release rather than the variable, so a consumer waits for Trino.
  value = helm_release.trino.name
}

output "http_port" {
  description = "Trino HTTP service port."
  value       = 8080
}

output "route_port" {
  description = "Port the ingress route reaches Trino on: the HTTPS port when workload authentication is on, since the HTTP listener is then disabled."
  value       = local.auth_enabled ? 8443 : 8080
}

output "traefik_backends" {
  description = "Backend transports Traefik needs to re-encrypt to Trino, keyed by transport name: the Secret holding the CA and the server name to verify. Empty when workload authentication is off."
  value = {
    for name in(local.auth_enabled ? [local.traefik_transport] : []) : name => {
      ca_secret   = local.tls_ca_secret_name
      server_name = "${var.release_name}.${var.namespace}.svc.cluster.local"
    }
  }
  # The Secret exists once the certificate release has been applied.
  depends_on = [helm_release.certificate]
}
