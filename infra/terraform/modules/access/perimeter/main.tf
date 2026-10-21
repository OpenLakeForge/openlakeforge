terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  port = 4180
  labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "perimeter"
  }
  cookie_secret_name = "${var.release_name}-cookie"
  cookie_name        = "_olf_perimeter"

  # Route grants live in release/identity-roles.yaml and nowhere else. Grants
  # are monotonic (olf rejects anything else), so what a service admits is a
  # prefix of `precedence`, fully described by its lowest role: the "floor".
  # The portal carries no grant row; any recognised role may open it.
  precedence = var.identity_roles.precedence
  last_role  = local.precedence[length(local.precedence) - 1]
  held       = { for service, by_role in var.identity_roles.grants : service => [for role in local.precedence : role if contains(keys(by_role), role)] }
  service_floor = merge(
    { portal = local.last_role },
    { for service, roles in local.held : service => roles[length(roles) - 1] if length(roles) > 0 },
  )
  route_floor = { for ref in var.protected_refs : ref => try(local.service_floor[reverse(split("/", ref))[0]], null) }
  # One instance per distinct floor. The last role's instance always exists:
  # it answers the login callback and admits every recognised role.
  floors = toset(concat([local.last_role], compact(values(local.route_floor))))
  # An external issuer may map only some canonical roles (olf accepts any
  # non-empty subset); an unmapped role simply has no claim value to admit.
  allowed_claims = {
    for floor in local.floors : floor => distinct(flatten([
      for role in slice(local.precedence, 0, index(local.precedence, floor) + 1) : lookup(var.role_mapping, role, [])
    ]))
  }

  # Identity headers a caller could forge, removed before the proxy's own are
  # set. An empty value deletes a request header in Traefik.
  identity_headers = ["User", "Email", "Groups", "Preferred-Username", "Access-Token"]
  forwarded_identity_headers = concat(
    [for suffix in local.identity_headers : "X-Forwarded-${suffix}"],
    [for suffix in local.identity_headers : "X-Auth-Request-${suffix}"],
    ["X-Auth-Request-Redirect"],
  )

  traefik_dynamic_config = {
    http = {
      middlewares = merge(
        { "olf-strip-identity" = { headers = { customRequestHeaders = { for name in local.forwarded_identity_headers : name => "" } } } },
        {
          for floor in local.floors : "olf-auth-${floor}" => {
            forwardAuth = {
              # The proxy's catch-all path authenticates, authorizes against
              # its allowed groups and answers 202, a login redirect (browser),
              # 401 (API caller) or 403 (role-restricted).
              address = "http://${var.release_name}-${floor}.${var.namespace}.svc.cluster.local:${local.port}/"
              # Only what the proxy needs; Accept and X-Requested-With choose
              # between the login redirect and a 401.
              authRequestHeaders  = ["Cookie", "Accept", "X-Requested-With"]
              authResponseHeaders = ["X-Auth-Request-User", "X-Auth-Request-Email", "X-Auth-Request-Groups", "X-Auth-Request-Preferred-Username"]
              # A refreshed session is a new cookie; without this the browser
              # keeps the old one and the idle timeout never slides. The
              # session is split over several cookies when it outgrows one.
              addAuthCookiesToResponse = concat([local.cookie_name], [for index in range(6) : "${local.cookie_name}_${index}"])
              trustForwardHeader       = false
            }
          }
        },
      )
    }
  }

  # Replaces oauth2-proxy's error page. A 500 is what the proxy answers when
  # Dex refuses a login (user outside the organisation or its mapped teams, or
  # the GitHub App not installed), so it gets the GitHub hint. The 403 branch keys on the proxy's default
  # 403 message, which is what a session that fails the allowed-group check
  # gets; other 403s (a failed CSRF check at the callback) keep their own text.
  error_page = <<-EOT
    {{define "error.html"}}<!DOCTYPE html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{.Title}}</title></head>
    <body>
    {{if eq .Message "You do not have permission to access this resource."}}
    <h1>Access is role-restricted</h1>
    <p>Your OpenLakeForge role does not permit this service, or your account holds no OpenLakeForge role. A platform-admin assigns roles.</p>
    %{~if var.github_organization != null~}
    {{else if eq .StatusCode 500}}
    <h1>Sign-in was refused</h1>
    <p>Sign-in was refused. Check that the OpenLakeForge GitHub App is installed on ${var.github_organization} and that you are in a mapped team.</p>
    <p><a href="https://github.com/OpenLakeForge/openlakeforge/blob/main/docs/setup/github-oauth-app.md">Setup guide</a></p>
    %{~endif~}
    {{else}}
    <h1>{{.Title}}</h1>
    <p>{{.Message}}</p>
    {{end}}
    <p><a href="${var.portal_url}">Return to the portal</a></p>
    </body>
    </html>
    {{end}}
  EOT

  # Create-if-missing, like the identity credentials Job: a redeploy keeps the
  # value, so it does not sign every user out.
  cookie_script = <<-EOT
    if [ -n "$(kubectl get secret "$SECRET" -n "$NAMESPACE" --ignore-not-found -o name)" ]; then
      [ -n "$(kubectl get secret "$SECRET" -n "$NAMESPACE" -o "jsonpath={.data['cookie-secret']}")" ] || {
        echo "Secret $SECRET exists without a cookie-secret key; refusing to modify it" >&2
        exit 1
      }
      echo "Secret $SECRET exists; keeping it"
      exit 0
    fi
    kubectl create secret generic "$SECRET" -n "$NAMESPACE" \
      --from-literal=cookie-secret="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
    echo "Created Secret $SECRET"
  EOT
}

resource "terraform_data" "grants" {
  input = local.route_floor

  lifecycle {
    precondition {
      condition     = alltrue([for floor in values(local.route_floor) : floor != null])
      error_message = "A protected route has no grant row in release/identity-roles.yaml; the perimeter denies a service with no grant, so it cannot be routed without one."
    }
    # With no --allowed-group the proxy admits every authenticated user.
    precondition {
      condition     = alltrue([for claims in values(local.allowed_claims) : length(claims) > 0])
      error_message = "identity role_mapping maps no role that a protected route admits, so its proxy would have no allowed group; map at least one role at or above every route's lowest role."
    }
  }
}

# The cookie secret is generated in the cluster and never passes through
# Terraform, which only references it (docs/architecture/identity-credentials.md).
resource "kubernetes_service_account_v1" "cookie" {
  metadata {
    name      = "${var.release_name}-cookie"
    namespace = var.namespace
    labels    = local.labels
  }
}

# `create` cannot be limited by resourceNames, so it covers every Secret in the
# namespace (inventoried in docs/architecture/identity-credentials.md); reading
# is limited to the Secret this Job owns.
resource "kubernetes_role_v1" "cookie" {
  metadata {
    name      = "${var.release_name}-cookie"
    namespace = var.namespace
    labels    = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.cookie_secret_name]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "cookie" {
  metadata {
    name      = "${var.release_name}-cookie"
    namespace = var.namespace
    labels    = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.cookie.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.cookie.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "cookie_secret" {
  metadata {
    name      = "${local.cookie_secret_name}-${substr(sha256(local.cookie_script), 0, 10)}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "perimeter-cookie-secret"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.cookie.metadata[0].name

        container {
          name    = "cookie-secret"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.cookie_script]

          env {
            name  = "SECRET"
            value = local.cookie_secret_name
          }
          env {
            name = "NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
    update = "5m"
  }

  depends_on = [kubernetes_role_binding_v1.cookie]
}

resource "kubernetes_config_map_v1" "templates" {
  metadata {
    name      = "${var.release_name}-templates"
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    "error.html" = local.error_page
  }
}

resource "kubernetes_deployment_v1" "proxy" {
  for_each = local.floors

  metadata {
    name      = "${var.release_name}-${each.key}"
    namespace = var.namespace
    labels    = merge(local.labels, { "app.kubernetes.io/name" = "${var.release_name}-${each.key}", "openlakeforge.io/role-floor" = each.key })
  }

  spec {
    replicas = 1

    selector {
      match_labels = { "app.kubernetes.io/name" = "${var.release_name}-${each.key}" }
    }

    template {
      metadata {
        # One constant label so a NetworkPolicy can select every floor (ADR 0017).
        labels = merge(local.labels, { "app.kubernetes.io/name" = "${var.release_name}-${each.key}", "openlakeforge.io/workload" = "oauth2-proxy" })
      }

      spec {
        container {
          name  = "oauth2-proxy"
          image = var.image
          args = concat(
            [
              "--provider=oidc",
              "--oidc-issuer-url=${var.issuer_url}",
              "--client-id=${var.client_id}",
              "--oidc-groups-claim=${var.role_claim}",
              # Overrides the default scopes: Dex needs `groups` and
              # `offline_access` asked for, an external issuer may not.
              "--scope=${var.scope}",
              "--code-challenge-method=S256",
              "--email-domain=*",
              # The decision is the group claim; a user an administrator
              # created without verifying the address is still let in.
              "--insecure-oidc-allow-unverified-email=true",
              "--redirect-url=${var.route_scheme}://${var.callback_host}/oauth2/callback",
              "--whitelist-domain=.${var.base_domain}",
              "--cookie-name=${local.cookie_name}",
              "--cookie-domain=.${var.base_domain}",
              "--cookie-samesite=lax",
              # Plain HTTP on *.localhost: a Secure cookie would never be sent back.
              "--cookie-secure=${var.route_scheme == "https"}",
              # 30 min idle: the cookie is re-issued on every refresh, so it
              # lapses only after 30 min with no request. The 10 h absolute
              # bound is the issuer's SSO session maximum, which ends the
              # refresh chain (modules/identity/dex).
              "--cookie-expire=30m",
              "--cookie-refresh=4m",
              # One CSRF/PKCE cookie per login attempt, so two sign-ins started
              # together (two tabs) do not overwrite each other's code verifier.
              # https://oauth2-proxy.github.io/oauth2-proxy/7.15.x/configuration/overview#command-line-options
              "--cookie-csrf-per-request=true",
              "--cookie-csrf-expire=5m",
              "--reverse-proxy=true",
              "--upstream=static://202",
              "--http-address=0.0.0.0:${local.port}",
              "--skip-provider-button=true",
              # oauth2-proxy defaults to approval_prompt=force, which makes Dex
              # show its consent page on every sign-in despite skipApprovalScreen.
              "--approval-prompt=auto",
              "--set-xauthrequest=true",
              # Unattended callers get 401 JSON, never a login page.
              "--api-route=^/(api|graphql|v1)(/|$)",
              "--custom-templates-dir=/templates",
              "--silence-ping-logging=true",
            ],
            [for claim in local.allowed_claims[each.key] : "--allowed-group=${claim}"],
          )

          port {
            name           = "http"
            container_port = local.port
          }

          env {
            name = "OAUTH2_PROXY_CLIENT_SECRET"
            value_from {
              secret_key_ref {
                name = var.client_secret_ref.name
                key  = var.client_secret_ref.key
              }
            }
          }
          env {
            name = "OAUTH2_PROXY_COOKIE_SECRET"
            value_from {
              secret_key_ref {
                name = local.cookie_secret_name
                key  = "cookie-secret"
              }
            }
          }
          dynamic "env" {
            for_each = var.ca_configmap_name == null ? [] : [1]
            content {
              # The issuer's certificate is signed by the local CA, which is the
              # only upstream this process talks to.
              name  = "SSL_CERT_FILE"
              value = "/ca/ca.crt"
            }
          }

          dynamic "volume_mount" {
            for_each = var.ca_configmap_name == null ? [] : [1]
            content {
              name       = "ca"
              mount_path = "/ca"
              read_only  = true
            }
          }
          volume_mount {
            name       = "templates"
            mount_path = "/templates"
            read_only  = true
          }

          readiness_probe {
            http_get {
              path = "/ping"
              port = "http"
            }
            period_seconds = 10
          }
          liveness_probe {
            http_get {
              path = "/ping"
              port = "http"
            }
            period_seconds        = 20
            initial_delay_seconds = 10
          }

          resources {
            requests = {
              cpu    = "10m"
              memory = "32Mi"
            }
            limits = {
              memory = "128Mi"
            }
          }

          security_context {
            run_as_user                = 65532
            run_as_non_root            = true
            read_only_root_filesystem  = true
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }
        }

        dynamic "volume" {
          for_each = var.ca_configmap_name == null ? [] : [1]
          content {
            name = "ca"
            config_map {
              name = var.ca_configmap_name
            }
          }
        }
        volume {
          name = "templates"
          config_map {
            name = kubernetes_config_map_v1.templates.metadata[0].name
          }
        }
      }
    }
  }

  timeouts {
    create = "5m"
    update = "5m"
  }

  depends_on = [kubernetes_job_v1.cookie_secret, terraform_data.grants]
}

resource "kubernetes_service_v1" "proxy" {
  for_each = local.floors

  metadata {
    name      = "${var.release_name}-${each.key}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    selector = { "app.kubernetes.io/name" = "${var.release_name}-${each.key}" }

    port {
      name        = "http"
      port        = local.port
      target_port = "http"
    }
  }
}
