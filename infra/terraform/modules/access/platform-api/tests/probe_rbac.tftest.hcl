# #1126: the service probes' RBAC is exactly the Role ADR 0021 decision 7 names.

mock_provider "kubernetes" {}

variables {
  namespace    = "olf-system"
  image        = "platform-api:test"
  issuer_url   = "http://auth.olf.localhost"
  audiences    = ["olf-cli"]
  role_claim   = "groups"
  role_mapping = { viewer = ["g"] }
  precedence   = ["viewer"]
  stages       = ["dev", "prod"]

  bootstrap_job_image = "bootstrap:test"
}

run "no_probe_rbac_and_no_token_by_default" {
  command = plan

  assert {
    condition     = length(kubernetes_role_v1.probe) == 0 && length(kubernetes_role_binding_v1.probe) == 0
    error_message = "without probe_namespaces no Role or RoleBinding may exist"
  }

  assert {
    condition     = kubernetes_deployment_v1.platform_api.spec[0].template[0].spec[0].automount_service_account_token == false
    error_message = "without probe_namespaces the pod must mount no ServiceAccount token"
  }
}

run "probe_role_lists_pods_and_controllers_in_each_stage_namespace_only" {
  command = plan

  variables {
    probe_namespaces = { dev = "olf-dev", prod = "olf-prod" }
  }

  assert {
    condition     = toset(keys(kubernetes_role_v1.probe)) == toset(["dev", "prod"]) && kubernetes_role_v1.probe["dev"].metadata[0].namespace == "olf-dev"
    error_message = "one Role per stage namespace, in that namespace"
  }

  assert {
    condition = (
      toset([for r in kubernetes_role_v1.probe["dev"].rule : "${join(",", r.api_groups)}|${join(",", sort(r.resources))}|${join(",", r.verbs)}"])
      == toset(["|pods|list", "apps|deployments,statefulsets|list"])
    )
    error_message = "the Role must grant exactly list on pods, deployments and statefulsets"
  }

  assert {
    condition     = kubernetes_role_binding_v1.probe["prod"].subject[0].namespace == "olf-system" && kubernetes_role_binding_v1.probe["prod"].role_ref[0].kind == "Role"
    error_message = "the binding must be a RoleBinding to the API's ServiceAccount in its own namespace"
  }

  assert {
    condition = (
      kubernetes_role_binding_v1.probe["prod"].subject[0].kind == "ServiceAccount"
      && kubernetes_role_binding_v1.probe["prod"].subject[0].name == kubernetes_deployment_v1.platform_api.spec[0].template[0].spec[0].service_account_name
    )
    error_message = "the binding subject must be the ServiceAccount the pod runs as"
  }

  assert {
    condition     = kubernetes_role_binding_v1.probe["prod"].role_ref[0].name == kubernetes_role_v1.probe["prod"].metadata[0].name
    error_message = "the binding must reference the Role of its own namespace"
  }

  assert {
    condition     = kubernetes_deployment_v1.platform_api.spec[0].template[0].spec[0].automount_service_account_token == true
    error_message = "the pod needs its token to list"
  }
}

run "probe_namespaces_refuse_the_apis_own_namespace" {
  command = plan

  variables {
    probe_namespaces = { dev = "olf-system" }
  }

  expect_failures = [kubernetes_role_v1.probe]
}

run "probe_namespaces_refuse_kube_system" {
  command = plan

  variables {
    probe_namespaces = { dev = "kube-system" }
  }

  expect_failures = [kubernetes_role_v1.probe]
}

# #1156: the API is told which namespaces to probe only where it holds a token.
run "settings_name_no_probe_namespace_without_probe_namespaces" {
  command = plan

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).stage_namespaces == {}
    error_message = "without probe_namespaces the API must be told of no namespace to probe"
  }
}

run "settings_carry_the_probe_namespaces_and_trino_base_urls" {
  command = plan

  variables {
    probe_namespaces = { dev = "olf-dev" }
    trino_urls       = { dev = "http://trino.olf-dev.svc.cluster.local:8080" }
  }

  assert {
    condition = (
      jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).stage_namespaces == { dev = "olf-dev" }
      && jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).trino_urls == { dev = "http://trino.olf-dev.svc.cluster.local:8080" }
    )
    error_message = "the settings must render the namespace map and the Trino base URL per stage"
  }
}

# ADR 0021 decision 3: a service caller's token is checked with a TokenReview, the API's one ClusterRole.
run "no_cluster_role_without_a_service_caller" {
  command = plan

  assert {
    condition     = length(kubernetes_cluster_role_v1.token_review) == 0 && length(kubernetes_cluster_role_binding_v1.token_review) == 0
    error_message = "without service_callers no ClusterRole or binding may exist"
  }
}

run "a_service_caller_gets_tokenreview_create_and_nothing_else" {
  command = plan

  variables {
    service_callers = { "alert-evaluator" = { namespace = "olf-system", service_account = "alert-evaluator" } }
  }

  assert {
    condition = (
      length(kubernetes_cluster_role_v1.token_review[0].rule) == 1
      && join(",", kubernetes_cluster_role_v1.token_review[0].rule[0].api_groups) == "authentication.k8s.io"
      && join(",", kubernetes_cluster_role_v1.token_review[0].rule[0].resources) == "tokenreviews"
      && join(",", kubernetes_cluster_role_v1.token_review[0].rule[0].verbs) == "create"
    )
    error_message = "the ClusterRole must grant create on tokenreviews only"
  }

  assert {
    condition     = kubernetes_cluster_role_binding_v1.token_review[0].subject[0].name == "platform-api" && kubernetes_cluster_role_binding_v1.token_review[0].subject[0].namespace == "olf-system"
    error_message = "only the API's own ServiceAccount is bound"
  }

  assert {
    condition     = kubernetes_deployment_v1.platform_api.spec[0].template[0].spec[0].automount_service_account_token == true
    error_message = "the API needs its token to ask the API server for the review"
  }

  assert {
    condition     = jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).service_callers["alert-evaluator"].service_account == "alert-evaluator" && jsondecode(kubernetes_config_map_v1.settings.data["settings.json"]).service_audience == "openlakeforge-platform-api"
    error_message = "settings name the one ServiceAccount and the audience"
  }
}

run "an_unknown_service_principal_is_refused" {
  command = plan

  variables {
    service_callers = { root = { namespace = "olf-system", service_account = "x" } }
  }

  expect_failures = [var.service_callers]
}
