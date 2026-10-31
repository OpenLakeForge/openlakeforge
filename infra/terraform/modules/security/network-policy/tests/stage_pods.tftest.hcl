# F34 (ADR 0017): a pod-narrowed stage row becomes its own egress policy in
# the stages it names, and nothing is rendered for a stage that names none.

mock_provider "kubernetes" {}

variables {
  stage_namespaces = { dev = "olf-dev", prod = "olf-prod" }
  shared_namespace = "olf-system"
  workloads        = {}
  apiserver_cidrs  = ["10.96.0.1/32"]
  node_cidrs       = ["172.18.0.2/32"]
  flows = [
    {
      id    = "F9"
      from  = { scope = "stage" }
      to    = { scope = "shared", pods = { "app.kubernetes.io/name" = "polaris" } }
      ports = [8181]
      tag   = "V"
    },
  ]
}

run "nothing_declared_renders_no_external_egress" {
  command = plan

  assert {
    condition     = length(kubernetes_network_policy_v1.stage_pods) == 0
    error_message = "a stage that declares no source egress must get no pod-narrowed policy"
  }

  assert {
    condition     = length(kubernetes_network_policy_v1.allow) == 2
    error_message = "every stage keeps its namespace-wide allow policy"
  }
}

run "declared_ports_reach_only_run_pods_of_the_declaring_stage" {
  command = plan

  variables {
    flows = [
      {
        id    = "F9"
        from  = { scope = "stage" }
        to    = { scope = "shared", pods = { "app.kubernetes.io/name" = "polaris" } }
        ports = [8181]
        tag   = "V"
      },
      {
        id     = "F34"
        stages = ["dev"]
        from   = { scope = "stage", pods = { "app.kubernetes.io/component" = "run_worker" } }
        to     = { scope = "any" }
        ports  = [443, 5432]
        tag    = "T"
      },
    ]
  }

  assert {
    condition     = join(",", keys(kubernetes_network_policy_v1.stage_pods)) == "dev/f34-1"
    error_message = "F34 must be rendered in dev only"
  }

  assert {
    condition     = kubernetes_network_policy_v1.stage_pods["dev/f34-1"].metadata[0].namespace == "olf-dev"
    error_message = "F34 must land in the declaring stage's namespace"
  }

  assert {
    condition     = kubernetes_network_policy_v1.stage_pods["dev/f34-1"].spec[0].pod_selector[0].match_labels == tomap({ "app.kubernetes.io/component" = "run_worker" })
    error_message = "F34 must select the run pods only"
  }

  assert {
    condition     = toset(kubernetes_network_policy_v1.stage_pods["dev/f34-1"].spec[0].policy_types) == toset(["Egress"])
    error_message = "F34 opens egress, never ingress"
  }

  assert {
    condition = (
      kubernetes_network_policy_v1.stage_pods["dev/f34-1"].spec[0].egress[0].to[0].ip_block[0].cidr == "0.0.0.0/0" &&
      kubernetes_network_policy_v1.stage_pods["dev/f34-1"].spec[0].egress[0].to[0].ip_block[0].except == tolist(["10.244.0.0/16", "10.96.0.0/16"])
    )
    error_message = "F34 is any address except the pod and Service ranges"
  }

  assert {
    condition     = sort([for p in kubernetes_network_policy_v1.stage_pods["dev/f34-1"].spec[0].egress[0].ports : "${p.protocol}/${p.port}"]) == tolist(["TCP/443", "TCP/5432"])
    error_message = "F34 opens the declared TCP ports and nothing else"
  }

  assert {
    condition     = length(kubernetes_network_policy_v1.allow["dev"].spec[0].egress) == 2
    error_message = "the namespace-wide policy must not carry the pod-narrowed row"
  }
}
