# Trino workload credentials (ADR 0016, #26) are created in-cluster, only if
# missing, so Terraform never holds a value: it only names the Secrets below
# (docs/architecture/identity/identity-credentials.md). Redeploy reuses them; rotation
# is deleting a principal Secret and replacing this Job.
locals {
  credentials_enabled = length(var.stage_namespaces) > 0
  # One principal per stage runtime (see catalog_access_rules) plus OpenMetadata.
  credential_principals = concat([for stage in sort(keys(var.stage_namespaces)) : "olf-${stage}-runtime"], ["openmetadata"])
  credential_labels = {
    "app.kubernetes.io/name"       = "trino"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "query"
  }
  password_file_secret = "trino-password-file"
  # The Web UI client Secret, copied here when the issuer made it elsewhere.
  oidc_source = try(var.web_ui_oidc.secret_namespace, null) == null || try(var.web_ui_oidc.secret_namespace, null) == var.namespace ? "" : var.web_ui_oidc.secret_namespace
  oidc_secret = try(var.web_ui_oidc.secret_ref.name, "")
  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    oidc_source          = local.oidc_source
    oidc_secret          = local.oidc_secret
    principals           = join(" ", local.credential_principals)
    stage_namespaces     = join(" ", [for stage in sort(keys(var.stage_namespaces)) : "${stage}=${var.stage_namespaces[stage]}"])
    password_file_script = file("${path.module}/templates/password-file.py")
  })
}

resource "kubernetes_service_account_v1" "credentials" {
  count = local.credentials_enabled ? 1 : 0

  metadata {
    name      = "trino-credentials"
    namespace = var.namespace
    labels    = local.credential_labels
  }
}

# `create` cannot be limited by resourceNames, so it covers every Secret in the
# namespace (inventoried in docs/architecture/identity/identity-credentials.md); reading
# and replacing are limited to the Secrets this Job owns.
resource "kubernetes_role_v1" "credentials" {
  count = local.credentials_enabled ? 1 : 0

  metadata {
    name      = "trino-credentials"
    namespace = var.namespace
    labels    = local.credential_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    resource_names = concat(
      [for principal in local.credential_principals : "trino-credential-${principal}"],
      ["trino-internal-secret", local.password_file_secret, "trino-credential"],
      compact([local.oidc_source == "" ? "" : local.oidc_secret]),
    )
    verbs = ["get"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = concat([local.password_file_secret, "trino-credential"], compact([local.oidc_source == "" ? "" : local.oidc_secret]))
    verbs          = ["update", "patch"]
  }
}

resource "kubernetes_role_binding_v1" "credentials" {
  count = local.credentials_enabled ? 1 : 0

  metadata {
    name      = "trino-credentials"
    namespace = var.namespace
    labels    = local.credential_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.credentials[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials[0].metadata[0].name
    namespace = var.namespace
  }
}

# Reading the one Web UI client Secret where the issuer created it.
resource "kubernetes_role_v1" "oidc_source" {
  count = local.credentials_enabled && local.oidc_source != "" ? 1 : 0

  metadata {
    name      = "trino-credentials-${var.namespace}"
    namespace = local.oidc_source
    labels    = local.credential_labels
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = [local.oidc_secret]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "oidc_source" {
  count = length(kubernetes_role_v1.oidc_source)

  metadata {
    name      = kubernetes_role_v1.oidc_source[0].metadata[0].name
    namespace = local.oidc_source
    labels    = local.credential_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.oidc_source[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials[0].metadata[0].name
    namespace = var.namespace
  }
}

# A stage namespace takes `trino-credential` and nothing else from this Job.
# The Job's own namespace is covered by the Role above, which a coordinator
# running in its stage namespace needs for the same Secret.
resource "kubernetes_role_v1" "credentials_stage" {
  for_each = local.credentials_enabled ? toset([for namespace in values(var.stage_namespaces) : namespace if namespace != var.namespace]) : toset([])

  metadata {
    name      = "trino-credentials"
    namespace = each.key
    labels    = local.credential_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = ["trino-credential"]
    verbs          = ["get", "update"]
  }
}

resource "kubernetes_role_binding_v1" "credentials_stage" {
  for_each = kubernetes_role_v1.credentials_stage

  metadata {
    name      = "trino-credentials"
    namespace = each.key
    labels    = local.credential_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = each.value.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials[0].metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "credentials" {
  count = local.credentials_enabled ? 1 : 0

  metadata {
    name      = "trino-credentials-${substr(sha256(local.credentials_script), 0, 10)}"
    namespace = var.namespace
    labels    = local.credential_labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.credential_labels, {
          "openlakeforge.io/job"       = "trino-credentials"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.credentials[0].metadata[0].name

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

          resources {
            requests = {
              cpu    = "10m"
              memory = "32Mi"
            }
            limits = {
              memory = "128Mi"
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

  depends_on = [
    kubernetes_role_binding_v1.credentials,
    kubernetes_role_binding_v1.credentials_stage,
    kubernetes_role_binding_v1.oidc_source,
  ]
}
