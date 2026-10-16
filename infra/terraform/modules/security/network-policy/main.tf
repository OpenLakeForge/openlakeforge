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
