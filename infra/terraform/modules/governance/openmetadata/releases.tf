locals {
  deps_chart      = var.deps_chart_package_path != null ? var.deps_chart_package_path : "openmetadata-dependencies"
  deps_repository = var.deps_chart_package_path != null ? null : var.chart_repository
  deps_version    = var.deps_chart_package_path != null ? null : var.deps_chart_version
  chart           = var.chart_package_path != null ? var.chart_package_path : "openmetadata"
  repository      = var.chart_package_path != null ? null : var.chart_repository
  version         = var.chart_package_path != null ? null : var.chart_version

  # Jetty's 8 KiB default rejects the request (431) once the perimeter's
  # chunked session cookies and per-request CSRF cookies on .<base_domain> add
  # up. Helm replaces lists, so every extraEnvs entry lives here.
  extra_envs = concat([
    { name = "SERVER_MAX_REQUEST_HEADER_SIZE", value = "64KiB" },
    ], var.oidc == null ? [] : [{
      name  = "JAVA_TOOL_OPTIONS"
      value = "-Djavax.net.ssl.trustStore=/etc/olf/java-trust/cacerts -Djavax.net.ssl.trustStorePassword=changeit"
  }])
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
      extraEnvs = local.extra_envs
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
          fernetkey = {
            secretRef = local.fernet_secret
            secretKey = "fernet-key"
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
    kubernetes_job_v1.fernet_key,
  ]
}

# OpenMetadata encrypts the credentials it stores (the Trino password, ADR 0016)
# with a Fernet key, and the chart's default is public. The key is created
# in-cluster, only if missing, so Terraform never holds it (#181,
# docs/architecture/identity/identity-credentials.md) and a redeploy does not rotate it:
# rotating would make every stored credential undecryptable. A Fernet key is 32
# random bytes, url-safe base64 encoded.
locals {
  fernet_secret = "${var.release_name}-fernet-key"
  fernet_script = <<-SH
    # `create` is the only Secret verb the bootstrap Role grants without a name
    # (ADR 0017), so an existing Secret is detected by the create failing.
    if out="$(kubectl create secret generic "${local.fernet_secret}" -n "$NAMESPACE" \
      --from-literal=fernet-key="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')" 2>&1)"; then
      echo "Created Secret ${local.fernet_secret}"
    elif echo "$out" | grep -q AlreadyExists; then
      echo "Secret ${local.fernet_secret} exists; keeping it"
    else
      echo "$out" >&2
      exit 1
    fi
  SH
}

resource "kubernetes_job_v1" "fernet_key" {
  metadata {
    name      = "openmetadata-fernet-key-${substr(sha256("${local.fernet_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, { "openlakeforge.io/job" = "openmetadata-fernet-key" })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.bootstrap.metadata[0].name

        container {
          name    = "fernet-key"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.fernet_script]

          env {
            name = "NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
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

  depends_on = [kubernetes_role_binding_v1.bootstrap]
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
