# The SSO security configuration, minus the client secret, for
# `olf infra openmetadata deploy-metadata` to apply once the issuer resolves (#432).
resource "kubernetes_config_map_v1" "sso_config" {
  count = var.oidc == null ? 0 : 1

  metadata {
    name      = "openmetadata-sso-config" # olf.infra.openmetadata._sso.CONFIGMAP
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "config.json" = jsonencode(local.oidc_security_config)
    secret_name   = var.oidc.secret_ref.name
    secret_key    = var.oidc.secret_ref.key
  }
}
