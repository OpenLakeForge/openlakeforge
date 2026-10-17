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
  trust_ca = var.github.host != null && var.ca_configmap_name != null

  # `groups` carries `<organisation>:<team-slug>` (teamNameField: slug).
  role_mapping = {
    for role, slugs in var.github.teams : role => [for slug in slugs : "${var.github.organization}:${slug}"]
  }

  # Every secret reaches Dex through a secretKeyRef, none is in the ConfigMap.
  # Verified against the pinned image: Dex expands `$NAME` in the connector and
  # storage config, but NOT in `staticClients[].secret` (the literal string
  # becomes the secret). A static client names its env var with `secretEnv`.
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
        # rootCA is required by Dex whenever hostName is set.
        hostName = var.github.host
        rootCA   = var.github.host == null ? null : "/etc/dex-ca/ca.crt"
      }
    }]
    # Dex matches redirect URIs exactly. The consumers' wildcard entries match
    # nothing until each is wired to OIDC (#25) with its callback path;
    # Superset's already is.
    staticClients = [
      for name, client in var.clients : {
        id           = name
        name         = name
        secretEnv    = local.client_secret_env[name]
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

          dynamic "volume_mount" {
            for_each = local.trust_ca ? [1] : []
            content {
              name       = "ca"
              mount_path = "/etc/dex-ca"
              read_only  = true
            }
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

        dynamic "volume" {
          for_each = local.trust_ca ? [1] : []
          content {
            name = "ca"
            # Not optional: the kubelet retries the mount until the ConfigMap is published.
            config_map {
              name = var.ca_configmap_name
              items {
                key  = "ca.crt"
                path = "ca.crt"
              }
            }
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

  # With a private CA the pod waits for the olf-local-ca ConfigMap, which the root publishes only after the
  # routes that depend on this Deployment: waiting here would deadlock a fresh apply. The kubelet retries the mount.
  wait_for_rollout = !local.trust_ca

  timeouts {
    create = "5m"
    update = "5m"
  }

  # Dex reads the client Secrets by name, so Terraform sees no edge to the Job
  # that creates them.
  depends_on = [kubernetes_job_v1.credentials]
}
