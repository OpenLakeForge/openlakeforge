# ADR 0024, Decision 2: kube-state-metrics, the one source of restart history
# and of the pod-to-component labels the recording rules join on. It watches
# only the OLF namespaces, four resources and an allowlist of metrics, and its
# grants are Roles in those namespaces: never a ClusterRole, which
# kube-state-metrics needs only when --namespaces is unset.
locals {
  ksm_name   = "kube-state-metrics"
  ksm_labels = { "app.kubernetes.io/name" = local.ksm_name }
  ksm_port   = 8080
  # Each stage namespace and olf-system, whose series take stage = platform.
  ksm_namespaces = toset(concat([var.namespace], values(var.stage_namespaces)))
  ksm_resources  = ["pods", "deployments", "statefulsets", "daemonsets"]
  ksm_metrics = [
    "kube_pod_container_status_restarts_total",
    "kube_pod_status_ready",
    "kube_pod_labels",
    # Tells a new container from one seen again after a scrape gap (rules.tf).
    "kube_pod_created",
    "kube_deployment_status_replicas_available",
    "kube_statefulset_status_replicas_ready",
    "kube_daemonset_status_number_ready",
  ]
}

resource "kubernetes_service_account_v1" "kube_state_metrics" {
  metadata {
    name      = local.ksm_name
    namespace = var.namespace
    labels    = local.ksm_labels
  }
  automount_service_account_token = false
}

# list and watch, nothing else: no get, no Secrets, ConfigMaps or events, and
# no customresourcedefinitions, which kube-state-metrics reads only for a
# custom-resource-state configuration this module never sets.
resource "kubernetes_role_v1" "kube_state_metrics" {
  for_each = local.ksm_namespaces

  metadata {
    name      = local.ksm_name
    namespace = each.value
    labels    = local.ksm_labels
  }

  lifecycle {
    precondition {
      condition     = each.value != "kube-system"
      error_message = "kube-state-metrics watches the OLF namespaces only (ADR 0024, Decision 2)."
    }
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["list", "watch"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "statefulsets", "daemonsets"]
    verbs      = ["list", "watch"]
  }
}

resource "kubernetes_role_binding_v1" "kube_state_metrics" {
  for_each = local.ksm_namespaces

  metadata {
    name      = local.ksm_name
    namespace = each.value
    labels    = local.ksm_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.kube_state_metrics[each.key].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.kube_state_metrics.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_deployment_v1" "kube_state_metrics" {
  metadata {
    name      = local.ksm_name
    namespace = var.namespace
    labels    = local.ksm_labels
  }

  spec {
    replicas = 1
    selector {
      match_labels = local.ksm_labels
    }

    template {
      metadata {
        labels = local.ksm_labels
      }

      spec {
        service_account_name = kubernetes_service_account_v1.kube_state_metrics.metadata[0].name
        # Its token goes only to the API server, which it verifies, so the
        # default projected token serves; the ServiceAccount itself mounts none.
        automount_service_account_token = true

        security_context {
          run_as_user     = 65534
          run_as_group    = 65534
          run_as_non_root = true
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }

        container {
          name              = local.ksm_name
          image             = var.kube_state_metrics_image
          image_pull_policy = "IfNotPresent"
          # The image's entrypoint already sets --port=8080 and --telemetry-port=8081.
          args = [
            "--namespaces=${join(",", sort(tolist(local.ksm_namespaces)))}",
            "--resources=${join(",", local.ksm_resources)}",
            "--metric-allowlist=${join(",", local.ksm_metrics)}",
            # Decision 3's component label; nothing else of a pod's labels is exposed.
            "--metric-labels-allowlist=pods=[app.kubernetes.io/name,app.kubernetes.io/component]",
          ]

          port {
            name           = "http"
            container_port = local.ksm_port
          }
          port {
            name           = "telemetry"
            container_port = 8081
          }

          liveness_probe {
            http_get {
              path = "/livez"
              port = "http"
            }
            initial_delay_seconds = 5
            period_seconds        = 10
          }
          readiness_probe {
            http_get {
              path = "/readyz"
              port = "telemetry"
            }
            initial_delay_seconds = 5
            period_seconds        = 10
          }

          # Starting values for four resources in a few namespaces; slice M-f
          # measures and replaces them, as it does Prometheus's.
          resources {
            requests = {
              cpu    = "10m"
              memory = "32Mi"
            }
            limits = {
              cpu    = "100m"
              memory = "128Mi"
            }
          }

          security_context {
            read_only_root_filesystem  = true
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
        }
      }
    }
  }

  # The Roles exist before the first list, so the pod does not start refused.
  depends_on = [kubernetes_role_binding_v1.kube_state_metrics]
}

# Prometheus's one way in (F50); the telemetry port is not exposed.
resource "kubernetes_service_v1" "kube_state_metrics" {
  metadata {
    name      = local.ksm_name
    namespace = var.namespace
    labels    = local.ksm_labels
  }
  spec {
    selector = local.ksm_labels
    port {
      name        = "http"
      port        = local.ksm_port
      target_port = "http"
    }
  }
}
