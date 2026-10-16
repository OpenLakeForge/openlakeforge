output "service_name" {
  description = "Dex Service the ingress routes to."
  value       = kubernetes_service_v1.dex.metadata[0].name
}

output "http_port" {
  description = "Port of the Dex Service."
  value       = 5556
}

# The identity.oidc fields of the provider contract (ADR 0014). References
# only: a client's secret is named by Secret and key, never carried.
output "contract" {
  description = "identity.oidc contract fields. Available once Dex has rolled out."
  value = {
    issuer_url   = local.issuer
    role_claim   = "groups"
    role_mapping = local.role_mapping
    clients = {
      for name in keys(var.clients) : name => {
        client_id  = name
        secret_ref = { name = local.client_secret_name[name], key = "client-secret" }
      }
    }
    adapter = "dex"
    # Dex has no end-session endpoint and sends no mail (ADR 0014).
    capabilities = { email_delivery = false, groups_in_token = true, logout_endpoint = false }
  }

  depends_on = [kubernetes_deployment_v1.dex]
}
