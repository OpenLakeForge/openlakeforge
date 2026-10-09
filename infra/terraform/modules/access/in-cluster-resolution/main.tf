terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  labels = {
    "app.kubernetes.io/name"       = "in-cluster-resolution"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "access"
  }
  ca_configmap_name = "olf-local-ca"
  ingress_fqdn      = "${var.ingress_service_name}.${var.namespace}.svc.cluster.local."
  escaped_domain    = replace(var.base_domain, ".", "\\.")
  trust_namespaces  = distinct(concat([var.namespace], var.trust_namespaces))
  publish_revision  = substr(sha256(join(",", concat(local.trust_namespaces, var.trust_namespace_uids, [var.ca_secret_name, local.ca_configmap_name]))), 0, 10)
}

# `*.localhost` resolves to a pod's own loopback, so a pod that calls
# https://auth.<base_domain> (token and JWKS back-channels) would never reach
# Traefik. The rewrite sends <base_domain> and every name under it to the
# Traefik Service, so the URL, its certificate and the token `iss` are the
# ones a browser sees. Everything else keeps the stock kubeadm Corefile.
#
# Owns the whole Corefile key and removes it on destroy; platform teardown
# (deployment/local/teardown.py) restores the stock Corefile afterwards.
resource "kubernetes_config_map_v1_data" "corefile" {
  metadata {
    name      = "coredns"
    namespace = "kube-system"
  }

  data = {
    Corefile = <<-COREFILE
      .:53 {
          errors
          health {
             lameduck 5s
          }
          ready
          rewrite stop {
             name regex ^(.+\.)?${local.escaped_domain}\.$ ${local.ingress_fqdn}
             answer auto
          }
          kubernetes cluster.local in-addr.arpa ip6.arpa {
             pods insecure
             fallthrough in-addr.arpa ip6.arpa
             ttl 30
          }
          prometheus :9153
          forward . /etc/resolv.conf {
             max_concurrent 1000
          }
          cache 30 {
             disable success cluster.local
             disable denial cluster.local
          }
          loop
          reload
          loadbalance
      }
    COREFILE
  }

  field_manager = "openlakeforge"
  force         = true
}

resource "kubernetes_service_account_v1" "publish_ca" {
  metadata {
    name      = "publish-local-ca"
    namespace = var.namespace
    labels    = local.labels
  }
}

resource "kubernetes_role_v1" "publish_ca" {
  for_each = toset(local.trust_namespaces)

  metadata {
    name      = "publish-local-ca"
    namespace = each.value
    labels    = local.labels
  }

  rule {
    api_groups = [""]
    resources  = ["configmaps"]
    verbs      = ["create", "get", "patch", "update"]
  }

  dynamic "rule" {
    for_each = each.value == var.namespace ? [1] : []
    content {
      api_groups     = [""]
      resources      = ["secrets"]
      resource_names = [var.ca_secret_name]
      verbs          = ["get"]
    }
  }
}

resource "kubernetes_role_binding_v1" "publish_ca" {
  for_each = kubernetes_role_v1.publish_ca

  metadata {
    name      = "publish-local-ca"
    namespace = each.key
    labels    = local.labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = each.value.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.publish_ca.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "publish_ca" {
  metadata {
    name      = "publish-local-ca-${local.publish_revision}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "publish-local-ca"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.publish_ca.metadata[0].name

        container {
          name    = "publish"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [file("${path.module}/templates/publish-ca.sh.tftpl")]

          env {
            name = "NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
            }
          }
          env {
            name  = "CA_SECRET"
            value = var.ca_secret_name
          }
          env {
            name  = "CA_CONFIGMAP"
            value = local.ca_configmap_name
          }
          env {
            name  = "TRUST_NAMESPACES"
            value = join(" ", local.trust_namespaces)
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
  }

  depends_on = [kubernetes_role_binding_v1.publish_ca]
}
