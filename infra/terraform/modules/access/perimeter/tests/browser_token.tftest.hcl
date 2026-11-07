# #799: the session's ID token reaches the platform API's route and no other.

mock_provider "kubernetes" {}

variables {
  namespace     = "olf-system"
  base_domain   = "olf.localhost"
  callback_host = "oauth2.olf.localhost"
  portal_url    = "http://olf.localhost"
  identity_roles = {
    precedence = ["platform-admin", "data-engineer", "viewer"]
    grants     = { orchestration = { "platform-admin" = "a", "data-engineer" = "b" } }
  }
  role_mapping      = { "platform-admin" = ["g1"], "data-engineer" = ["g2"], viewer = ["g3"] }
  role_claim        = "groups"
  issuer_url        = "http://auth.olf.localhost"
  client_id         = "perimeter"
  cli_client_id     = "olf-cli"
  client_secret_ref = { name = "s", key = "k" }
  protected_refs    = ["shared/portal", "stage/dev/orchestration"]
}

run "only_the_api_group_middleware_copies_authorization_upstream" {
  command = plan

  variables {
    login_audience = "olf-cli"
  }

  assert {
    condition = [
      for name, middleware in output.traefik_dynamic_config.http.middlewares : name
      if contains(try(middleware.forwardAuth.authResponseHeaders, []), "Authorization")
    ] == ["olf-auth-${output.api_group}"]
    error_message = "exactly one middleware, the API group's, may return Authorization to the backend"
  }

  assert {
    condition     = output.group_middlewares[output.api_group] == ["olf-strip-identity@file", "olf-auth-${output.api_group}@file"]
    error_message = "the API group must strip forged headers and then use its own auth middleware"
  }

  assert {
    condition     = alltrue([for floor, chain in output.group_middlewares : !strcontains(join(",", chain), "-api") if floor != output.api_group])
    error_message = "no other floor's chain may include the API middleware"
  }
}

run "dex_login_asks_for_the_cli_audience_and_the_proxy_accepts_it" {
  command = plan

  variables {
    login_audience = "olf-cli"
  }

  assert {
    condition = alltrue([
      for floor, deployment in kubernetes_deployment_v1.proxy : (
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--set-authorization-header=true")
        && contains(deployment.spec[0].template[0].spec[0].container[0].args, "--oidc-extra-audience=olf-cli")
        && contains(deployment.spec[0].template[0].spec[0].container[0].args, "--scope=openid email profile federated:id audience:server:client_id:olf-cli")
      )
    ])
    error_message = "every proxy must request the olf-cli audience and federated:id, accept the audience and return the Authorization header"
  }
}

run "another_issuer_requests_no_audience" {
  command = plan

  assert {
    condition = alltrue([
      for floor, deployment in kubernetes_deployment_v1.proxy : (
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--scope=openid email profile")
        && !anytrue([for arg in deployment.spec[0].template[0].spec[0].container[0].args : startswith(arg, "--oidc-extra-audience")])
      )
    ])
    error_message = "without a login audience the scope stays as given and no extra audience is accepted"
  }
}

# #991: keys and codes go to the issuer's Service; the public URL stays the
# token's iss and the login page.
run "internal_url_skips_discovery_and_the_public_url" {
  command = plan

  variables {
    login_audience = "olf-cli"
    internal_url   = "http://dex.olf-system.svc.cluster.local:5556"
  }

  assert {
    condition = alltrue([
      for floor, deployment in kubernetes_deployment_v1.proxy : alltrue([
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--skip-oidc-discovery=true"),
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--login-url=http://auth.olf.localhost/auth"),
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--redeem-url=http://dex.olf-system.svc.cluster.local:5556/token"),
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--oidc-jwks-url=http://dex.olf-system.svc.cluster.local:5556/keys"),
        contains(deployment.spec[0].template[0].spec[0].container[0].args, "--oidc-issuer-url=http://auth.olf.localhost"),
        !anytrue([for arg in deployment.spec[0].template[0].spec[0].container[0].args : startswith(arg, "--extra-jwt-issuers")]),
      ])
    ])
    error_message = "every proxy must skip discovery, take keys and codes from the internal URL and verify bearer tokens without an extra issuer"
  }
}

run "no_internal_url_discovers_the_issuer" {
  command = plan

  assert {
    condition = alltrue([
      for floor, deployment in kubernetes_deployment_v1.proxy :
      !contains(deployment.spec[0].template[0].spec[0].container[0].args, "--skip-oidc-discovery=true")
    ])
    error_message = "an external issuer is discovered at its public URL"
  }
}
