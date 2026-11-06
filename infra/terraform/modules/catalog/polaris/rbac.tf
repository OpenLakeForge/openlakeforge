# The token-key Job's create-if-missing Secrets (release.tf). `create` cannot
# be limited by resourceNames, so it stays namespace-wide (ADR 0017, E1). The
# principal Secrets are written by `olf` after the apply, with the operator's
# kubeconfig, so no Role here grants those (#562).
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
    resources  = ["secrets"]
    verbs      = ["create"]
  }

  # The Job checks these before creating them.
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.token_broker_secret, var.bootstrap_secret_name]
    verbs          = ["get"]
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
