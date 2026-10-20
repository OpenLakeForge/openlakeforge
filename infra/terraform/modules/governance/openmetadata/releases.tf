locals {
  deps_chart      = var.deps_chart_package_path != null ? var.deps_chart_package_path : "openmetadata-dependencies"
  deps_repository = var.deps_chart_package_path != null ? null : var.chart_repository
  deps_version    = var.deps_chart_package_path != null ? null : var.deps_chart_version
  chart           = var.chart_package_path != null ? var.chart_package_path : "openmetadata"
  repository      = var.chart_package_path != null ? null : var.chart_repository
  version         = var.chart_package_path != null ? null : var.chart_version
}

# OpenSearch via the openmetadata-dependencies chart (Airflow + MySQL disabled)
resource "helm_release" "openmetadata_deps" {
  name       = "${var.release_name}-deps"
  repository = local.deps_repository
  chart      = local.deps_chart
  version    = local.deps_version
  namespace  = var.namespace

  wait    = true
  timeout = 1200

  values = [
    file(var.deps_values_file),
  ]
}

# Main OpenMetadata application
resource "helm_release" "openmetadata" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  wait            = true
  timeout         = 600
  cleanup_on_fail = true
  upgrade_install = true

  values = concat([
    file(var.base_values_file),
    yamlencode({
      openmetadata = {
        config = {
          database = {
            host         = var.postgresql_contract.host
            port         = var.postgresql_contract.port
            driverClass  = "org.postgresql.Driver"
            dbScheme     = "postgresql"
            databaseName = var.postgresql_contract.openmetadata_db_name
            auth = {
              username = var.postgresql_contract.openmetadata_db_user
              password = {
                secretRef = var.postgresql_contract.openmetadata_credentials_secret_name
                secretKey = "postgresql-password"
              }
            }
            dbParams = "sslmode=${local.postgresql_ssl_mode}"
          }
          pipelineServiceClientConfig = {
            k8s = {
              namespace = var.namespace
            }
          }
        }
      }
    }),
    ], var.oidc == null ? [] : [
    # The JVM does not read the system CA store, so a copy of its cacerts with
    # the local CA added is built before the server (and its migration) start.
    # OpenMetadata reaches the issuer's discovery, token and keys URLs with it.
    # The provider itself is switched by `olf openmetadata deploy-metadata`, not here (main.tf).
    yamlencode({
      extraVolumes = [
        { name = "olf-oidc-ca", configMap = { name = var.oidc.ca_configmap_name } },
        { name = "olf-java-trust", emptyDir = {} },
      ]
      extraVolumeMounts = [
        { name = "olf-java-trust", mountPath = "/etc/olf/java-trust", readOnly = true },
        { name = "olf-oidc-ca", mountPath = "/etc/olf/oidc-ca", readOnly = true }, # olf's issuer probe
      ]
      extraEnvs = [{
        name  = "JAVA_TOOL_OPTIONS"
        value = "-Djavax.net.ssl.trustStore=/etc/olf/java-trust/cacerts -Djavax.net.ssl.trustStorePassword=changeit"
      }]
      preMigrateInitContainers = [{
        name            = "olf-java-trust"
        image           = "docker.getcollate.io/openmetadata/server:${var.chart_version}"
        imagePullPolicy = "IfNotPresent"
        command         = ["/bin/sh", "-ec"]
        args = [<<-EOT
          jhome="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
          cp "$jhome/lib/security/cacerts" /etc/olf/java-trust/cacerts
          chmod u+w /etc/olf/java-trust/cacerts
          keytool -importcert -noprompt -alias olf-local-ca -file /etc/olf/oidc-ca/ca.crt \
            -keystore /etc/olf/java-trust/cacerts -storepass changeit
        EOT
        ]
        volumeMounts = [
          { name = "olf-oidc-ca", mountPath = "/etc/olf/oidc-ca", readOnly = true },
          { name = "olf-java-trust", mountPath = "/etc/olf/java-trust" },
        ]
      }]
    }),
  ])

  depends_on = [
    helm_release.openmetadata_deps,
  ]
}

resource "terraform_data" "openmetadata_release_revision" {
  triggers_replace = [
    helm_release.openmetadata.metadata.revision,
  ]
}

resource "terraform_data" "openmetadata_catalog_schemas" {
  triggers_replace = [
    local.catalog_database_name,
    sha256(local.catalog_schema_names_json),
  ]
}
