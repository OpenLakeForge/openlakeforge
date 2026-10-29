# ServiceAccount + RBAC for the bootstrap job
resource "kubernetes_service_account_v1" "bootstrap" {
  metadata {
    name        = "openmetadata-bootstrap"
    namespace   = var.namespace
    labels      = local.labels
    annotations = var.service_account_annotations
  }
}

resource "kubernetes_role_v1" "bootstrap" {
  metadata {
    name      = "openmetadata-bootstrap"
    namespace = var.namespace
    labels    = local.labels
  }

  # `create` cannot be limited by resourceNames (ADR 0017, exception E1); every
  # other verb is pinned to the Secrets the Jobs manage.
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [var.automation_secret_name, var.ingestion_bot_secret_name]
    verbs          = ["delete", "get", "patch", "update"]
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [var.admin_secret_name]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "bootstrap" {
  metadata {
    name      = "openmetadata-bootstrap"
    namespace = var.namespace
    labels    = local.labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.bootstrap.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.bootstrap.metadata[0].name
    namespace = var.namespace
  }
}

# A Role only grants access inside its own namespace, so replicating the
# ingestion-bot Secret into a stage namespace needs one there too -- and so
# does revoking it from a stage that has since turned governance off, which is
# why the set spans both.
resource "kubernetes_role_v1" "bootstrap_workload" {
  for_each = toset([for namespace in concat(var.workload_namespaces, var.revoked_namespaces) : namespace if namespace != var.namespace])

  metadata {
    name      = "openmetadata-bootstrap"
    namespace = each.value
    labels    = local.labels
  }

  # `create` cannot be limited by resourceNames (ADR 0017, exception E1); every
  # other verb is pinned to the two Secrets the Jobs manage.
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [var.automation_secret_name, var.ingestion_bot_secret_name]
    verbs          = ["delete", "get", "patch", "update"]
  }

  # The Superset admin password, which the Superset module creates here. Only
  # governed stages register their dashboards; a revoked one needs no read.
  dynamic "rule" {
    for_each = contains(var.workload_namespaces, each.value) ? [1] : []
    content {
      api_groups     = [""]
      resources      = ["secrets"]
      resource_names = [var.superset_admin_secret_name]
      verbs          = ["get"]
    }
  }
}

resource "kubernetes_role_binding_v1" "bootstrap_workload" {
  for_each = kubernetes_role_v1.bootstrap_workload

  metadata {
    name      = "openmetadata-bootstrap"
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
    name      = kubernetes_service_account_v1.bootstrap.metadata[0].name
    namespace = var.namespace
  }
}
