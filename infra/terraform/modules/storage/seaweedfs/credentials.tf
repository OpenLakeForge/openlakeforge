# The S3 keys are created in-cluster, only if missing, so Terraform never
# generates one (#400, docs/architecture/identity/identity-credentials.md).
# Redeploy reuses them; rotation is deleting a Secret and replacing this Job.
#
# The same Job builds the gateway's auth config from those Secrets, so Terraform
# passes only identity data: names, Secret references and bucket grants. SeaweedFS
# reads the config at startup, so keeping every identity in one config, rather
# than minting credentials only in Kubernetes, makes a stage Secret incapable of
# authenticating as a different stage.
locals {
  auth_secret_name = "${var.release_name}-s3-auth"

  admin_namespaces = distinct(concat([var.namespace], var.workload_namespaces))
  stage_namespaces = distinct([for stage in values(var.stage_credentials) : stage.namespace])

  # Every namespace that receives a Secret, with the names it may read.
  credential_secret_names = {
    for namespace in distinct(concat(local.admin_namespaces, local.stage_namespaces)) : namespace => concat(
      contains(local.admin_namespaces, namespace) ? [var.credentials_secret_name] : [],
      namespace == var.namespace ? [local.auth_secret_name] : [],
      [for stage in values(var.stage_credentials) : stage.credentials_secret_name if stage.namespace == namespace],
    )
  }

  auth_identities = concat(
    [{
      name      = "openlakeforge-platform"
      secret    = var.credentials_secret_name
      namespace = var.namespace
      actions   = ["Admin"]
    }],
    [for stage, binding in var.stage_credentials : {
      name      = "openlakeforge-${stage}"
      secret    = binding.credentials_secret_name
      namespace = binding.namespace
      # Bronze/Silver/Gold are wholly stage-exclusive buckets, so the
      # bucket-level grant is already scoped. The ops bucket is shared across
      # every stage's activation artifacts, so its grant is further scoped to
      # this stage's own prefix - SeaweedFS matches the action's bucket/key
      # component with filepath.Match, so a trailing `*` confines it to
      # `activations/<stage>/...` and denies every other stage's prefix in the
      # same bucket.
      actions = concat(
        flatten([for bucket in [
          binding.bronze_bucket_name,
          binding.silver_bucket_name,
          binding.gold_bucket_name,
        ] : ["Read:${bucket}", "Write:${bucket}", "List:${bucket}"]]),
        [for action in ["Read", "Write", "List"] : "${action}:${binding.ops_bucket_name}/activations/${stage}*"],
      )
    }],
  )

  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    admin_secret     = var.credentials_secret_name
    admin_namespaces = join(" ", local.admin_namespaces)
    namespace        = var.namespace
    auth_secret      = local.auth_secret_name
    s3_deployment    = local.s3_service_name
    identities       = jsonencode(local.auth_identities)
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

removed {
  from = kubernetes_secret_v1.s3_auth_config
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
  # The auth config and the gateway's config-hash annotation live in the
  # module's own namespace.
  dynamic "rule" {
    for_each = each.key == var.namespace ? [1] : []
    content {
      api_groups     = [""]
      resources      = ["secrets"]
      resource_names = [local.auth_secret_name]
      verbs          = ["patch"]
    }
  }
  dynamic "rule" {
    for_each = each.key == var.namespace ? [1] : []
    content {
      api_groups     = ["apps"]
      resources      = ["deployments"]
      resource_names = [local.s3_service_name]
      verbs          = ["get", "patch"]
    }
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
