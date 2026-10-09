terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  labels = {
    "app.kubernetes.io/name"       = "keycloak"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "identity"
  }
  pod_labels = {
    "app.kubernetes.io/name"     = "keycloak"
    "app.kubernetes.io/instance" = var.release_name
  }

  database = var.postgresql_contract.databases[var.database_key]
  issuer   = "https://${var.hostname}/realms/${var.realm_name}"

  # The env var the Job reads each client secret from. keycloak-config-cli
  # substitutes `$(env:NAME)` in the realm file, so no secret value ever
  # appears in the ConfigMap, the Job spec, or a log line.
  client_secret_env = { for name in keys(var.clients) : name => "CLIENT_SECRET_${upper(name)}" }

  # Realm as code (ADR 0014): groups named exactly as the canonical roles and
  # the OIDC clients; never user records, so adding a user needs no apply. The
  # group mapper is what emits the role claim the contract declares.
  realm = {
    realm                 = var.realm_name
    displayName           = "OpenLakeForge"
    enabled               = true
    sslRequired           = "external"
    registrationAllowed   = false
    resetPasswordAllowed  = true
    loginWithEmailAllowed = true
    bruteForceProtected   = true
    groups                = [for role in var.roles : { name = role }]
    clients = [
      for name, client in var.clients : {
        clientId                  = name
        name                      = name
        enabled                   = true
        protocol                  = "openid-connect"
        publicClient              = false
        secret                    = "$(env:${local.client_secret_env[name]})"
        standardFlowEnabled       = true
        implicitFlowEnabled       = false
        directAccessGrantsEnabled = false
        serviceAccountsEnabled    = false
        redirectUris              = client.redirect_uris
        webOrigins                = ["+"]
        attributes                = { "post.logout.redirect.uris" = "+" }
        protocolMappers = [{
          name           = var.role_claim
          protocol       = "openid-connect"
          protocolMapper = "oidc-group-membership-mapper"
          config = {
            "claim.name"           = var.role_claim
            "full.path"            = "false"
            "id.token.claim"       = "true"
            "access.token.claim"   = "true"
            "userinfo.token.claim" = "true"
          }
        }]
      }
    ]
  }
  realm_json = jsonencode(local.realm)

  # A new Job runs when the realm file changes; replacing the credentials Job
  # (a rotation) re-runs it through replace_triggered_by. Hashing Secret values
  # here would make Terraform read them.
  apply_revision = substr(sha256(local.realm_json), 0, 10)
}

resource "kubernetes_service_v1" "keycloak" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    selector = local.pod_labels

    port {
      name        = "http"
      port        = 8080
      target_port = "http"
    }

    type = "ClusterIP"
  }
}

resource "kubernetes_deployment_v1" "keycloak" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    replicas = 1

    selector {
      match_labels = local.pod_labels
    }

    template {
      metadata {
        labels = local.pod_labels
      }

      spec {
        container {
          name  = "keycloak"
          image = var.image
          args = [
            "start",
            "--http-enabled=true",
            # Traefik terminates TLS; `--hostname` fixes the issuer to the
            # browser-facing URL whatever address a caller used.
            "--hostname=https://${var.hostname}",
            "--proxy-headers=xforwarded",
            "--health-enabled=true",
            "--db=postgres",
            "--db-url=jdbc:postgresql://${var.postgresql_contract.host}:${var.postgresql_contract.port}/${local.database.db_name}",
          ]

          port {
            name           = "http"
            container_port = 8080
          }
          port {
            name           = "management"
            container_port = 9000
          }

          env {
            name  = "KC_DB_USERNAME"
            value = local.database.db_user
          }
          env {
            name = "KC_DB_PASSWORD"
            value_from {
              secret_key_ref {
                name = local.database.credentials_secret_name
                key  = "postgresql-password"
              }
            }
          }
          env {
            name = "KC_BOOTSTRAP_ADMIN_USERNAME"
            value_from {
              secret_key_ref {
                name = local.admin_secret_name
                key  = "username"
              }
            }
          }
          env {
            name = "KC_BOOTSTRAP_ADMIN_PASSWORD"
            value_from {
              secret_key_ref {
                name = local.admin_secret_name
                key  = "password"
              }
            }
          }
          env {
            name  = "JAVA_OPTS_KC_HEAP"
            value = "-XX:MaxRAMPercentage=60 -XX:InitialRAMPercentage=40"
          }

          resources {
            requests = {
              cpu    = "200m"
              memory = "768Mi"
            }
            limits = {
              memory = "1Gi"
            }
          }

          # `start` runs Keycloak's build step on every boot, which is slow on
          # a laptop; the startup probe allows five minutes before restarts.
          startup_probe {
            http_get {
              path = "/health/started"
              port = "management"
            }
            period_seconds    = 5
            failure_threshold = 60
          }
          readiness_probe {
            http_get {
              path = "/health/ready"
              port = "management"
            }
            period_seconds = 10
          }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "10m"
    update = "10m"
  }

  # Keycloak and the realm Job read the Secrets by name, so Terraform sees no
  # edge to the Job that creates them.
  depends_on = [kubernetes_job_v1.credentials]
}

resource "kubernetes_config_map_v1" "realm" {
  metadata {
    name      = "${var.release_name}-realm"
    namespace = var.namespace
    labels    = local.labels
  }
  data = {
    "realm.json" = local.realm_json
  }
}

# keycloak-config-cli rather than the Terraform Keycloak provider: that
# provider must reach Keycloak at plan time, which would be configured from a
# resource created in the same apply (ADR 0014).
resource "kubernetes_job_v1" "realm" {
  metadata {
    name      = "${var.release_name}-realm-${local.apply_revision}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 6

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "keycloak-realm"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy = "Never"

        container {
          name  = "config-cli"
          image = var.config_cli_image

          env {
            name  = "KEYCLOAK_URL"
            value = "http://${kubernetes_service_v1.keycloak.metadata[0].name}.${var.namespace}.svc.cluster.local:8080"
          }
          env {
            name = "KEYCLOAK_USER"
            value_from {
              secret_key_ref {
                name = local.admin_secret_name
                key  = "username"
              }
            }
          }
          env {
            name = "KEYCLOAK_PASSWORD"
            value_from {
              secret_key_ref {
                name = local.admin_secret_name
                key  = "password"
              }
            }
          }
          env {
            name  = "KEYCLOAK_AVAILABILITYCHECK_ENABLED"
            value = "true"
          }
          env {
            name  = "KEYCLOAK_AVAILABILITYCHECK_TIMEOUT"
            value = "180s"
          }
          env {
            name  = "IMPORT_FILES_LOCATIONS"
            value = "/config/realm.json"
          }
          env {
            name  = "IMPORT_VARSUBSTITUTION_ENABLED"
            value = "true"
          }
          dynamic "env" {
            for_each = local.client_secret_env
            content {
              name = env.value
              value_from {
                secret_key_ref {
                  name = local.client_secret_name[env.key]
                  key  = "client-secret"
                }
              }
            }
          }

          volume_mount {
            name       = "realm"
            mount_path = "/config"
            read_only  = true
          }

          resources {
            requests = {
              cpu    = "50m"
              memory = "256Mi"
            }
            limits = {
              memory = "512Mi"
            }
          }
        }

        volume {
          name = "realm"
          config_map {
            name = kubernetes_config_map_v1.realm.metadata[0].name
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "10m"
    update = "10m"
  }

  depends_on = [kubernetes_deployment_v1.keycloak]

  lifecycle {
    replace_triggered_by = [kubernetes_job_v1.credentials]
  }
}
