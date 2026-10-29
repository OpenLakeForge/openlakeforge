# The S3 keys are created in-cluster, only if missing, so Terraform never
# generates one (#400, docs/architecture/identity/identity-credentials.md).
# Redeploy reuses them; rotation is deleting a Secret and replacing this Job.
locals {
  admin_namespaces = distinct(concat([var.namespace], var.workload_namespaces))
  stage_namespaces = distinct([for stage in values(var.stage_credentials) : stage.namespace])

  # Every namespace that receives a Secret, with the names it may read.
  credential_secret_names = {
    for namespace in distinct(concat(local.admin_namespaces, local.stage_namespaces)) : namespace => concat(
      contains(local.admin_namespaces, namespace) ? [var.credentials_secret_name] : [],
      [for stage in values(var.stage_credentials) : stage.credentials_secret_name if stage.namespace == namespace],
    )
  }

  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    admin_secret     = var.credentials_secret_name
    admin_namespaces = join(" ", local.admin_namespaces)
    stages           = join(" ", [for stage in values(var.stage_credentials) : "${stage.credentials_secret_name}=${stage.namespace}"])
  })
}

# Earlier releases generated the keys in Terraform. The Secrets stay in the
# cluster and the Job keeps them, so the upgrade rotates nothing. Drop the
# blocks once every environment has applied past this change.
removed {
  from = random_id.s3_access_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.s3_secret_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_id.stage_s3_access_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.stage_s3_secret_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.s3_credentials
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.stage_s3_credentials
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
          "openlakeforge.io/job"       = "seaweedfs-credentials"
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
