locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "traefik"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
  # One Ingress per namespace: its routes share one wildcard certificate,
  # which cert-manager's ingress-shim issues once per TLS secret.
  route_namespaces = { for route in values(var.routes) : route.namespace => route.tls_host... }
}

resource "helm_release" "traefik" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  wait    = true
  timeout = 600

  values = [file(var.base_values_file)]
}

# A standard Ingress, not an IngressRoute: no CRD has to exist at plan time,
# and #176 can attach its auth middleware through the
# traefik.ingress.kubernetes.io/router.middlewares annotation without
# touching hosts.
resource "kubernetes_ingress_v1" "routes" {
  for_each = local.route_namespaces

  metadata {
    name      = "openlakeforge-routes"
    namespace = each.key
    annotations = {
      "cert-manager.io/cluster-issuer"                   = var.cluster_issuer_name
      "traefik.ingress.kubernetes.io/router.entrypoints" = "websecure"
    }
  }

  spec {
    ingress_class_name = helm_release.traefik.name

    tls {
      hosts       = distinct(each.value)
      secret_name = "openlakeforge-routes-tls"
    }

    dynamic "rule" {
      for_each = { for ref, route in var.routes : ref => route if route.namespace == each.key }
      content {
        host = rule.value.host
        http {
          path {
            path      = "/"
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
