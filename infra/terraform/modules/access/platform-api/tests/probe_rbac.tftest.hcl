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
