# Identity credentials are created in-cluster, only if missing, so Terraform
# never holds a value: it only knows the Secret names and keys below (#181).
# Redeploy reuses them; rotation is deleting a Secret and replacing this Job.
locals {
  admin_secret_name  = "${var.release_name}-admin-creds"
  client_secret_name = { for name in keys(var.clients) : name => "${var.release_name}-client-${name}" }
  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    release_name      = var.release_name
    admin_secret_name = local.admin_secret_name
    clients           = join(" ", sort(keys(var.clients)))
  })
}

# Existing clusters hold these Secrets from when Terraform generated them.
# `destroy = false` makes state forget them instead of deleting them, which
# would rotate every credential.
removed {
  from = kubernetes_secret_v1.admin
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.client
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.client
  lifecycle {
    destroy = false
  }
}

resource "kubernetes_service_account_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials"
    namespace = var.namespace
    labels    = local.labels
  }
}

# `create` cannot be limited by resourceNames, so it covers every Secret in the
# namespace (inventoried in docs/architecture/identity-credentials.md); reading
# is limited to the Secrets this Job owns.
resource "kubernetes_role_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials"
    namespace = var.namespace
    labels    = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = concat([local.admin_secret_name], values(local.client_secret_name))
    verbs          = ["get"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials"
    namespace = var.namespace
    labels    = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.credentials.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials-${substr(sha256(local.credentials_script), 0, 10)}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "keycloak-credentials"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.credentials.metadata[0].name

        container {
          name    = "credentials"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.credentials_script]

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

  depends_on = [kubernetes_role_binding_v1.credentials]
}
