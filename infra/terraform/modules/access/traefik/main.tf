locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "traefik"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
  # Traefik attaches middleware per Ingress, so routes with different access
  # groups need separate Ingresses. Every route is under one wildcard, and
  # Traefik's TLS store is global and keyed by SNI, so a certificate per
  # namespace would be a lottery over which one is served. One certificate is
  # issued by cert-manager's ingress-shim from the primary Ingress in this
  # module's namespace and is the store's default; Ingresses elsewhere carry an
  # empty tls block, which only enables TLS on their router.
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
  secret_name = "openlakeforge-routes-tls"
  edge_name   = "edge"
  cert_secret = "${local.edge_name}-tls"
  # The chart mounts the Secret at mountPath itself, so its files sit directly under it. A path that
  # does not exist is read by Traefik as inline PEM and fails with "no PEM data".
  cert_dir = "/etc/traefik/edge-tls"
  edge_config = {
    http = {
      routers = {
        everything = {
          rule        = "HostRegexp(`.+`)"
          entryPoints = ["websecure"]
          service     = "ingress"
          tls         = {}
        }
      }
      services = {
        ingress = { loadBalancer = { servers = [{ url = "http://${var.release_name}.${var.namespace}.svc.cluster.local:80" }] } }
      }
    }
    tls = {
      certificates = [{ certFile = "${local.cert_dir}/tls.crt", keyFile = "${local.cert_dir}/tls.key" }]
      stores       = { default = { defaultCertificate = { certFile = "${local.cert_dir}/tls.crt", keyFile = "${local.cert_dir}/tls.key" } } }
    }
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
    var.route_tls ? [yamlencode({ tlsStore = { default = { defaultCertificate = { secretName = local.secret_name } } } })] : [],
    # No route certificates (plain HTTP, or HTTPS terminated upstream): 443 is not published on the host.
    var.route_tls ? [] : [yamlencode({ ports = { websecure = { hostPort = null } } })],
    # Traefik overwrites X-Forwarded-* from any peer outside trustedIPs, so a
    # forged X-Forwarded-Proto never reaches a backend as https. Only the
    # customer's edge, named here, may assert the original scheme and host.
    length(var.trusted_proxies) == 0 ? [] : [yamlencode({ ports = { web = { forwardedHeaders = { trustedIPs = var.trusted_proxies } } } })],
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
      { "traefik.ingress.kubernetes.io/router.entrypoints" = var.route_tls ? "websecure" : "web" },
      var.route_tls && each.value.namespace == var.namespace && local.primary_group[each.value.namespace] == each.value.group ? { "cert-manager.io/cluster-issuer" = var.cluster_issuer_name } : {},
      each.value.group == "" ? {} : { "traefik.ingress.kubernetes.io/router.middlewares" = join(",", var.group_middlewares[each.value.group]) },
    )
  }

  spec {
    ingress_class_name = helm_release.traefik.name

    dynamic "tls" {
      for_each = var.route_tls ? [1] : []
      content {
        hosts = each.value.namespace != var.namespace ? [] : (
          local.primary_group[each.value.namespace] == each.value.group
          ? distinct(local.namespace_hosts[each.value.namespace])
          : distinct([for route in values(var.routes) : route.tls_host if route.namespace == each.value.namespace && route.group == each.value.group])
        )
        secret_name = each.value.namespace == var.namespace ? local.secret_name : null
      }
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

# A second Traefik, from the image the platform already pins, standing in for
# the customer's reverse proxy or tunnel: it terminates HTTPS for the base
# domain and forwards plain HTTP to the platform's Traefik, setting
# X-Forwarded-Proto/Host as any edge does. Test fixture for `tls: external`
# only; a real deployment brings its own edge and this module is not applied.
resource "helm_release" "edge" {
  count = var.test_edge == null ? 0 : 1

  name       = local.edge_name
  repository = local.repository
  chart      = var.chart_package_path != null ? var.chart_package_path : "traefik"
  version    = var.chart_package_path != null ? null : var.chart_version
  namespace  = var.namespace

  wait      = true
  timeout   = 600
  skip_crds = true # the platform's Traefik release owns the CRDs

  values = [
    file(var.base_values_file),
    yamlencode({
      # Otherwise the chart names the Service edge-traefik, and the CoreDNS rewrite would point at nothing.
      fullnameOverride = local.edge_name
      ingressClass     = { enabled = false }
      providers = {
        kubernetesIngress = { enabled = false }
        kubernetesCRD     = { enabled = false }
        file              = { enabled = true, content = local.edge_config }
      }
      ports = {
        web       = { hostPort = null, expose = { default = false } }
        websecure = { hostPort = 443 }
      }
      volumes = [{ name = local.cert_secret, mountPath = "/etc/traefik/edge-tls", type = "secret" }]
      extraObjects = [{
        apiVersion = "cert-manager.io/v1"
        kind       = "Certificate"
        metadata   = { name = local.cert_secret, namespace = var.namespace }
        spec = {
          secretName = local.cert_secret
          dnsNames   = [var.test_edge.base_domain, "*.${var.test_edge.base_domain}"]
          issuerRef  = { kind = "ClusterIssuer", name = var.test_edge.cluster_issuer_name }
        }
      }]
    }),
  ]
}
