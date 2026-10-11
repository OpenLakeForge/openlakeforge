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
    var.dynamic_config == null ? [] : [yamlencode({ providers = { file = { enabled = true, content = var.dynamic_config } } })],
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
