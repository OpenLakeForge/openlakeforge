locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "traefik"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
  # Traefik attaches middleware per Ingress, so routes with different access
  # groups need separate Ingresses. A namespace still has one wildcard
  # certificate, issued by cert-manager's ingress-shim from the one Ingress per
  # namespace (the primary) that carries the issuer annotation.
  ingresses = {
    for key in distinct([for route in values(var.routes) : "${route.namespace}/${route.group}"]) : key => {
      namespace = split("/", key)[0]
      group     = split("/", key)[1]
    }
  }
  namespace_hosts = { for route in values(var.routes) : route.namespace => route.tls_host... }
  # The file provider carries both the middlewares and the transports a backend
  # that speaks HTTPS is verified with. Null (no provider content) when neither
  # is set, so a cluster without them gets the same values as before.
  dynamic_config = var.dynamic_config == null && length(var.backend_transports) == 0 ? null : {
    http = merge(try(var.dynamic_config.http, {}), {
      for key, transports in {
        serversTransports = {
          for name, transport in var.backend_transports : name => {
            rootCAs    = ["/etc/traefik/backend-ca/${name}/ca.crt"]
            serverName = transport.server_name
          }
        }
      } : key => transports if length(var.backend_transports) > 0
    })
  }
  primary_group = {
    for namespace in keys(local.namespace_hosts) : namespace => sort(distinct([for route in values(var.routes) : route.group if route.namespace == namespace]))[0]
  }
}

resource "helm_release" "traefik" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  wait    = true
  timeout = 600

  values = concat(
    [file(var.base_values_file)],
    # Middlewares are defined in the file provider, not as CRD objects: a
    # Middleware kind cannot be planned before this release installs its CRDs.
    local.dynamic_config == null ? [] : [yamlencode({ providers = { file = { enabled = true, content = local.dynamic_config } } })],
    # The chart mounts each Secret by its own name; it holds the CA as ca.crt.
    length(var.backend_transports) == 0 ? [] : [yamlencode({
      volumes = [for name, transport in var.backend_transports : { name = transport.ca_secret, mountPath = "/etc/traefik/backend-ca/${name}", type = "secret" }]
    })],
  )
}

# A standard Ingress, not an IngressRoute: no CRD has to exist at plan time.
# A route's access group selects its middlewares through the
# traefik.ingress.kubernetes.io/router.middlewares annotation.
resource "kubernetes_ingress_v1" "routes" {
  for_each = local.ingresses

  metadata {
    name      = each.value.group == "" ? "openlakeforge-routes" : "openlakeforge-routes-${each.value.group}"
    namespace = each.value.namespace
    annotations = merge(
      { "traefik.ingress.kubernetes.io/router.entrypoints" = "websecure" },
      local.primary_group[each.value.namespace] == each.value.group ? { "cert-manager.io/cluster-issuer" = var.cluster_issuer_name } : {},
      each.value.group == "" ? {} : { "traefik.ingress.kubernetes.io/router.middlewares" = join(",", var.group_middlewares[each.value.group]) },
    )
  }

  spec {
    ingress_class_name = helm_release.traefik.name

    tls {
      hosts = (
        local.primary_group[each.value.namespace] == each.value.group
        ? distinct(local.namespace_hosts[each.value.namespace])
        : distinct([for route in values(var.routes) : route.tls_host if route.namespace == each.value.namespace && route.group == each.value.group])
      )
      secret_name = "openlakeforge-routes-tls"
    }

    dynamic "rule" {
      for_each = { for ref, route in var.routes : ref => route if route.namespace == each.value.namespace && route.group == each.value.group }
      content {
        host = rule.value.host
        http {
          path {
            path      = rule.value.path
            path_type = "Prefix"
            backend {
              service {
                name = rule.value.service_name
                port {
                  number = rule.value.service_port
                }
              }
            }
          }
        }
      }
    }
  }
}
