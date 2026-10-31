terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  # A row may list several sources under `from`; each becomes its own row.
  rows    = flatten([for f in var.flows : [for s in flatten([f.from]) : merge(f, { from = s })]])
  egress  = [for f in local.rows : f if f.from.scope == "stage" && length(try(f.from.pods, {})) == 0]
  ingress = [for f in local.rows : f if f.to.scope == "stage"]

  # A stage row that names pods narrows a grant the namespace-wide policy
  # would otherwise give every pod (F34), so it gets a policy of its own in
  # each stage its `stages` list names (all of them when absent). Labels in a
  # stage namespace are set by user code (decision 4): this narrows, it does
  # not isolate.
  stage_pod_egress = merge([
    for index, f in local.rows : {
      for stage, namespace in var.stage_namespaces : "${stage}/${lower(f.id)}-${index}" => {
        name      = "olf-flows-${lower(f.id)}"
        namespace = namespace
        flow      = f
      } if contains(try(f.stages, keys(var.stage_namespaces)), stage)
    } if f.from.scope == "stage" && length(try(f.from.pods, {})) > 0
  ]...)

  # Peers a namespace selector cannot name are addresses. `any` stops short of
  # the pod and Service ranges so it cannot reach a pod by IP.
  cidrs = { apiserver = var.apiserver_cidrs, node = var.node_cidrs, any = ["0.0.0.0/0"] }

  # Namespaces are matched by label, never by name. kube-system gets the
  # automatic name label because the cluster, not OLF, creates it.
  scope_labels = {
    shared        = { "openlakeforge.io/scope" = "shared" }
    "kube-system" = { "kubernetes.io/metadata.name" = "kube-system" }
  }

  # A row gives a workload a rule when its pods include every label of the
  # workload, so a Job that shares only the name label never matches.
  workload_ingress = { for name, labels in var.workloads : name => [
    for f in local.rows : f if f.to.scope == "shared" && alltrue([for k, v in labels : try(f.to.pods[k], null) == v])
  ] }
  workload_egress = { for name, labels in var.workloads : name => [
    for f in local.rows : f if f.from.scope == "shared" && alltrue([for k, v in labels : try(f.from.pods[k], null) == v])
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

resource "kubernetes_network_policy_v1" "stage_pods" {
  for_each = local.stage_pod_egress

  metadata {
    name      = each.value.name
    namespace = each.value.namespace
  }
  spec {
    pod_selector {
      match_labels = each.value.flow.from.pods
    }
    policy_types = ["Egress"]

    egress {
      dynamic "to" {
        for_each = local.cidrs[each.value.flow.to.scope]
        content {
          ip_block {
            cidr   = to.value
            except = each.value.flow.to.scope == "any" ? var.internal_cidrs : null
          }
        }
      }
      dynamic "ports" {
        for_each = setproduct(each.value.flow.ports, try(each.value.flow.protocols, ["TCP"]))
        content {
          port     = tostring(ports.value[0])
          protocol = ports.value[1]
        }
      }
    }
  }

  depends_on = [kubernetes_network_policy_v1.default_deny]
}

# Only the pods named in `workloads` are selected; the rest of olf-system
# stays unisolated. A policy that selects both directions denies every flow it
# does not list, so there is no separate deny object. DNS is the baseline of
# ADR 0017 decision 2, so every workload gets it here rather than as a row.
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
        dynamic "from" {
          for_each = contains(keys(local.cidrs), flow.value.from.scope) ? local.cidrs[flow.value.from.scope] : []
          content {
            ip_block {
              cidr   = from.value
              except = flow.value.from.scope == "any" ? var.internal_cidrs : null
            }
          }
        }
        dynamic "from" {
          for_each = contains(keys(local.cidrs), flow.value.from.scope) ? [] : [flow.value.from]
          content {
            namespace_selector {
              match_labels = from.value.scope == "stage" ? null : local.scope_labels[from.value.scope]
              dynamic "match_expressions" {
                for_each = from.value.scope == "stage" ? [1] : []
                content {
                  key      = "openlakeforge.io/stage"
                  operator = "Exists"
                }
              }
            }
            dynamic "pod_selector" {
              for_each = length(try(from.value.pods, {})) > 0 ? [from.value.pods] : []
              content {
                match_labels = pod_selector.value
              }
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

    egress {
      to {
        namespace_selector {
          match_labels = local.scope_labels["kube-system"]
        }
        pod_selector {
          match_labels = { "k8s-app" = "kube-dns" }
        }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    dynamic "egress" {
      for_each = local.workload_egress[each.key]
      iterator = flow
      content {
        dynamic "to" {
          for_each = contains(keys(local.cidrs), flow.value.to.scope) ? local.cidrs[flow.value.to.scope] : []
          content {
            ip_block {
              cidr   = to.value
              except = flow.value.to.scope == "any" ? var.internal_cidrs : null
            }
          }
        }
        dynamic "to" {
          for_each = contains(keys(local.cidrs), flow.value.to.scope) ? [] : [flow.value.to]
          content {
            namespace_selector {
              match_labels = to.value.scope == "stage" ? null : local.scope_labels[to.value.scope]
              dynamic "match_expressions" {
                for_each = to.value.scope == "stage" ? [1] : []
                content {
                  key      = "openlakeforge.io/stage"
                  operator = "Exists"
                }
              }
            }
            dynamic "pod_selector" {
              for_each = length(try(to.value.pods, {})) > 0 ? [to.value.pods] : []
              content {
                match_labels = pod_selector.value
              }
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
