# Credentials are created in-cluster, only if missing, so Terraform never holds
# a value: it names the Secrets below and nothing more (#400,
# docs/architecture/identity/identity-credentials.md). Redeploy reuses them;
# rotation is deleting a Secret and replacing this Job.
locals {
  admin_secret_name = "${var.release_name}-admin-creds"

  # Every namespace that receives a Secret, with the names it may touch. The
  # module's own namespace also holds the admin and Polaris Secrets.
  credential_secret_names = {
    for namespace in distinct(concat([var.namespace], [for secret in values(local.database_secrets) : secret.namespace])) :
    namespace => concat(
      [for secret in values(local.database_secrets) : secret.name if secret.namespace == namespace],
      namespace == var.namespace ? [local.admin_secret_name, var.polaris_credentials_secret_name] : [],
    )
  }

  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    admin_secret     = local.admin_secret_name
    polaris_secret   = var.polaris_credentials_secret_name
    polaris_user     = var.polaris_db_user
    polaris_jdbc_url = "jdbc:postgresql://${local.host}:${local.port}/${var.polaris_db_name}"
    databases = join(" ", [
      for database in var.databases :
      "${database.credentials_secret_name}=${join(",", distinct(concat([var.namespace], database.namespaces)))}"
    ])
  })
}

# Earlier releases generated these in Terraform. The Secrets stay in the
# cluster and the Job keeps them, so the upgrade rotates nothing. Drop the
# blocks once every environment has applied past this change.
removed {
  from = random_password.postgres_admin
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.polaris
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.database
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.admin_credentials
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.database_credentials
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.polaris_credentials
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
# namespace (ADR 0017 exception E1); reading is limited to the Secrets this Job
# owns there.
resource "kubernetes_role_v1" "credentials" {
  for_each = local.credential_secret_names

  metadata {
    name      = "${var.release_name}-credentials"
    namespace = each.key
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
    resource_names = each.value
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "credentials" {
  for_each = kubernetes_role_v1.credentials

  metadata {
    name      = each.value.metadata[0].name
    namespace = each.key
    labels    = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = each.value.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials-${substr(sha256("${local.credentials_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "postgresql-credentials"
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
