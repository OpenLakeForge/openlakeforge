locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "traefik"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
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
