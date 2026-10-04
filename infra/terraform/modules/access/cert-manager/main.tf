locals {
  chart      = var.chart_package_path != null ? var.chart_package_path : "cert-manager"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version
  # Fixed: renaming it would orphan the CA Secret users already trust.
  issuer_name = "local-ca"
}

resource "helm_release" "cert_manager" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  wait    = true
  timeout = 600

  values = [file(var.base_values_file)]
}

# A release of its own: the issuers are cert-manager CRD kinds, which Helm
# cannot map until the release above has installed the CRDs.
resource "helm_release" "local_ca" {
  name      = "local-ca"
  chart     = "${path.module}/local-ca"
  namespace = var.namespace

  values = [yamlencode({
    issuerName = local.issuer_name
    baseDomain = var.base_domain
  })]

  depends_on = [helm_release.cert_manager]
}
