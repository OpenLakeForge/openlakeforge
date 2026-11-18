# ADR 0024 decisions 1 to 5: one bounded Prometheus, its scrape limits, the
# stage label from namespaces and buckets, a grant of nodes/metrics only, and
# kube-state-metrics on namespaced Roles.
# Whether a disabled profile renders it at all is the local root's test.

mock_provider "kubernetes" {}

variables {
  namespace        = "olf-system"
  retention_days   = 7
  volume_size      = "4Gi"
  retention_size   = "3GB"
  stage_namespaces = { dev = "olf-dev", prod = "olf-prod" }
  trino_targets    = { dev = "trino.olf-dev.svc:8080", prod = "trino.olf-prod.svc:8080" }
  object_store_targets = {
    master = "seaweedfs-master.olf-system.svc:9327"
    volume = "seaweedfs-volume.olf-system.svc:9327"
    filer  = "seaweedfs-filer.olf-system.svc:9327"
    s3     = "seaweedfs-s3.olf-system.svc:9327"
  }
  bucket_stages = { olf-dev-bronze = "dev", olf-prod-gold = "prod", olf-ops = "platform" }
}

run "every_scrape_is_bounded_and_history_is_capped" {
  command = plan

  assert {
    condition = (
      yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).global.label_limit == 16
      && yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).global.label_value_length_limit == 256
      && yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).global.scrape_interval == "30s"
    )
    error_message = "label count and length are limited for every job, at a 30 s interval"
  }

  assert {
    condition = {
      for job in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs :
      job.job_name => [job.sample_limit, try(job.target_limit, null)]
      } == {
      kubelet-resource   = [2000, 20]
      kubelet            = [2000, 20]
      trino              = [500, 3]
      object-store       = [500, 4]
      kube-state-metrics = [3000, 1]
      prometheus         = [200, null]
    }
    error_message = "each job carries Decision 3's sample_limit and target_limit"
  }

  assert {
    condition     = alltrue([for job in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs : job.metric_relabel_configs[0].action == "keep"])
    error_message = "every job keeps an allowlist before anything is stored"
  }

  assert {
    condition     = yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).storage.tsdb.retention == { time = "7d", size = "3GB" }
    error_message = "retention by time and by size, whichever comes first"
  }

  assert {
    condition     = kubernetes_persistent_volume_claim_v1.data.spec[0].resources[0].requests["storage"] == "4Gi" && !kubernetes_persistent_volume_claim_v1.data.wait_until_bound
    error_message = "the data volume is the size row's, and the apply does not wait on a WaitForFirstConsumer claim"
  }
}

run "nothing_reloads_deletes_or_receives_over_http" {
  command = plan

  assert {
    condition = length([
      for arg in kubernetes_deployment_v1.prometheus.spec[0].template[0].spec[0].container[0].args : arg
      if strcontains(arg, "enable-admin-api") || strcontains(arg, "enable-lifecycle") || strcontains(arg, "otlp") || strcontains(arg, "remote-write")
    ]) == 0
    error_message = "no admin API, lifecycle endpoint, OTLP receiver or remote-write receiver"
  }

  assert {
    condition = alltrue([
      for arg in ["--query.timeout=10s", "--query.max-samples=5000000", "--query.max-concurrency=4"] :
      contains(kubernetes_deployment_v1.prometheus.spec[0].template[0].spec[0].container[0].args, arg)
    ])
    error_message = "queries are bounded in time, samples and concurrency"
  }

  assert {
    condition     = kubernetes_deployment_v1.prometheus.spec[0].replicas == "1" && kubernetes_deployment_v1.prometheus.spec[0].strategy[0].type == "Recreate"
    error_message = "one writer for one TSDB"
  }
}

run "stage_comes_from_the_namespace_and_the_bucket" {
  command = plan

  assert {
    condition = toset([
      for rule in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs[0].metric_relabel_configs :
      "${rule.regex}=${rule.replacement}" if try(rule.target_label, "") == "stage"
    ]) == toset(["olf-dev=dev", "olf-prod=prod", "olf-system=platform"])
    error_message = "a kubelet series takes its stage from its namespace, olf-system as platform"
  }

  assert {
    condition = toset([
      for rule in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs[3].metric_relabel_configs :
      "${rule.regex}=${rule.replacement}" if try(rule.target_label, "") == "stage"
    ]) == toset(["olf-dev-bronze=dev", "olf-prod-gold=prod", "olf-ops=platform"])
    error_message = "an object-store series takes its stage from its bucket, the ops bucket as platform"
  }
}

run "the_grant_is_node_discovery_and_nodes_metrics_only" {
  command = plan

  assert {
    condition = (
      length(kubernetes_cluster_role_v1.prometheus.rule) == 2
      && kubernetes_cluster_role_v1.prometheus.rule[0].resources == tolist(["nodes"])
      && kubernetes_cluster_role_v1.prometheus.rule[0].verbs == tolist(["get", "list", "watch"])
      && kubernetes_cluster_role_v1.prometheus.rule[1].resources == tolist(["nodes/metrics"])
      && kubernetes_cluster_role_v1.prometheus.rule[1].verbs == tolist(["get"])
    )
    error_message = "never nodes/proxy, pods, Services, ConfigMaps or Secrets"
  }

  assert {
    condition = (
      !kubernetes_deployment_v1.prometheus.spec[0].template[0].spec[0].automount_service_account_token
      && kubernetes_deployment_v1.prometheus.spec[0].template[0].spec[0].volume[2].projected[0].sources[0].service_account_token[0].expiration_seconds == 600
    )
    error_message = "the token is projected at the shortest lifetime, never the default mount"
  }

  assert {
    condition     = alltrue([for job in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs : !try(job.tls_config.insecure_skip_verify, false)])
    error_message = "the kubelet's certificate is verified unless a provider opts out"
  }
}

run "kind_skips_kubelet_verification_on_the_kubelet_jobs_only" {
  command = plan

  variables {
    kubelet_insecure_skip_verify = true
  }

  assert {
    condition = [
      for job in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs : job.job_name
      if try(job.tls_config.insecure_skip_verify, false)
    ] == ["kubelet-resource", "kubelet"]
    error_message = "only the kubelet jobs skip verification"
  }
}

run "an_unbounded_retention_size_is_refused" {
  command = plan

  variables {
    retention_size = "0"
  }

  expect_failures = [var.retention_size]
}

run "an_unknown_object_store_process_is_refused" {
  command = plan

  variables {
    object_store_targets = { gateway = "seaweedfs-gateway.olf-system.svc:9327" }
  }

  expect_failures = [var.object_store_targets]
}

run "kube_state_metrics_reads_four_resources_in_the_olf_namespaces_only" {
  command = plan

  assert {
    condition     = toset(keys(kubernetes_role_v1.kube_state_metrics)) == toset(["olf-system", "olf-dev", "olf-prod"]) && toset(keys(kubernetes_role_binding_v1.kube_state_metrics)) == toset(keys(kubernetes_role_v1.kube_state_metrics))
    error_message = "a Role and RoleBinding in olf-system and each stage namespace, and nowhere else"
  }

  assert {
    condition = alltrue([
      for role in kubernetes_role_v1.kube_state_metrics : (
        length(role.rule) == 2
        && toset(role.rule[0].api_groups) == toset([""]) && toset(role.rule[0].resources) == toset(["pods"])
        && toset(role.rule[1].api_groups) == toset(["apps"]) && toset(role.rule[1].resources) == toset(["deployments", "statefulsets", "daemonsets"])
        && alltrue([for rule in role.rule : toset(rule.verbs) == toset(["list", "watch"])])
      )
    ])
    error_message = "list and watch on pods, deployments, statefulsets and daemonsets; no get, Secret or other resource"
  }

  assert {
    condition = alltrue([
      for arg in [
        "--namespaces=olf-dev,olf-prod,olf-system",
        "--resources=pods,deployments,statefulsets,daemonsets",
        "--metric-labels-allowlist=pods=[app.kubernetes.io/name,app.kubernetes.io/component]",
      ] : contains(kubernetes_deployment_v1.kube_state_metrics.spec[0].template[0].spec[0].container[0].args, arg)
    ])
    error_message = "the namespaces, resources and pod labels are bounded on the command line, without which a ClusterRole would be needed"
  }

  assert {
    condition     = endswith(kubernetes_deployment_v1.kube_state_metrics.spec[0].template[0].spec[0].container[0].image, "@sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b")
    error_message = "the image is pinned by digest"
  }

  assert {
    condition = [
      for job in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs : job.static_configs[0].targets
      if job.job_name == "kube-state-metrics"
    ] == [["kube-state-metrics.olf-system.svc:8080"]]
    error_message = "Prometheus scrapes the one kube-state-metrics Service on 8080 (F50)"
  }

  assert {
    condition = toset([
      for rule in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs[4].metric_relabel_configs :
      "${rule.regex}=${rule.replacement}" if try(rule.target_label, "") == "stage"
    ]) == toset(["olf-dev=dev", "olf-prod=prod", "olf-system=platform"])
    error_message = "a kube-state-metrics series takes its stage from its namespace, olf-system as platform"
  }
}

run "kube_state_metrics_never_watches_kube_system" {
  command = plan

  variables {
    stage_namespaces = { dev = "kube-system" }
  }

  expect_failures = [kubernetes_role_v1.kube_state_metrics]
}

run "served_series_carry_only_decision_3_labels_and_every_group_is_limited" {
  command = plan

  assert {
    condition     = yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).rule_files == ["/etc/prometheus/rules.yml"]
    error_message = "Prometheus loads the rule file from its ConfigMap"
  }

  assert {
    condition = alltrue([
      for group in yamldecode(kubernetes_config_map_v1.prometheus.data["rules.yml"]).groups : group.limit == 500 && group.interval == "1m"
    ])
    error_message = "each group limits the series a rule may produce to 500, evaluated at the Portal's finest step"
  }

  # Every served series aggregates with sum, and its by-clause names only stage, component, service or domain.
  assert {
    condition = alltrue(flatten([
      for group in yamldecode(kubernetes_config_map_v1.prometheus.data["rules.yml"]).groups : [
        for rule in group.rules : (
          can(regex("^sum (by \\(([a-z, ]*)\\) )?\\(", rule.expr))
          && length(setsubtract(
            toset(compact(split(", ", try(regex("^sum by \\(([a-z, ]*)\\)", rule.expr)[0], "")))),
            toset(["stage", "component", "service", "domain"]),
          )) == 0
        ) if startswith(rule.record, "olf:")
      ]
    ]))
    error_message = "an olf: series may carry only stage, component, service and domain (ADR 0024, Decision 3)"
  }

  assert {
    condition = toset(flatten([
      for group in yamldecode(kubernetes_config_map_v1.prometheus.data["rules.yml"]).groups : [for rule in group.rules : rule.record if startswith(rule.record, "olf:")]
      ])) == toset([
      "olf:cpu_usage_cores", "olf:memory_working_set_bytes", "olf:container_restarts",
      "olf:node_cpu_usage_cores", "olf:node_memory_working_set_bytes",
      "olf:trino_running_queries", "olf:trino_queued_queries", "olf:trino_failed_queries", "olf:trino_heap_used_bytes",
      "olf:object_store_bucket_bytes", "olf:object_store_disk_used_bytes", "olf:object_store_disk_free_bytes",
    ])
    error_message = "one rule per Prometheus-sourced series of Decision 4's catalog"
  }

  assert {
    condition = alltrue(flatten([
      for group in yamldecode(kubernetes_config_map_v1.prometheus.data["rules.yml"]).groups : [
        for rule in group.rules : strcontains(rule.expr, "group_left (component) namespace_pod:kube_pod_labels:component")
        if contains(["olf:cpu_usage_cores", "olf:memory_working_set_bytes", "olf:container_restarts"], rule.record)
      ]
    ]))
    error_message = "per-component series take component from the kube_pod_labels join, never from a raw pod label"
  }

  assert {
    condition = [
      for rule in yamldecode(kubernetes_config_map_v1.prometheus.data["rules.yml"]).groups[0].rules : rule.expr if rule.record == "olf:container_restarts"
      ] == [join("", [
        "sum by (stage, component) ((clamp_min(kube_pod_container_status_restarts_total - kube_pod_container_status_restarts_total offset 1m, 0)",
        " or (kube_pod_container_status_restarts_total unless kube_pod_container_status_restarts_total offset 1m))",
        " * on (namespace, pod) group_left (component) namespace_pod:kube_pod_labels:component)",
    ])]
    error_message = "restarts are counted per container before the sum, a new container's in full and never below zero"
  }

  assert {
    condition = contains(
      split("|", [for job in yamldecode(kubernetes_config_map_v1.prometheus.data["prometheus.yml"]).scrape_configs : job.metric_relabel_configs[0].regex if job.job_name == "trino"][0]),
      "JMX_java_lang_TYPE_Memory_ATTRIBUTE_HeapMemoryUsage_used",
    )
    error_message = "the Trino job keeps the heap series every heap rule reads"
  }
}
