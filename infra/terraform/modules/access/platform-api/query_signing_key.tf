# The key that signs the API's query ids and cursors (ADR 0021, #800). Minted
# in-cluster only if missing, so Terraform never holds the value (#400) and a
# redeploy keeps it: rotating it is deleting the Secret and replacing the Job,
# which invalidates the ids and cursors in flight. One Secret, so every replica
# shares it. The Job's pods carry no platform-api name label, so the workload
# policy of the API does not select them and they reach the API server.
locals {
  query_signing_secret = "${var.release_name}-query-signing-key"
  query_signing_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "platform-api"
  }
  query_signing_script = <<-SH
    if kubectl get secret "${local.query_signing_secret}" -n "${var.namespace}" >/dev/null 2>&1; then
      echo "Secret ${local.query_signing_secret} exists; keeping it"
    else
      kubectl create secret generic "${local.query_signing_secret}" -n "${var.namespace}" \
        --from-literal=key="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 64)" >/dev/null
      echo "Created Secret ${local.query_signing_secret}"
    fi
  SH
}

resource "kubernetes_service_account_v1" "query_signing_key" {
  count = var.person_query ? 1 : 0

  metadata {
    name      = "${var.release_name}-query-signing-key"
    namespace = var.namespace
    labels    = local.query_signing_labels
  }
}

# `create` cannot be limited by resourceNames (Exception E1, ADR 0017); the read is.
resource "kubernetes_role_v1" "query_signing_key" {
  count = var.person_query ? 1 : 0

  metadata {
    name      = "${var.release_name}-query-signing-key"
    namespace = var.namespace
    labels    = local.query_signing_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.query_signing_secret]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "query_signing_key" {
  count = var.person_query ? 1 : 0

  metadata {
    name      = kubernetes_role_v1.query_signing_key[0].metadata[0].name
    namespace = var.namespace
    labels    = local.query_signing_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.query_signing_key[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.query_signing_key[0].metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "query_signing_key" {
  count = var.person_query ? 1 : 0

  metadata {
    name      = "${var.release_name}-query-signing-key-${substr(sha256("${local.query_signing_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.query_signing_labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.query_signing_labels, {
          "openlakeforge.io/job"       = "platform-api-query-signing-key"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.query_signing_key[0].metadata[0].name

        container {
          name    = "query-signing-key"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.query_signing_script]

          resources {
            requests = {
              cpu    = "10m"
              memory = "32Mi"
            }
            limits = {
              memory = "128Mi"
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

  depends_on = [kubernetes_role_binding_v1.query_signing_key]
}
