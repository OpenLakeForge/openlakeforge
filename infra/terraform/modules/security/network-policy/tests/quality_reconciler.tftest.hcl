# ADR 0007: the quality reconciler's pods may reach OpenMetadata and the object
# store, plus DNS, and nothing may reach them. Rows are the reconciler's two in
# release/network-flows.yaml, spelled out because a module test is copied away
# from the repository tree.

mock_provider "kubernetes" {}

variables {
  stage_namespaces = { dev = "olf-dev" }
  shared_namespace = "olf-system"
  apiserver_cidrs  = ["10.96.0.1/32"]
  node_cidrs       = ["172.18.0.2/32"]
  workloads = {
    quality-reconciler = { "openlakeforge.io/job" = "quality-reconciler" }
    openmetadata       = { "app.kubernetes.io/name" = "openmetadata", "app.kubernetes.io/instance" = "openmetadata" }
    seaweedfs          = { "app.kubernetes.io/name" = "seaweedfs", "app.kubernetes.io/instance" = "seaweedfs" }
  }
  flows = [
    {
      id    = "F13"
      from  = { scope = "shared", pods = { "openlakeforge.io/job" = "quality-reconciler" } }
      to    = { scope = "shared", pods = { "app.kubernetes.io/name" = "openmetadata", "app.kubernetes.io/instance" = "openmetadata" } }
      ports = [8585]
      tag   = "T"
    },
    {
      id    = "F39"
      from  = { scope = "shared", pods = { "openlakeforge.io/job" = "quality-reconciler" } }
      to    = { scope = "shared", pods = { "app.kubernetes.io/name" = "seaweedfs", "app.kubernetes.io/instance" = "seaweedfs", "app.kubernetes.io/component" = "s3" } }
      ports = [8333]
      tag   = "T"
    },
  ]
}

run "the_reconciler_has_dns_openmetadata_and_the_object_store_and_nothing_else" {
  command = plan

  assert {
    condition     = contains(keys(kubernetes_network_policy_v1.workload), "quality-reconciler")
    error_message = "the reconciler's pods are isolated by a policy of their own"
  }

  assert {
    condition = (
      length(kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].egress) == 3
      && kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].egress[1].ports[0].port == "8585"
      && kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].egress[1].to[0].pod_selector[0].match_labels["app.kubernetes.io/name"] == "openmetadata"
      && kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].egress[2].ports[0].port == "8333"
      && kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].egress[2].to[0].pod_selector[0].match_labels["app.kubernetes.io/name"] == "seaweedfs"
    )
    error_message = "egress is DNS, OpenMetadata 8585 and the object store 8333: no API server, no other service and no external address"
  }

  assert {
    condition     = length(kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].ingress) == 0
    error_message = "nothing may reach the reconciler's pods"
  }

  assert {
    condition = alltrue([
      for egress in kubernetes_network_policy_v1.workload["quality-reconciler"].spec[0].egress :
      alltrue([for to in egress.to : length(to.ip_block) == 0])
    ])
    error_message = "no egress by address: an ipBlock would reach outside the cluster or the API server"
  }
}

run "openmetadata_accepts_the_reconciler_on_its_port_only" {
  command = plan

  assert {
    condition = anytrue([
      for ingress in kubernetes_network_policy_v1.workload["openmetadata"].spec[0].ingress :
      length(ingress.from) == 1
      && ingress.from[0].pod_selector[0].match_labels["openlakeforge.io/job"] == "quality-reconciler"
      && ingress.ports[0].port == "8585"
    ])
    error_message = "OpenMetadata admits the reconciler's pods on 8585"
  }
}

run "without_governance_the_reconciler_has_no_policy" {
  command = plan

  variables {
    workloads = {
      seaweedfs = { "app.kubernetes.io/name" = "seaweedfs", "app.kubernetes.io/instance" = "seaweedfs" }
    }
  }

  assert {
    condition     = !contains(keys(kubernetes_network_policy_v1.workload), "quality-reconciler")
    error_message = "a workload that is not deployed gets no policy, and its rows render only on the side that is"
  }
}
