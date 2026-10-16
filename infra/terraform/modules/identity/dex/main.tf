terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

locals {
  labels = {
    "app.kubernetes.io/name"       = "dex"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "identity"
  }
  pod_labels = {
    "app.kubernetes.io/name"     = "dex"
    "app.kubernetes.io/instance" = var.release_name
  }

  database = var.postgresql_contract.databases[var.database_key]
  issuer   = "https://${var.hostname}"

  # `groups` carries `<organisation>:<team-slug>` (teamNameField: slug).
  role_mapping = {
    for role, slugs in var.github.teams : role => [for slug in slugs : "${var.github.organization}:${slug}"]
  }

  # Dex expands `$NAME` in its config file from the environment, so every
  # secret reaches it through a secretKeyRef and none is in the ConfigMap.
  client_secret_env = { for name in keys(var.clients) : name => "DEX_CLIENT_SECRET_${upper(name)}" }

  # JSON is YAML; Dex parses it as such. The session policy is pinned here
  # (5 min ID tokens bound revocation; a refresh re-reads team membership), not
  # left to Dex's 24 h defaults. reuseInterval stays because the floor-specific
  # oauth2-proxy instances can redeem one cookie's refresh token concurrently.
  config = {
    issuer = local.issuer
    storage = {
      type = "postgres"
      config = {
        host     = var.postgresql_contract.host
        port     = var.postgresql_contract.port
        database = local.database.db_name
        user     = local.database.db_user
        password = "$DEX_DB_PASSWORD"
        ssl      = { mode = "disable" }
      }
    }
    web       = { http = "0.0.0.0:5556" }
    telemetry = { http = "0.0.0.0:5558" }
    logger    = { level = "info", format = "json" }
    oauth2    = { skipApprovalScreen = true }
    expiry = {
      idTokens = "5m"
      refreshTokens = {
        validIfNotUsedFor = "30m"
        absoluteLifetime  = "10h"
        reuseInterval     = "3s"
      }
    }
    enablePasswordDB = false
    connectors = [{
      type = "github"
      id   = "github"
      name = "GitHub"
      config = {
        clientID     = "$GITHUB_CLIENT_ID"
        clientSecret = "$GITHUB_CLIENT_SECRET"
        redirectURI  = "${local.issuer}/callback"
        # A user in none of the listed teams is refused at login.
        orgs          = [{ name = var.github.organization, teams = sort(distinct(flatten(values(var.github.teams)))) }]
        teamNameField = "slug"
        loadAllGroups = false
        useLoginAsID  = false
      }
    }]
    # Dex matches redirect URIs exactly. The consumers' wildcard entries match
    # nothing until each is wired to OIDC (#25) with its callback path.
    staticClients = [
      for name, client in var.clients : {
        id           = name
        name         = name
        secret       = format("$%s", local.client_secret_env[name])
        redirectURIs = client.redirect_uris
      }
    ]
  }
  config_yaml = jsonencode(local.config)
}

resource "kubernetes_config_map_v1" "dex" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }
  data = {
    "config.yaml" = local.config_yaml
  }
}

resource "kubernetes_service_v1" "dex" {
  metadata {
    name      = var.release_name
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    selector = local.pod_labels

    port {
      name        = "http"
      port        = 5556
      target_port = "http"
    }

    type = "ClusterIP"
  }
}

resource "kubernetes_deployment_v1" "dex" {
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
        # Dex reads its config once at start: a changed ConfigMap restarts it.
        annotations = {
          "openlakeforge.io/config-sha" = substr(sha256(local.config_yaml), 0, 12)
        }
      }

      spec {
        automount_service_account_token = false

        container {
          name    = "dex"
          image   = var.image
          command = ["/usr/local/bin/dex"]
          args    = ["serve", "/etc/dex/config.yaml"]

          port {
            name           = "http"
            container_port = 5556
          }
          port {
            name           = "telemetry"
            container_port = 5558
          }

          env {
            name = "DEX_DB_PASSWORD"
            value_from {
              secret_key_ref {
                name = local.database.credentials_secret_name
                key  = "postgresql-password"
              }
            }
          }
          env {
            name = "GITHUB_CLIENT_ID"
            value_from {
              secret_key_ref {
                name = var.github.oauth_app_secret_ref.name
                key  = var.github.oauth_app_secret_ref.client_id_key
              }
            }
          }
          env {
            name = "GITHUB_CLIENT_SECRET"
            value_from {
              secret_key_ref {
                name = var.github.oauth_app_secret_ref.name
                key  = var.github.oauth_app_secret_ref.client_secret_key
              }
            }
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
            name       = "config"
            mount_path = "/etc/dex"
            read_only  = true
          }

          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
            }
          }

          resources {
            requests = {
              cpu    = "50m"
              memory = "64Mi"
            }
            limits = {
              memory = "256Mi"
            }
          }

          readiness_probe {
            http_get {
              path = "/healthz/ready"
              port = "telemetry"
            }
            period_seconds = 10
          }
          liveness_probe {
            http_get {
              path = "/healthz/live"
              port = "telemetry"
            }
            period_seconds = 20
          }
        }

        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.dex.metadata[0].name
          }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }

  # Dex reads the client Secrets by name, so Terraform sees no edge to the Job
  # that creates them.
  depends_on = [kubernetes_job_v1.credentials]
}
