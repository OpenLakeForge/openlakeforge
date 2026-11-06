resource "kubernetes_job_v1" "metastore_bootstrap" {
  metadata {
    name      = "polaris-metastore-bootstrap-${helm_release.polaris.metadata.revision}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "catalog-metastore-bootstrap"
          "openlakeforge.io/readiness" = "required"
        })
        annotations = local.metastore_bootstrap_annotations
      }

      spec {
        restart_policy = "Never"

        container {
          name  = "metastore-bootstrap"
          image = var.metastore_bootstrap_job_image

          command = ["/bin/sh", "-ec"]
          args = [templatefile("${path.module}/templates/metastore-bootstrap.sh.tftpl", {
            release_name = var.release_name
            oauth_scope  = local.oauth_scope
          })]

          env {
            name  = "POLARIS_PERSISTENCE_TYPE"
            value = "relational-jdbc"
          }

          env {
            name  = "POLARIS_REALM"
            value = local.realm
          }

          env {
            name = "POLARIS_BOOTSTRAP_CREDENTIALS"
            value_from {
              secret_key_ref {
                name = var.bootstrap_secret_name
                key  = "POLARIS_BOOTSTRAP_CREDENTIALS"
              }
            }
          }

          env {
            name = "ROOT_CLIENT_ID"
            value_from {
              secret_key_ref {
                name = var.bootstrap_secret_name
                key  = "ROOT_CLIENT_ID"
              }
            }
          }

          env {
            name = "ROOT_CLIENT_SECRET"
            value_from {
              secret_key_ref {
                name = var.bootstrap_secret_name
                key  = "ROOT_CLIENT_SECRET"
              }
            }
          }

          env {
            name = "QUARKUS_DATASOURCE_USERNAME"
            value_from {
              secret_key_ref {
                name = var.postgresql_contract.polaris_credentials_secret_name
                key  = "username"
              }
            }
          }

          env {
            name = "QUARKUS_DATASOURCE_PASSWORD"
            value_from {
              secret_key_ref {
                name = var.postgresql_contract.polaris_credentials_secret_name
                key  = "password"
              }
            }
          }

          env {
            name = "QUARKUS_DATASOURCE_JDBC_URL"
            value_from {
              secret_key_ref {
                name = var.postgresql_contract.polaris_credentials_secret_name
                key  = "jdbcUrl"
              }
            }
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
            limits = {
              memory = "512Mi"
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
    update = "5m"
  }

  depends_on = [
    helm_release.polaris,
  ]

  lifecycle {
    replace_triggered_by = [
      terraform_data.polaris_release_revision,
    ]
  }
}
