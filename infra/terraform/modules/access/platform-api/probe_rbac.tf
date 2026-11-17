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

  lifecycle {
    precondition {
      condition     = !contains(["kube-system", var.namespace], each.value)
      error_message = "probe_namespaces must not name the API's own namespace or kube-system (ADR 0021, decision 7)."
    }
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

# ADR 0021 decision 3: a service caller's token is checked with a TokenReview as the API's own ServiceAccount. A
# TokenReview is a cluster-scoped create, so this is the API's one ClusterRole, and it grants that single verb on that
# single resource: it reads nothing and cannot act as the token's holder.
resource "kubernetes_cluster_role_v1" "token_review" {
  count = length(var.service_callers) > 0 ? 1 : 0

  metadata {
    name   = "${var.namespace}-${var.release_name}-token-review"
    labels = local.labels
  }

  rule {
    api_groups = ["authentication.k8s.io"]
    resources  = ["tokenreviews"]
    verbs      = ["create"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "token_review" {
  count = length(var.service_callers) > 0 ? 1 : 0

  metadata {
    name   = "${var.namespace}-${var.release_name}-token-review"
    labels = local.labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.token_review[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.platform_api.metadata[0].name
    namespace = var.namespace
  }
}
