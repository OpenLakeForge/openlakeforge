# Workers are the only replicated Trino pods (spec.query.size medium and large).
# One at a time keeps a node drain from taking every worker; the coordinator is
# a single replica and gets no budget, because minAvailable=1 on one pod would
# block the drain (#351). The chart renders no PodDisruptionBudget of its own.
resource "kubernetes_pod_disruption_budget_v1" "worker" {
  count = try(var.query_sizing.workers, 0) > 0 ? 1 : 0

  metadata {
    name      = "${var.release_name}-worker"
    namespace = var.namespace
  }

  spec {
    max_unavailable = 1
    selector {
      match_labels = {
        "app.kubernetes.io/name"      = "trino"
        "app.kubernetes.io/instance"  = var.release_name
        "app.kubernetes.io/component" = "worker"
      }
    }
  }

  depends_on = [helm_release.trino]
}
