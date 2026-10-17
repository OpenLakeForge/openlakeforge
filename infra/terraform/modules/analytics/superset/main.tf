locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "superset"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version

  trust_volumes = concat(
    var.trino_ca_configmap_name == null ? [] : [{ name = "query-ca", path = "/etc/olf/query-ca", configmap = var.trino_ca_configmap_name }],
    var.oidc == null ? [] : [{ name = "oidc-ca", path = "/etc/olf/oidc-ca", configmap = var.oidc.ca_configmap_name }],
  )

  # Non-secret contract fields; olf_oidc.settings turns them into Superset
  # config in the image. The client secret arrives through the environment.
  oidc_config = var.oidc == null ? null : {
    issuer_url   = var.oidc.issuer_url
    client_id    = var.oidc.client_id
    scope        = var.oidc.scope
    role_claim   = var.oidc.role_claim
    role_mapping = var.oidc.role_mapping
    grants       = var.oidc.grants
  }
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

  values = concat([
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

      extraEnvRaw = concat([
        {
          name = "DB_PASS"
          valueFrom = {
            secretKeyRef = {
              name = var.postgresql_contract.superset_credentials_secret_name
              key  = "postgresql-password"
            }
          }
        },
        ], var.oidc == null ? [] : [
        {
          name = "OIDC_CLIENT_SECRET"
          valueFrom = {
            secretKeyRef = {
              name = var.oidc.secret_ref.name
              key  = var.oidc.secret_ref.key
            }
          }
        },
      ])

      extraSecretEnv = {
        SUPERSET_SECRET_KEY = random_password.secret_key.result
      }
    }),
    ], length(local.trust_volumes) == 0 ? [] : [
    yamlencode({
      extraVolumes      = [for v in local.trust_volumes : { name = v.name, configMap = { name = v.configmap } }]
      extraVolumeMounts = [for v in local.trust_volumes : { name = v.name, mountPath = v.path, readOnly = true }]
    })
    ], var.oidc == null ? [] : [
    yamlencode({
      configOverrides = {
        openlakeforge_oidc = <<-EOT
          import json, os, sys
          import certifi
          # The issuer's certificate is signed by the local CA. REQUESTS_CA_BUNDLE
          # replaces the trust store of every requests call, so the local CA is
          # appended to the public roots rather than standing alone: alerts and
          # reports still reach public HTTPS endpoints.
          with open("/tmp/olf-ca-bundle.pem", "w") as bundle:
              bundle.write(open(certifi.where()).read() + "\n" + open("/etc/olf/oidc-ca/ca.crt").read())
          os.environ["REQUESTS_CA_BUNDLE"] = "/tmp/olf-ca-bundle.pem"
          sys.path.insert(0, "/app/olf")
          from olf_oidc import settings
          from olf_security import OlfSecurityManager, enforce_session_bound
          globals().update(settings(json.loads(r'''${jsonencode(local.oidc_config)}'''), os.environ["OIDC_CLIENT_SECRET"]))
          CUSTOM_SECURITY_MANAGER = OlfSecurityManager
          FLASK_APP_MUTATOR = enforce_session_bound
        EOT
      }
    }),
  ])

  depends_on = [kubernetes_job_v1.oidc_secret]
}
