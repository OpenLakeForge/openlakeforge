# ADR 0024, Decisions 3 and 4: the platform API serves only these `olf:`
# series. Each one sums its raw input down to Decision 3's labels (`stage`,
# `component`, `service`), so a pod, container, node, bucket or run ID never
# reaches a served series. Rules evaluate once a minute, the Portal's finest step.
locals {
  # One series per pod in the namespaces kube-state-metrics watches, carrying the
  # pod's app.kubernetes.io/name as `component`, or `unlabelled`. Not served,
  # so it has no `olf:` prefix. topk keeps one per pod when a recreated pod
  # briefly has two label sets, which would otherwise fail every join below.
  pod_component_rule = "namespace_pod:kube_pod_labels:component"
  pod_component = join("", [
    "topk by (namespace, pod) (1, max by (namespace, pod, component) (",
    "label_replace(label_replace(kube_pod_labels, \"component\", \"$1\", \"label_app_kubernetes_io_name\", \"(.+)\"),",
    " \"component\", \"unlabelled\", \"label_app_kubernetes_io_name\", \"\")))",
  ])
  # The component a pod had at any evaluation in the last two minutes, so a pod
  # deleted since the last one still has it for its final restarts.
  by_component = "on (namespace, pod) group_left (component) topk by (namespace, pod) (1, last_over_time(${local.pod_component_rule}[2m]))"
  # Per container, the highest count of the minute ending now less the last
  # count of the minute before. Range selectors keep the samples of a pod
  # deleted before this evaluation, which an instant selector reads as stale;
  # the windows are left-open, so a sample counts in exactly one minute.
  # clamp_min drops a counter that goes back.
  #
  # A container with no sample the minute before counts its whole value only
  # if its pod was created in the last two minutes: kube-state-metrics' counter
  # starts at 0 with the container, and a crash on start is the case worth
  # charting. One seen again after a scrape gap or lost history counts 0, so a
  # gap undercounts the restarts within it and never replays a lifetime.
  restarts        = "kube_pod_container_status_restarts_total"
  restarts_now    = "max_over_time(${local.restarts}[1m])"
  restarts_before = "last_over_time(${local.restarts}[1m] offset 1m)"
  pod_is_new      = "on (namespace, pod) (time() - max_over_time(kube_pod_created[1m]) < 120)"
  restarts_per_minute = join(" or ", [
    "(clamp_min(${local.restarts_now} - ${local.restarts_before}, 0)",
    "(${local.restarts_now} and ${local.pod_is_new})",
    "${local.restarts_now} * 0)",
  ])

  # A JMX composite attribute is exposed as JMX_<domain>_TYPE_<type>_ATTRIBUTE_<attribute>_<item>;
  # main.tf's Trino job keeps it. Here so the rules render from this file alone, as the promtool test does.
  trino_heap_used = "JMX_java_lang_TYPE_Memory_ATTRIBUTE_HeapMemoryUsage_used"

  rule_groups = {
    kubernetes = [
      { record = local.pod_component_rule, expr = local.pod_component },
      { record = "olf:cpu_usage_cores", expr = "sum by (stage, component) (rate(container_cpu_usage_seconds_total{stage!=\"\"}[2m]) * ${local.by_component})" },
      { record = "olf:memory_working_set_bytes", expr = "sum by (stage, component) (container_memory_working_set_bytes{stage!=\"\"} * ${local.by_component})" },
      # Restarts in the minute before each evaluation, per container, so the
      # API sums the series over a step. A counter summed across pods would drop
      # whenever a pod goes, which increase() reads as a reset and counts again.
      { record = "olf:container_restarts", expr = "sum by (stage, component) (${local.restarts_per_minute} * ${local.by_component})" },
      { record = "olf:node_cpu_usage_cores", expr = "sum (rate(node_cpu_usage_seconds_total[2m]))" },
      { record = "olf:node_memory_working_set_bytes", expr = "sum (node_memory_working_set_bytes)" },
    ]
    trino = [
      { record = "olf:trino_running_queries", expr = "sum by (stage) (trino_execution_name_QueryManager_RunningQueries)" },
      { record = "olf:trino_queued_queries", expr = "sum by (stage) (trino_execution_name_QueryManager_QueuedQueries)" },
      # A counter of one coordinator per stage; the API takes its increase per step.
      { record = "olf:trino_failed_queries", expr = "sum by (stage) (trino_execution_name_QueryManager_FailedQueries)" },
      { record = "olf:trino_heap_used_bytes", expr = "sum by (stage) (${local.trino_heap_used})" },
    ]
    object-store = [
      # A bucket outside the stage map has no stage and is left out.
      { record = "olf:object_store_bucket_bytes", expr = "sum by (stage) (SeaweedFS_s3_bucket_size_bytes{stage!=\"\"})" },
      { record = "olf:object_store_disk_used_bytes", expr = "sum by (service) (SeaweedFS_volumeServer_resource{type=\"used\"})" },
      { record = "olf:object_store_disk_free_bytes", expr = "sum by (service) (SeaweedFS_volumeServer_resource{type=\"free\"})" },
    ]
  }

  rules_yaml = yamlencode({
    groups = [
      # limit is per rule: one producing more than 500 series fails its
      # evaluation and records nothing, instead of growing the database.
      for name, rules in local.rule_groups : { name = "olf-${name}", interval = "1m", limit = 500, rules = rules }
    ]
  })
}
