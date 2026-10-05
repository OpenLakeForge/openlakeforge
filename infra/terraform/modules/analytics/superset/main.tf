locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "superset"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
}

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

resource "helm_release" "superset" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  # Keep Terraform aligned with the web, worker, Redis, and init hook readiness
  # reported by Helm.
  wait            = true
  timeout         = 900
  cleanup_on_fail = true
  upgrade_install = true

  values = [
    file(var.base_values_file),
    yamlencode({
      image = {
        repository = var.image_repository
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      }

      postgresql = {
        enabled = false
      }

      service = {
        type = "ClusterIP"
        port = var.http_port
      }

      supersetNode = {
        connections = {
          db_type  = "postgresql"
          db_host  = var.postgresql_contract.host
          db_port  = tostring(var.postgresql_contract.port)
          db_user  = var.postgresql_contract.superset_db_user
          db_pass  = "managed-by-${var.postgresql_contract.superset_credentials_secret_name}"
          db_name  = var.postgresql_contract.superset_db_name
          db_extra = var.postgresql_ssl_mode == "disable" ? "" : "?sslmode=${var.postgresql_ssl_mode}"
        }
      }

      init = {
        createAdmin = true
        jobAnnotations = {
          "helm.sh/hook"               = "post-install,post-upgrade"
          "helm.sh/hook-delete-policy" = "before-hook-creation,hook-succeeded"
        }
        adminUser = {
          username  = var.admin_username
          firstname = "OpenLakeForge"
          lastname  = "Admin"
          email     = var.admin_email
          password  = var.admin_password
        }
      }

      extraEnvRaw = [
        {
          name = "DB_PASS"
          valueFrom = {
            secretKeyRef = {
              name = var.postgresql_contract.superset_credentials_secret_name
              key  = "postgresql-password"
            }
          }
        },
      ]

      extraSecretEnv = {
        SUPERSET_SECRET_KEY = random_password.secret_key.result
      }
    }),
  ]
}
