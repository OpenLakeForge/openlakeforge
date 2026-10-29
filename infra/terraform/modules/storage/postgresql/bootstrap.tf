resource "kubernetes_job_v1" "bootstrap" {
  metadata {
    name      = "${var.release_name}-bootstrap-${local.bootstrap_hash}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.pod_labels, {
          "openlakeforge.io/job"       = "postgresql-bootstrap"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy = "Never"

        container {
          name  = "bootstrap"
          image = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"

          command = ["/bin/sh", "-ec"]
          args = [<<-SCRIPT
            until pg_isready -h "${local.host}" -p "${local.port}" -U "$POSTGRES_USER"; do
              sleep 2
            done

            /bootstrap/init.sh
          SCRIPT
          ]

          env {
            name  = "POSTGRES_USER"
            value = "postgres"
          }
          env {
            name = "POSTGRES_PASSWORD"
            value_from {
              secret_key_ref {
                name = local.admin_secret_name
                key  = "postgres-password"
              }
            }
          }
          env {
            name = "PGPASSWORD"
            value_from {
              secret_key_ref {
                name = local.admin_secret_name
                key  = "postgres-password"
              }
            }
          }
          env {
            name  = "PGHOST"
            value = local.host
          }
          env {
            name  = "PGPORT"
            value = tostring(local.port)
          }
          dynamic "env" {
            for_each = local.database_env_values
            content {
              name  = env.key
              value = env.value
            }
          }
          dynamic "env" {
            for_each = local.database_env_passwords
            content {
              name = env.key
              value_from {
                secret_key_ref {
                  name = local.databases_by_key[env.value].credentials_secret_name
                  key  = "postgresql-password"
                }
              }
            }
          }
          env {
            name  = "POLARIS_DB_USER"
            value = var.polaris_db_user
          }
          env {
            name = "POLARIS_DB_PASSWORD"
            value_from {
              secret_key_ref {
                name = var.polaris_credentials_secret_name
                key  = "password"
              }
            }
          }
          env {
            name  = "POLARIS_DB_NAME"
            value = var.polaris_db_name
          }

          volume_mount {
            name       = "init-scripts"
            mount_path = "/bootstrap"
            read_only  = true
          }
        }

        volume {
          name = "init-scripts"
          config_map {
            name         = kubernetes_config_map_v1.init_scripts.metadata[0].name
            default_mode = "0755"
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
    kubernetes_stateful_set_v1.postgresql,
    kubernetes_service_v1.postgresql,
  ]
}
