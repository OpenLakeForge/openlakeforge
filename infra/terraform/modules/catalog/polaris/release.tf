# Earlier releases generated the root client secret in Terraform. The Secret
# stays in the cluster and the Job below keeps it, so the upgrade rotates
# nothing. Drop the blocks once every environment has applied past this change.
removed {
  from = random_password.root_client_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.bootstrap_credentials
  lifecycle {
    destroy = false
  }
}

# Polaris signs the OAuth tokens it issues. Without a configured key the chart
# generates an in-memory one on every start, so a restart invalidates every
# token Trino (and other clients) hold and writes fail until Trino restarts.
# The key is created in-cluster, only if missing, so Terraform never holds the
# value (#181, docs/architecture/identity/identity-credentials.md) and a redeploy does
# not rotate it. Rotation is deleting the Secret and re-running the Job.
# The root client secret is created by the same Job for the same reason (#400).
# Replacing it is not just deleting the Secret: the metastore keeps the root
# credential it was bootstrapped with.
locals {
  token_broker_secret = "${var.release_name}-token-broker"
  token_broker_script = <<-SH
    if kubectl get secret "${local.token_broker_secret}" -n "$NAMESPACE" >/dev/null 2>&1; then
      echo "Secret ${local.token_broker_secret} exists; keeping it"
    else
      kubectl create secret generic "${local.token_broker_secret}" -n "$NAMESPACE" \
        --from-literal=symmetric.key="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 64)"
    fi
    if kubectl get secret "${var.bootstrap_secret_name}" -n "$NAMESPACE" >/dev/null 2>&1; then
      echo "Secret ${var.bootstrap_secret_name} exists; keeping it"
    else
      root_secret="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
      kubectl create secret generic "${var.bootstrap_secret_name}" -n "$NAMESPACE" \
        --from-literal=POLARIS_BOOTSTRAP_CREDENTIALS="${local.realm},${local.root_client_id},$root_secret" \
        --from-literal=ROOT_CLIENT_ID="${local.root_client_id}" \
        --from-literal=ROOT_CLIENT_SECRET="$root_secret"
    fi
  SH
}

resource "kubernetes_job_v1" "token_broker_key" {
  metadata {
    name      = "polaris-token-key-${substr(sha256("${local.token_broker_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, { "openlakeforge.io/job" = "polaris-token-key" })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.bootstrap.metadata[0].name

        container {
          name    = "token-key"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.token_broker_script]

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

  depends_on = [kubernetes_job_v1.token_broker_key]

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
            name = local.token_broker_secret
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
              name = var.bootstrap_secret_name
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
