resource "kubernetes_job_v1" "bootstrap" {
  metadata {
    name      = local.bootstrap_job_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "governance-bootstrap"
          "openlakeforge.io/readiness" = "required"
        })
        annotations = local.bootstrap_annotations
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.bootstrap.metadata[0].name

        container {
          name  = "bootstrap"
          image = var.bootstrap_job_image

          command = ["/bin/sh", "-ec"]
          args    = [local.bootstrap_script]

          env {
            name = "NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
            }
          }

          # Written by the job itself on the run that turns SSO on; absent
          # before that, and basic login is used instead.
          env {
            name = "AUTOMATION_JWT"
            value_from {
              secret_key_ref {
                name     = var.automation_secret_name
                key      = var.automation_jwt_key
                optional = true
              }
            }
          }

          dynamic "env" {
            for_each = var.oidc == null ? [] : [var.oidc.secret_ref]
            content {
              name = "OIDC_CLIENT_SECRET"
              value_from {
                secret_key_ref {
                  name = env.value.name
                  key  = env.value.key
                }
              }
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "15m"
    update = "15m"
  }

  depends_on = [
    helm_release.openmetadata,
    kubernetes_role_binding_v1.bootstrap,
  ]

  lifecycle {
    replace_triggered_by = [
      terraform_data.openmetadata_release_revision,
      terraform_data.openmetadata_catalog_schemas,
    ]
  }
}

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
