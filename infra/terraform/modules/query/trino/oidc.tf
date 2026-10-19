# Web UI sign-in through the identity.oidc issuer (ADR 0016, #26). Only the UI
# changes: clients of /v1 keep the workload authentication of the same ADR,
# because `web-ui.authentication.type` is independent of
# `http-server.authentication.type`.
#
# Trino's UI filter answers a request that is not secure with a redirect to its
# "UI disabled" page, so the route must be HTTPS at the ingress (Trino reads
# X-Forwarded-Proto through process-forwarded). The caller passes null for a
# plain-HTTP route.
#
# The endpoints are set instead of discovered: Trino reads the issuer's
# metadata at startup and gives up after 30 seconds, and the issuer's public
# URL is served by Traefik, which is created after Trino. These paths are
# Dex's own, so the module takes a Dex issuer.
locals {
  oidc_enabled = var.web_ui_oidc != null
  oidc_prefix  = "http-server.authentication.oauth2"
  oidc_properties = !local.oidc_enabled ? [] : [
    "web-ui.authentication.type=oauth2",
    "${local.oidc_prefix}.issuer=${var.web_ui_oidc.issuer_url}",
    "${local.oidc_prefix}.oidc.discovery=false",
    "${local.oidc_prefix}.auth-url=${var.web_ui_oidc.issuer_url}/auth",
    "${local.oidc_prefix}.token-url=${var.web_ui_oidc.issuer_url}/token",
    "${local.oidc_prefix}.jwks-url=${var.web_ui_oidc.issuer_url}/keys",
    "${local.oidc_prefix}.client-id=${var.web_ui_oidc.client_id}",
    "${local.oidc_prefix}.client-secret=$${ENV:TRINO_OIDC_CLIENT_SECRET}",
    # Dex puts `email` in a token only when the scope asks for it.
    "${local.oidc_prefix}.scopes=openid,email",
    "${local.oidc_prefix}.principal-field=email",
    # The issuer's certificate is signed by the local CA. This HTTP client
    # fetches the signing keys and exchanges the code; it is separate from the
    # internal-communication one.
    "oauth2-jwk.http-client.trust-store-path=/etc/trino/tls/ca.crt",
  ]
  oidc_env = !local.oidc_enabled ? [] : [
    {
      name = "TRINO_OIDC_CLIENT_SECRET"
      valueFrom = {
        secretKeyRef = {
          name = var.web_ui_oidc.secret_ref.name
          key  = var.web_ui_oidc.secret_ref.key
        }
      }
    },
  ]
}
