# ADR 0022 decisions 1 to 3: the DaemonSet is bounded, pinned, credentialed by
# Secret reference and routed by namespace.

mock_provider "kubernetes" {}

variables {
  namespace               = "olf-system"
  bucket                  = "ops"
  s3_endpoint_url         = "http://seaweedfs-s3.olf-system:8333"
  s3_region               = "us-east-1"
  credentials_secret_name = "log-collector-s3-creds"
  routes = {
    dev      = { namespace = "olf-dev", key_prefix = "activations/dev/logs/v2/" }
    prod     = { namespace = "olf-prod", key_prefix = "activations/prod/logs/v2/" }
    platform = { namespace = "olf-system", key_prefix = "platform/logs/v2/" }
  }
}

run "memory_and_disk_are_bounded" {
  command = plan

  assert {
    condition     = kubernetes_daemon_set_v1.collector.spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "256Mi"
    error_message = "the container needs a memory limit"
  }

  assert {
    condition     = length(regexall("Mem_Buf_Limit +16MB", kubernetes_config_map_v1.collector.data["fluent-bit.conf"])) == 3
    error_message = "every tail input needs Mem_Buf_Limit"
  }

  assert {
    condition = (
      length(regexall("store_dir_limit_size +512M", kubernetes_config_map_v1.collector.data["fluent-bit.conf"])) == 3
      && length(regexall("storage.total_limit_size +256M", kubernetes_config_map_v1.collector.data["fluent-bit.conf"])) == 3
      && !strcontains(kubernetes_config_map_v1.collector.data["fluent-bit.conf"], "store_dir_limit_size 0")
      && length(regexall("quarantine_dir_limit_size +128M", kubernetes_config_map_v1.collector.data["fluent-bit.conf"])) == 3
      && length(regexall("retry_exhausted_action +quarantine", kubernetes_config_map_v1.collector.data["fluent-bit.conf"])) == 3
    )
    error_message = "every S3 output needs an explicit spool limit and chunk limit, never the unlimited default"
  }
}

run "an_unbounded_spool_is_refused" {
  command = plan

  variables {
    spool_limit_size = "0"
  }

  expect_failures = [var.spool_limit_size]
}

run "an_unbounded_quarantine_is_refused" {
  command = plan

  variables {
    quarantine_limit_size = "0"
  }

  expect_failures = [var.quarantine_limit_size]
}

run "empty_missing_serializes_as_an_array" {
  command = plan

  assert {
    condition     = strcontains(kubernetes_config_map_v1.collector.data["fluent-bit.conf"], "type_array_key              missing")
    error_message = "the lua filter must declare missing an array so an empty one is [] not {}"
  }
}

# The test runs on a copy of the module, which cannot read the catalog;
# `olf check components` compares this default with `fluent_bit` there.
run "image_is_pinned_by_digest" {
  command = plan

  assert {
    condition     = can(regex("^fluent/fluent-bit:5\\.2\\.0@sha256:[0-9a-f]{64}$", kubernetes_daemon_set_v1.collector.spec[0].template[0].spec[0].container[0].image))
    error_message = "the collector image must be the digest-pinned Fluent Bit 5.2.0 of the catalog"
  }
}

run "credentials_are_secret_references_only" {
  command = plan

  assert {
    condition = alltrue([
      for e in kubernetes_daemon_set_v1.collector.spec[0].template[0].spec[0].container[0].env :
      e.value == null && e.value_from[0].secret_key_ref[0].name == "log-collector-s3-creds"
    ])
    error_message = "every credential env var must come from the Secret, never carry a value"
  }

  assert {
    condition     = !strcontains(kubernetes_config_map_v1.collector.data["fluent-bit.conf"], "access_key")
    error_message = "the configuration must carry no credential"
  }
}

run "each_namespace_routes_to_its_own_prefix" {
  command = plan

  assert {
    condition = (
      strcontains(kubernetes_config_map_v1.collector.data["fluent-bit.conf"], "Path                        /var/log/containers/*_olf-dev_*.log")
      && strcontains(kubernetes_config_map_v1.collector.data["fluent-bit.conf"], "s3_key_format               /activations/dev/logs/v2/namespace=$TAG[0]/date=$TAG[1]/dev-$UUID.ndjson.gz")
      && strcontains(kubernetes_config_map_v1.collector.data["fluent-bit.conf"], "s3_key_format               /platform/logs/v2/namespace=$TAG[0]/date=$TAG[1]/platform-$UUID.ndjson.gz")
    )
    error_message = "the input's namespace, not a record field, selects the stage prefix"
  }
}

run "rbac_is_read_only_on_pods_and_namespaces" {
  command = plan

  assert {
    condition = (
      toset(flatten([for r in kubernetes_cluster_role_v1.collector.rule : r.resources])) == toset(["pods", "namespaces"])
      && toset(flatten([for r in kubernetes_cluster_role_v1.collector.rule : r.verbs])) == toset(["get", "list", "watch"])
    )
    error_message = "the ClusterRole must be get, list and watch on pods and namespaces only"
  }
}
