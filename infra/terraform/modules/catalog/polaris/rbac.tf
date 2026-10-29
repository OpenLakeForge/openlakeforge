# Every principal Secret and pending marker the bootstrap and replication Jobs
# touch is named at plan time (ADR 0017, RBAC inventory). `create` cannot be
# limited by resourceNames, so it stays namespace-wide; every other verb is
# pinned to these names. One set serves every namespace.
locals {
  bootstrap_secret_names = distinct(concat(
    [
      var.trino_credentials_secret_name,
      var.floe_credentials_secret_name,
      var.om_credentials_secret_name,
      var.deployer_credentials_secret_name,
      "polaris-dbt-creds", # removed on every run: pre-migration leftover
    ],
    flatten([for stage in values(var.stage_catalogs) : [
      stage.trino_credentials_secret_name,
      stage.floe_credentials_secret_name,
      stage.deployer_credentials_secret_name,
    ]]),
  ))
  bootstrap_marker_names = [for name in local.bootstrap_secret_names : "${name}-bootstrap-pending"]
}

resource "kubernetes_service_account_v1" "bootstrap" {
  metadata {
    name      = "polaris-bootstrap"
    namespace = var.namespace
    labels    = local.labels
  }
}

resource "kubernetes_role_v1" "bootstrap" {
  metadata {
    name      = "polaris-bootstrap"
    namespace = var.namespace
    labels    = local.labels
  }

  rule {
    api_groups = [""]
    resources  = ["configmaps", "secrets"]
    verbs      = ["create"]
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = local.bootstrap_secret_names
    verbs          = ["delete", "get", "patch", "update"]
  }

  # The credentials Job checks these before creating them.
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.token_broker_secret, var.bootstrap_secret_name]
    verbs          = ["get"]
  }

  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = local.bootstrap_marker_names
    verbs          = ["delete", "get", "patch", "update"]
  }
}

resource "kubernetes_role_binding_v1" "bootstrap" {
  metadata {
    name      = "polaris-bootstrap"
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

# The bootstrap job writes principal Secrets into every workload namespace,
# which needs its own Role there - a Role only ever grants access inside the
# namespace that holds it.
resource "kubernetes_role_v1" "bootstrap_workload" {
  for_each = toset([for namespace in var.workload_namespaces : namespace if namespace != var.namespace])

  metadata {
    name      = "polaris-bootstrap"
    namespace = each.value
    labels    = local.labels
  }

  rule {
    api_groups = [""]
    resources  = ["configmaps", "secrets"]
    verbs      = ["create"]
  }

  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = local.bootstrap_secret_names
    verbs          = ["delete", "get", "patch", "update"]
  }

  rule {
    api_groups     = [""]
    resources      = ["configmaps"]
    resource_names = local.bootstrap_marker_names
    verbs          = ["delete", "get", "patch", "update"]
  }
}

resource "kubernetes_role_binding_v1" "bootstrap_workload" {
  for_each = kubernetes_role_v1.bootstrap_workload

  metadata {
    name      = "polaris-bootstrap"
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
