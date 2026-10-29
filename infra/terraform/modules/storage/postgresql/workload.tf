# Init script runs on first start and creates the additional users + databases.
# Uses postgres:16-alpine's /docker-entrypoint-initdb.d/ hook (shell files are executed
# as the postgres superuser before the server becomes available to clients).
resource "kubernetes_config_map_v1" "init_scripts" {
  metadata {
    name      = "${var.release_name}-init"
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "init.sh" = local.bootstrap_script
  }
}

resource "kubernetes_stateful_set_v1" "postgresql" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    selector {
      match_labels = local.pod_labels
    }
    service_name = var.release_name
    replicas     = 1

    template {
      metadata {
        labels = local.pod_labels
      }

      spec {
        container {
          name  = "postgresql"
          image = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"

          port {
            container_port = 5432
            name           = "postgresql"
          }

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
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
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
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }

          volume_mount {
            name       = "init-scripts"
            mount_path = "/docker-entrypoint-initdb.d"
          }

          readiness_probe {
            exec {
              command = ["pg_isready", "-U", "postgres"]
            }
            initial_delay_seconds = 5
            period_seconds        = 10
            failure_threshold     = 6
          }

          liveness_probe {
            exec {
              command = ["pg_isready", "-U", "postgres"]
            }
            initial_delay_seconds = 30
            period_seconds        = 30
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

        volume {
          name = "init-scripts"
          config_map {
            name         = kubernetes_config_map_v1.init_scripts.metadata[0].name
            default_mode = "0755"
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name   = "data"
        labels = local.labels
      }

      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = var.storage_class_name

        resources {
          requests = {
            storage = var.storage_size
          }
        }
      }
    }
  }

  wait_for_rollout = true

  depends_on = [kubernetes_job_v1.credentials]
}
