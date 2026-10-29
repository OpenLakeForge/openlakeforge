# The SECRET_KEY and the admin password are created in-cluster, only if
# missing, so Terraform never holds a value (#400,
# docs/architecture/identity/identity-credentials.md). Redeploy reuses them;
# rotating the password is deleting the Secret and replacing this Job. Rotating
# the key is not supported: Superset encrypts stored database passwords with it.
locals {
  credentials_secret_name = "${var.release_name}-credentials"
  # Earlier releases put the key in the chart's own Secret. Reading it back
  # keeps what Superset has already encrypted readable.
  legacy_env_secret_name = "${var.release_name}-env"

  credentials_script = <<-SH
    rand() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
    if kubectl get secret "${local.credentials_secret_name}" -n "$NAMESPACE" >/dev/null 2>&1; then
      echo "Secret ${local.credentials_secret_name} exists; keeping it"
    else
      secret_key="$(kubectl get secret "${local.legacy_env_secret_name}" -n "$NAMESPACE" --ignore-not-found \
        -o 'go-template={{with index .data "SUPERSET_SECRET_KEY"}}{{.}}{{end}}' | base64 -d)"
      [ -n "$secret_key" ] || secret_key="$(rand 64)"
      kubectl create secret generic "${local.credentials_secret_name}" -n "$NAMESPACE" \
        --from-literal=secret-key="$secret_key" --from-literal=admin-password="$(rand 32)"
    fi
  SH
}

# Earlier releases generated the key in Terraform.
removed {
  from = random_password.secret_key
  lifecycle {
    destroy = false
  }
}

resource "kubernetes_service_account_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials"
    namespace = var.namespace
    labels    = local.oidc_labels
  }
}

# `create` cannot be limited by resourceNames (ADR 0017 exception E1); reading
# is limited to the two Secrets this Job looks at.
resource "kubernetes_role_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials"
    namespace = var.namespace
    labels    = local.oidc_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.credentials_secret_name, local.legacy_env_secret_name]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "credentials" {
  metadata {
    name      = kubernetes_role_v1.credentials.metadata[0].name
    namespace = var.namespace
    labels    = local.oidc_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.credentials.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials-${substr(sha256("${local.credentials_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.oidc_labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.oidc_labels, {
          "openlakeforge.io/job"       = "superset-credentials"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.credentials.metadata[0].name

        container {
          name    = "credentials"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.credentials_script]

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

  depends_on = [kubernetes_role_binding_v1.credentials]
}
