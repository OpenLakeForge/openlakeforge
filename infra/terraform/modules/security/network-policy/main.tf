terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  egress  = [for f in var.flows : f if f.from.scope == "stage"]
  ingress = [for f in var.flows : f if f.to.scope == "stage"]

  # Namespaces are matched by label, never by name. kube-system gets the
  # automatic name label because the cluster, not OLF, creates it.
  scope_labels = {
    shared        = { "openlakeforge.io/scope" = "shared" }
    "kube-system" = { "kubernetes.io/metadata.name" = "kube-system" }
  }

  # A row gives a workload a rule when its pods include every label of the
  # workload, so a Job that shares only the name label never matches.
  workload_ingress = { for name, labels in var.workloads : name => [
    for f in var.flows : f if f.to.scope == "shared" && alltrue([for k, v in labels : try(f.to.pods[k], null) == v])
  ] }
  workload_egress = { for name, labels in var.workloads : name => [
    for f in var.flows : f if f.from.scope == "shared" && alltrue([for k, v in labels : try(f.from.pods[k], null) == v])
  ] }
}

# Pods in a namespace with no policy are not isolated, so the deny selects
# every pod and the allow policy below adds back only what the table lists.
resource "kubernetes_network_policy_v1" "default_deny" {
  for_each = var.stage_namespaces

  metadata {
    name      = "olf-default-deny"
    namespace = each.value
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "allow" {
  for_each = var.stage_namespaces

  metadata {
    name      = "olf-flows"
    namespace = each.value
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]

    # A stage namespace is one trust domain: run pods and Floe Jobs carry
    # labels user code controls, so no rule inside it keys on a label.
    ingress {
      from {
        pod_selector {}
      }
    }
    egress {
      to {
        pod_selector {}
      }
    }

    dynamic "ingress" {
      for_each = local.ingress
      iterator = flow
      content {
        from {
          namespace_selector {
            match_labels = local.scope_labels[flow.value.from.scope]
          }
          dynamic "pod_selector" {
            for_each = length(try(flow.value.from.pods, {})) > 0 ? [flow.value.from.pods] : []
            content {
              match_labels = pod_selector.value
            }
          }
        }
        dynamic "ports" {
          for_each = setproduct(flow.value.ports, try(flow.value.protocols, ["TCP"]))
          content {
            port     = tostring(ports.value[0])
            protocol = ports.value[1]
          }
        }
      }
    }

    dynamic "egress" {
      for_each = local.egress
      iterator = flow
      content {
        dynamic "to" {
          for_each = flow.value.to.scope == "apiserver" ? [] : [flow.value.to]
          content {
            namespace_selector {
              match_labels = local.scope_labels[to.value.scope]
            }
            dynamic "pod_selector" {
              for_each = length(try(to.value.pods, {})) > 0 ? [to.value.pods] : []
              content {
                match_labels = pod_selector.value
              }
            }
          }
        }
        dynamic "to" {
          for_each = flow.value.to.scope == "apiserver" ? var.apiserver_cidrs : []
          content {
            ip_block {
              cidr = to.value
            }
          }
        }
        dynamic "ports" {
          for_each = setproduct(flow.value.ports, try(flow.value.protocols, ["TCP"]))
          content {
            port     = tostring(ports.value[0])
            protocol = ports.value[1]
          }
        }
      }
    }
  }

  depends_on = [kubernetes_network_policy_v1.default_deny]
}

# Only the data-plane pods named in `workloads` are selected; the rest of
# olf-system stays unisolated until the access-plane slice. A policy that
# selects both directions denies every flow it does not list, so there is no
# separate deny object.
resource "kubernetes_network_policy_v1" "workload" {
  for_each = var.workloads

  metadata {
    name      = "olf-flows-${each.key}"
    namespace = var.shared_namespace
  }
  spec {
    pod_selector {
      match_labels = each.value
    }
    policy_types = ["Ingress", "Egress"]

    dynamic "ingress" {
      for_each = local.workload_ingress[each.key]
      iterator = flow
      content {
        from {
          namespace_selector {
            match_labels = flow.value.from.scope == "stage" ? null : local.scope_labels[flow.value.from.scope]
            dynamic "match_expressions" {
              for_each = flow.value.from.scope == "stage" ? [1] : []
              content {
                key      = "openlakeforge.io/stage"
                operator = "Exists"
              }
            }
          }
          dynamic "pod_selector" {
            for_each = length(try(flow.value.from.pods, {})) > 0 ? [flow.value.from.pods] : []
            content {
              match_labels = pod_selector.value
            }
          }
        }
        dynamic "ports" {
          for_each = setproduct(flow.value.ports, try(flow.value.protocols, ["TCP"]))
          content {
            port     = tostring(ports.value[0])
            protocol = ports.value[1]
          }
        }
      }
    }

    dynamic "egress" {
      for_each = local.workload_egress[each.key]
      iterator = flow
      content {
        to {
          namespace_selector {
            match_labels = local.scope_labels[flow.value.to.scope]
          }
          dynamic "pod_selector" {
            for_each = length(try(flow.value.to.pods, {})) > 0 ? [flow.value.to.pods] : []
            content {
              match_labels = pod_selector.value
            }
          }
        }
        dynamic "ports" {
          for_each = setproduct(flow.value.ports, try(flow.value.protocols, ["TCP"]))
          content {
            port     = tostring(ports.value[0])
            protocol = ports.value[1]
          }
        }
      }
    }
  }
}
