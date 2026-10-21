resource "random_password" "root_client_secret" {
  length  = 32
  special = false
}

resource "kubernetes_secret_v1" "bootstrap_credentials" {
  metadata {
    name      = var.bootstrap_secret_name
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    POLARIS_BOOTSTRAP_CREDENTIALS = "${local.realm},${local.root_client_id},${random_password.root_client_secret.result}"
    ROOT_CLIENT_ID                = local.root_client_id
    ROOT_CLIENT_SECRET            = random_password.root_client_secret.result
  }

  type = "Opaque"
}

# Polaris signs the OAuth tokens it issues. Without a configured key the chart
# generates an in-memory one on every start, so a restart invalidates every
# token Trino (and other clients) hold and writes fail until Trino restarts.
resource "random_password" "token_broker_key" {
  length  = 64
  special = false
}

resource "kubernetes_secret_v1" "token_broker" {
  metadata {
    name      = "${var.release_name}-token-broker"
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "symmetric.key" = random_password.token_broker_key.result
  }

  type = "Opaque"
}

locals {
  chart_package = var.chart_package_path != null ? var.chart_package_path : "polaris"
  chart_repo    = var.chart_package_path != null ? null : var.chart_repository
  chart_ver     = var.chart_package_path != null ? null : var.chart_version
}

resource "helm_release" "polaris" {
  name       = var.release_name
  repository = local.chart_repo
  chart      = local.chart_package
  version    = local.chart_ver
  namespace  = var.namespace

  wait    = true
  timeout = 300

  values = [
    file(var.base_values_file),
    yamlencode({
      persistence = {
        type = "relational-jdbc"
        relationalJdbc = {
          secret = {
            name     = var.postgresql_contract.polaris_credentials_secret_name
            username = "username"
            password = "password"
            jdbcUrl  = "jdbcUrl"
          }
        }
      }
      authentication = {
        tokenBroker = {
          type = "symmetric-key"
          secret = {
            name = kubernetes_secret_v1.token_broker.metadata[0].name
            symmetricKey = {
              secretKey = "symmetric.key"
            }
          }
        }
      }
      extraEnv = [
        {
          name = "POLARIS_BOOTSTRAP_CREDENTIALS"
          valueFrom = {
            secretKeyRef = {
              name = kubernetes_secret_v1.bootstrap_credentials.metadata[0].name
              key  = "POLARIS_BOOTSTRAP_CREDENTIALS"
            }
          }
        },
        {
          name  = "AWS_REGION"
          value = var.storage_contract.region
        },
        {
          name  = "AWS_ENDPOINT_URL_S3"
          value = var.storage_contract.endpoint
        },
        {
          name  = "AWS_S3_FORCE_PATH_STYLE"
          value = tostring(var.storage_contract.path_style_access)
        },
        {
          name = "AWS_ACCESS_KEY_ID"
          valueFrom = {
            secretKeyRef = {
              name = var.storage_contract.credentials_secret_name
              key  = var.storage_contract.access_key_id_key
            }
          }
        },
        {
          name = "AWS_SECRET_ACCESS_KEY"
          valueFrom = {
            secretKeyRef = {
              name = var.storage_contract.credentials_secret_name
              key  = var.storage_contract.secret_access_key_key
            }
          }
        },
      ]
    }),
  ]
}

resource "terraform_data" "polaris_release_revision" {
  triggers_replace = [
    helm_release.polaris.metadata.revision,
  ]
}

resource "terraform_data" "polaris_bootstrap_revision" {
  triggers_replace = [
    var.bootstrap_revision,
  ]
}
