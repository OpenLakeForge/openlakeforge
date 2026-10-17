# A secretKeyRef cannot cross namespaces and the issuer's client Secret lives in
# the shared namespace (created in cluster, so Terraform never holds it, #181).
# This Job copies it next to Superset, as the ingestion-bot Secret is.
locals {
  oidc_labels = {
    "app.kubernetes.io/name"       = "superset"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "reporting"
  }
}

resource "kubernetes_service_account_v1" "oidc_secret" {
  count = var.oidc == null ? 0 : 1

  metadata {
    name      = "${var.release_name}-oidc-secret"
    namespace = var.namespace
    labels    = local.oidc_labels
  }
}

resource "kubernetes_role_v1" "oidc_secret_source" {
  count = var.oidc == null ? 0 : 1

  metadata {
    name      = "${var.namespace}-${var.release_name}-oidc-secret"
    namespace = var.oidc.secret_namespace
    labels    = local.oidc_labels
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [var.oidc.secret_ref.name]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "oidc_secret_source" {
  count = var.oidc == null ? 0 : 1

  metadata {
    name      = kubernetes_role_v1.oidc_secret_source[0].metadata[0].name
    namespace = var.oidc.secret_namespace
    labels    = local.oidc_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.oidc_secret_source[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.oidc_secret[0].metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_role_v1" "oidc_secret_target" {
  count = var.oidc == null ? 0 : 1

  metadata {
    name      = "${var.release_name}-oidc-secret"
    namespace = var.namespace
    labels    = local.oidc_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create", "get", "patch", "update"]
  }
}

resource "kubernetes_role_binding_v1" "oidc_secret_target" {
  count = var.oidc == null ? 0 : 1

  metadata {
    name      = kubernetes_role_v1.oidc_secret_target[0].metadata[0].name
    namespace = var.namespace
    labels    = local.oidc_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.oidc_secret_target[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.oidc_secret[0].metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "oidc_secret" {
  count = var.oidc == null ? 0 : 1

  metadata {
    # A rotated source Secret is copied again by deleting this Job.
    name      = "${var.release_name}-oidc-secret-${substr(sha256("${var.oidc.secret_namespace}/${var.oidc.secret_ref.name}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.oidc_labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.oidc_labels, {
          "openlakeforge.io/job"       = "superset-oidc-secret"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.oidc_secret[0].metadata[0].name

        container {
          name    = "copy"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args = [<<-EOT
            kubectl get secret "$NAME" -n "$SOURCE" -o json \
              | jq '{apiVersion, kind, metadata: {name: .metadata.name}, type, data}' \
              | kubectl apply -n "$TARGET" -f -
          EOT
          ]

          env {
            name  = "NAME"
            value = var.oidc.secret_ref.name
          }
          env {
            name  = "SOURCE"
            value = var.oidc.secret_namespace
          }
          env {
            name  = "TARGET"
            value = var.namespace
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
  }

  depends_on = [
    kubernetes_role_binding_v1.oidc_secret_source,
    kubernetes_role_binding_v1.oidc_secret_target,
  ]
}
