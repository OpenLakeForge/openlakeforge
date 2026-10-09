output "service_name" {
  description = "Keycloak Service the ingress routes to."
  value       = kubernetes_service_v1.keycloak.metadata[0].name
}

output "http_port" {
  description = "Port of the Keycloak Service."
  value       = 8080
}

output "admin_secret_name" {
  description = "Secret holding the bootstrap admin username and password."
  value       = kubernetes_secret_v1.admin.metadata[0].name
}

# The identity.oidc fields of the provider contract (ADR 0014). References
# only: a client's secret is named by Secret and key, never carried.
output "contract" {
  description = "identity.oidc contract fields. Available once the realm Job has completed."
  value = {
    issuer_url = local.issuer
    role_claim = var.role_claim
    # Groups are named exactly as the canonical roles.
    role_mapping = { for role in var.roles : role => [role] }
    clients = {
      for name in keys(var.clients) : name => {
        client_id  = name
        secret_ref = { name = kubernetes_secret_v1.client[name].metadata[0].name, key = "client-secret" }
      }
    }
    adapter = "keycloak"
    # admin_api becomes true when `olf users` lands (#331).
    capabilities = { admin_api = false, email_delivery = var.smtp != null, groups_in_token = true, logout_endpoint = true }
  }

  depends_on = [kubernetes_job_v1.realm]
}
