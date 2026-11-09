# #990: X-Forwarded-Proto and -Host are honoured from the customer's edge only.
# Traefik rewrites them from the connection for any peer outside trustedIPs, so
# a request sent straight to the web entrypoint with a forged
# X-Forwarded-Proto: https reaches a backend as plain HTTP. The two things that
# would break that are a missing allowlist and the chart's `insecure` switch,
# which trusts every peer.

mock_provider "helm" {}
mock_provider "kubernetes" {}

variables {
  namespace        = "olf-system"
  base_values_file = "tests/base-values.yaml"
  route_tls        = false
  routes           = {}
}

run "trusts_only_the_named_upstream" {
  command = plan

  variables {
    trusted_proxies = ["203.0.113.0/24"]
  }

  assert {
    condition = anytrue([
      for values in helm_release.traefik.values :
      try(yamldecode(values).ports.web.forwardedHeaders.trustedIPs == ["203.0.113.0/24"], false)
    ])
    error_message = "the web entrypoint must trust forwarded headers from exactly the configured upstream"
  }

  assert {
    condition = !anytrue([
      for values in helm_release.traefik.values :
      try(yamldecode(values).ports.web.forwardedHeaders.insecure, false)
    ])
    error_message = "forwardedHeaders.insecure would trust every peer"
  }

  assert {
    condition = anytrue([
      for values in helm_release.traefik.values :
      try(yamldecode(values).ports.websecure.hostPort == null, false)
    ])
    error_message = "without route certificates 443 must not be published on the host"
  }
}

run "trusts_nobody_by_default" {
  command = plan

  assert {
    condition = !anytrue([
      for values in helm_release.traefik.values :
      can(yamldecode(values).ports.web.forwardedHeaders)
    ])
    error_message = "no upstream configured must leave the chart's default, which trusts no peer"
  }
}

run "rejects_a_malformed_upstream" {
  command = plan

  variables {
    trusted_proxies = ["not-a-cidr"]
  }

  expect_failures = [var.trusted_proxies]
}
