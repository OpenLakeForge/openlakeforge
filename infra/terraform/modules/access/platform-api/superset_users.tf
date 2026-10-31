# The platform API's Superset service user, one per stage with Superset
# (ADR 0021, open item 3). One Job in the API's namespace mints every stage's
# password there and copies it into the stage's namespace, where Superset's init
# script creates the user from it. The API's namespace so holds only Secrets this
# module created; the Job's only reach into a stage namespace is that one copy.
locals {
  superset_username     = "olf-platform-api"
  superset_stage_secret = "platform-api-superset"
  superset_sources      = { for stage, namespace in var.superset_service_users : stage => "platform-api-superset-${stage}" }
  superset_users_labels = merge(local.labels, {
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "platform-api"
  })

  superset_users_script = templatefile("${path.module}/templates/superset-users.sh.tftpl", {
    namespace     = var.namespace
    username      = local.superset_username
    stage_secret  = local.superset_stage_secret
    stage_secrets = join(" ", [for stage, source in local.superset_sources : "${source}=${var.superset_service_users[stage]}"])
  })
}

resource "kubernetes_service_account_v1" "superset_users" {
  count = length(var.superset_service_users) == 0 ? 0 : 1

  metadata {
    name      = "${var.release_name}-superset-users"
    namespace = var.namespace
    labels    = local.superset_users_labels
  }
}

# `create` cannot be limited by resourceNames (Exception E1, ADR 0017); the rest is.
resource "kubernetes_role_v1" "superset_users_source" {
  count = length(var.superset_service_users) == 0 ? 0 : 1

  metadata {
    name      = "${var.release_name}-superset-users"
    namespace = var.namespace
    labels    = local.superset_users_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = values(local.superset_sources)
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "superset_users_source" {
  count = length(var.superset_service_users) == 0 ? 0 : 1

  metadata {
    name      = kubernetes_role_v1.superset_users_source[0].metadata[0].name
    namespace = var.namespace
    labels    = local.superset_users_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.superset_users_source[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.superset_users[0].metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_role_v1" "superset_users_target" {
  for_each = var.superset_service_users

  metadata {
    name      = "${var.release_name}-superset-users"
    namespace = each.value
    labels    = local.superset_users_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.superset_stage_secret]
    verbs          = ["get", "patch"]
  }
}

resource "kubernetes_role_binding_v1" "superset_users_target" {
  for_each = var.superset_service_users

  metadata {
    name      = kubernetes_role_v1.superset_users_target[each.key].metadata[0].name
    namespace = each.value
    labels    = local.superset_users_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.superset_users_target[each.key].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.superset_users[0].metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "superset_users" {
  count = length(var.superset_service_users) == 0 ? 0 : 1

  metadata {
    # A new stage or script runs the Job again; it keeps every password that exists.
    name      = "${var.release_name}-superset-users-${substr(sha256("${local.superset_users_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.superset_users_labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.superset_users_labels, {
          "openlakeforge.io/job"       = "platform-api-superset-users"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.superset_users[0].metadata[0].name

        container {
          name    = "superset-users"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.superset_users_script]
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
    update = "5m"
  }

  depends_on = [
    kubernetes_role_binding_v1.superset_users_source,
    kubernetes_role_binding_v1.superset_users_target,
  ]
}
