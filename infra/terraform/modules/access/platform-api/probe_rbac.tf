# ADR 0021 decision 7: the API's one Kubernetes permission. A Role per stage
# namespace (never a ClusterRole), read-only, naming the three resources its
# service probes list and nothing else: no Secrets, no pod logs, no exec, no
# `get` or `watch`, and nothing in `olf-system` or any namespace not listed.
resource "kubernetes_service_account_v1" "platform_api" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  automount_service_account_token = false
}

resource "kubernetes_role_v1" "probe" {
  for_each = var.probe_namespaces

  metadata {
    name      = "${var.release_name}-service-probe"
    namespace = each.value
    labels    = local.labels
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["list"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "statefulsets"]
    verbs      = ["list"]
  }
}

resource "kubernetes_role_binding_v1" "probe" {
  for_each = var.probe_namespaces

  metadata {
    name      = "${var.release_name}-service-probe"
    namespace = each.value
    labels    = local.labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.probe[each.key].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.platform_api.metadata[0].name
    namespace = var.namespace
  }
}
