# The Corefile sends the base domain to the ingress, and answers a cluster-local
# name with a host search domain appended locally instead of forwarding it.

mock_provider "kubernetes" {}

variables {
  namespace           = "olf-system"
  base_domain         = "olf.localhost"
  trust_namespaces    = ["olf-dev"]
  bootstrap_job_image = "alpine/k8s:1.31.13"
}

run "a_cluster_local_name_with_a_host_search_domain_is_answered_locally" {
  command = plan

  assert {
    # Unquoted, with single backslashes: CoreDNS keeps a quoted `\\` as two characters, which never matches.
    condition     = strcontains(kubernetes_config_map_v1_data.corefile[0].data.Corefile, "match \\.(svc|pod)\\.cluster\\.local\\.[^.]+.*$\n")
    error_message = "the template matches a Service or Pod name followed by another suffix"
  }

  # The rendered pattern, applied as RE2 (what CoreDNS uses) to names a pod asks for.
  assert {
    condition = alltrue([
      for name in ["dex.olf-system.svc.cluster.local.home.", "10-244-0-5.olf-dev.pod.cluster.local.corp.example."] :
      can(regex(regex("match (\\S+)\n", kubernetes_config_map_v1_data.corefile[0].data.Corefile)[0], name))
    ])
    error_message = "a Service or Pod name with a host search domain appended is answered locally"
  }

  assert {
    condition = !anytrue([
      for name in [
        "dex.olf-system.svc.cluster.local.",
        "pod-0.cluster.local.svc.cluster.local.",
        "_grpc._tcp.cluster.local.svc.cluster.local.",
        "api.github.com.",
      ] :
      can(regex(regex("match (\\S+)\n", kubernetes_config_map_v1_data.corefile[0].data.Corefile)[0], name))
    ])
    error_message = "real names, including a Service `cluster` in namespace `local`, still resolve"
  }

  assert {
    condition     = strcontains(kubernetes_config_map_v1_data.corefile[0].data.Corefile, "rcode NXDOMAIN")
    error_message = "such a name is answered NXDOMAIN"
  }

  # Placed before kubernetes and forward, it must let every other name through.
  assert {
    condition     = strcontains(kubernetes_config_map_v1_data.corefile[0].data.Corefile, "fallthrough\n")
    error_message = "a name the template does not match falls through to kubernetes and forward"
  }

  assert {
    condition     = strcontains(kubernetes_config_map_v1_data.corefile[0].data.Corefile, "name regex ^(.+\\.)?olf\\.localhost\\.$ traefik.olf-system.svc.cluster.local.")
    error_message = "the base domain still resolves to the ingress"
  }
}

run "without_the_rewrite_the_corefile_is_left_alone" {
  command = plan

  variables {
    rewrite_to_ingress = false
  }

  assert {
    condition     = length(kubernetes_config_map_v1_data.corefile) == 0
    error_message = "no Corefile is written without the ingress rewrite"
  }
}
