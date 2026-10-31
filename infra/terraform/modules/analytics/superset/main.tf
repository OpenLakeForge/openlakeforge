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
    sign_out_url = var.oidc.sign_out_url
  }

  # The platform API's service role (ADR 0021, open item 3, checked against
  # Superset 6.1.0): import assets, read and write datasets, charts and
  # dashboards, export dashboards, read databases and its own roles. No Admin, no SQL Lab, no role
  # management, and no write on databases: the API imports sparsely and never
  # sends a database. Export also needs data access, which the artifacts phase
  # grants this role beside olf_data_access. `fab import-roles` only adds, so a
  # permission dropped from this list stays granted until revoked by hand.
  platform_api_role = "olf_platform_api"
  platform_api_roles_json = jsonencode([{
    name = local.platform_api_role
    permissions = [for pair in [
      ["can_import_", "ImportExportRestApi"],
      ["can_read", "Dataset"], ["can_write", "Dataset"],
      ["can_read", "Chart"], ["can_write", "Chart"],
      ["can_read", "Dashboard"], ["can_write", "Dashboard"], ["can_export", "Dashboard"],
      ["can_read", "Database"],
      # Its own roles (/api/v1/me/roles/), to check a report's schemas were granted before importing it.
      ["can_read", "CurrentUserRestApi"],
    ] : { permission = { name = pair[0] }, view_menu = { name = pair[1] } }]
  }])
  platform_api_init = var.platform_api_user == null ? "" : <<-SH
    cat > /tmp/olf-platform-api-role.json <<'JSON'
    ${local.platform_api_roles_json}
    JSON
    superset fab import-roles --path /tmp/olf-platform-api-role.json
    superset fab create-user --username "${var.platform_api_user.username}" --firstname OpenLakeForge \
      --lastname "Platform API" --email "${var.platform_api_user.username}@openlakeforge.invalid" \
      --role ${local.platform_api_role} --password "$PLATFORM_API_PASSWORD" || true
    superset fab reset-password --username "${var.platform_api_user.username}" --password "$PLATFORM_API_PASSWORD"
  SH
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
      # Unset values fall through to the release pin in the base values file.
      image = { for key, value in {
        repository = var.image_repository
        tag        = var.image_tag
        pullPolicy = var.image_pull_policy
      } : key => value if value != null }

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
        # The chart's script puts the admin password into the init Job's
        # Secret, so this one reads it from the environment. Resetting it on
        # every run makes the Secret authoritative for an existing admin too.
        initscript = <<-SH
          #!/bin/sh
          set -eu
          superset db upgrade
          superset init
          superset fab create-admin --username "${var.admin_username}" --firstname OpenLakeForge --lastname Admin \
            --email "${var.admin_email}" --password "$ADMIN_PASSWORD" || true
          superset fab reset-password --username "${var.admin_username}" --password "$ADMIN_PASSWORD"
          ${local.platform_api_init}
        SH
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
        {
          name = "SUPERSET_SECRET_KEY"
          valueFrom = {
            secretKeyRef = {
              name = local.credentials_secret_name
              key  = "secret-key"
            }
          }
        },
        {
          name = "ADMIN_PASSWORD"
          valueFrom = {
            secretKeyRef = {
              name = local.credentials_secret_name
              key  = "admin-password"
            }
          }
        },
        ], var.platform_api_user == null ? [] : [
        {
          name = "PLATFORM_API_PASSWORD"
          valueFrom = {
            secretKeyRef = {
              name = var.platform_api_user.secret_name
              key  = "password"
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
    }),
    ], length(local.trust_volumes) == 0 ? [] : [
    yamlencode({
      extraVolumes      = [for v in local.trust_volumes : { name = v.name, configMap = { name = v.configmap } }]
      extraVolumeMounts = [for v in local.trust_volumes : { name = v.name, mountPath = v.path, readOnly = true }]
    })
    ], var.oidc != null || !var.public_admin ? [] : [
    # Authentication off (no spec.identity; local-only, enforced by profile
    # validation). Not derived from oidc == null: an external issuer also
    # deploys no oidc here but does authenticate. Every visitor is signed in as
    # the local admin; an anonymous Admin role is not enough, since Superset
    # needs a real user for the welcome page and as the owner of saved objects.
    yamlencode({
      configOverrides = {
        openlakeforge_public_admin = <<-EOT
          def FLASK_APP_MUTATOR(app):
              from flask_login import current_user, login_user

              @app.before_request
              def _olf_sign_in_as_local_admin():
                  if not current_user.is_authenticated:
                      user = app.appbuilder.sm.find_user(username="${var.admin_username}")
                      if user is not None:
                          login_user(user)
        EOT
      }
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

  depends_on = [kubernetes_job_v1.oidc_secret, kubernetes_job_v1.credentials]
}
