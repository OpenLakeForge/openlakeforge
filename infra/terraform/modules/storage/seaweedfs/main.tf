locals {
  labels = {
    "app.kubernetes.io/name"       = "seaweedfs"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "storage"
  }

  chart      = var.chart_package_path != null ? var.chart_package_path : "seaweedfs"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version

  s3_service_name     = "${var.release_name}-s3"
  master_service_name = "${var.release_name}-master"
  filer_service_name  = "${var.release_name}-filer-client"
  access_key_id       = "olf${random_id.s3_access_key.hex}"
  bucket_job_annotations = {
    "openlakeforge.io/seaweedfs-release-revision" = tostring(helm_release.seaweedfs.metadata.revision)
  }
}

resource "random_id" "s3_access_key" {
  byte_length = 8
}

resource "random_password" "s3_secret_key" {
  length  = 40
  special = false
}

resource "random_id" "stage_s3_access_key" {
  for_each = var.stage_credentials

  byte_length = 8
}

resource "random_password" "stage_s3_secret_key" {
  for_each = var.stage_credentials

  length  = 40
  special = false
}

# SeaweedFS reads this immutable configuration at S3 gateway startup. Keeping
# the identities here, rather than minting credentials only in Kubernetes,
# makes a stage Secret incapable of authenticating as a different stage.
resource "kubernetes_secret_v1" "s3_auth_config" {
  metadata {
    name      = "${var.release_name}-s3-auth"
    namespace = var.namespace
    labels    = local.labels
  }

  data = {
    seaweedfs_s3_config = jsonencode({
      identities = concat(
        [{
          name        = "openlakeforge-platform"
          credentials = [{ accessKey = local.access_key_id, secretKey = random_password.s3_secret_key.result }]
          actions     = ["Admin"]
        }],
        [for stage, binding in var.stage_credentials : {
          name = "openlakeforge-${stage}"
          credentials = [{
            accessKey = "olf${random_id.stage_s3_access_key[stage].hex}"
            secretKey = random_password.stage_s3_secret_key[stage].result
          }]
          # Bronze/Silver/Gold are wholly stage-exclusive buckets, so the
          # bucket-level grant is already scoped. The ops bucket is shared
          # across every stage's activation artifacts, so its grant is
          # further scoped to this stage's own prefix - SeaweedFS matches the
          # action's bucket/key component with filepath.Match, so a trailing
          # `*` confines it to `activations/<stage>/...` and denies every
          # other stage's prefix in the same bucket.
          actions = concat(
            flatten([for bucket in [
              binding.bronze_bucket_name,
              binding.silver_bucket_name,
              binding.gold_bucket_name,
            ] : ["Read:${bucket}", "Write:${bucket}", "List:${bucket}"]]),
            [for action in ["Read", "Write", "List"] : "${action}:${binding.ops_bucket_name}/activations/${stage}*"],
          )
        }],
      )
    })
  }

  type = "Opaque"
}

# One Secret per namespace that runs a workload reading object storage: a
# Secret cannot be read across namespaces, and stage-scoped services no
# longer share this module's own namespace.
resource "kubernetes_secret_v1" "s3_credentials" {
  for_each = toset(concat([var.namespace], var.workload_namespaces))

  metadata {
    name      = var.credentials_secret_name
    namespace = each.value
    labels    = local.labels
  }

  data = {
    AWS_ACCESS_KEY_ID     = local.access_key_id
    AWS_SECRET_ACCESS_KEY = random_password.s3_secret_key.result
  }

  type = "Opaque"
}

resource "kubernetes_secret_v1" "stage_s3_credentials" {
  for_each = var.stage_credentials

  metadata {
    name      = each.value.credentials_secret_name
    namespace = each.value.namespace
    labels    = local.labels
  }

  data = {
    AWS_ACCESS_KEY_ID     = "olf${random_id.stage_s3_access_key[each.key].hex}"
    AWS_SECRET_ACCESS_KEY = random_password.stage_s3_secret_key[each.key].result
  }

  type = "Opaque"
}

resource "helm_release" "seaweedfs" {
  name       = var.release_name
  repository = local.repository
  chart      = local.chart
  version    = local.version
  namespace  = var.namespace

  wait    = true
  timeout = 900

  values = [
    file(var.base_values_file),
    yamlencode({
      image = {
        tag = var.image_tag
      }

      s3 = {
        port                 = var.s3_port
        domainName           = "${var.namespace}.svc.cluster.local"
        createBuckets        = []
        enableAuth           = true
        existingConfigSecret = kubernetes_secret_v1.s3_auth_config.metadata[0].name
      }
    }),
  ]
}

resource "kubernetes_service_v1" "bucket_virtual_host" {
  for_each = toset(var.bucket_names)

  metadata {
    name      = each.key
    namespace = var.namespace
    labels = merge(local.labels, {
      "openlakeforge.io/service-role" = "s3-bucket-virtual-host"
    })
  }

  spec {
    type = "ClusterIP"

    selector = {
      "app.kubernetes.io/component" = "s3"
      "app.kubernetes.io/instance"  = var.release_name
      "app.kubernetes.io/name"      = "seaweedfs"
    }

    port {
      name        = "swfs-s3"
      port        = var.s3_port
      target_port = var.s3_port
      protocol    = "TCP"
    }
  }

  depends_on = [
    helm_release.seaweedfs,
  ]
}

resource "kubernetes_job_v1" "bucket" {
  for_each = toset(var.bucket_names)

  metadata {
    name      = "seaweedfs-bucket-${each.key}-${helm_release.seaweedfs.metadata.revision}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "bucket-bootstrap"
          "openlakeforge.io/readiness" = "required"
        })
        annotations = local.bucket_job_annotations
      }

      spec {
        restart_policy = "Never"

        container {
          name  = "create-bucket"
          image = "chrislusf/seaweedfs:${var.image_tag}"

          command = ["/bin/sh", "-ec"]
          args = [<<-SCRIPT
            set -eu

            wait_for_service() {
              url="$1"
              attempt=1
              while [ "$attempt" -le 60 ]; do
                if wget -q --spider "$url" >/dev/null 2>&1; then
                  return 0
                fi
                sleep 5
                attempt=$((attempt + 1))
              done
              echo "Service at $url did not become ready" >&2
              exit 1
            }

            wait_for_service "http://${local.master_service_name}:9333/cluster/status"
            wait_for_service "http://${local.filer_service_name}:8888/"

            export WEED_CLUSTER_DEFAULT=sw
            export WEED_CLUSTER_SW_MASTER="${local.master_service_name}:9333"
            export WEED_CLUSTER_SW_FILER="${local.filer_service_name}:8888"

            bucket_list="$(echo 's3.bucket.list' | /usr/bin/weed shell)"
            if echo "$bucket_list" | awk '{print $1}' | grep -Fxq "${each.key}"; then
              echo "Bucket '${each.key}' already exists."
            else
              echo "Creating bucket '${each.key}'."
              echo 's3.bucket.create --name ${each.key}' | /usr/bin/weed shell
            fi
          SCRIPT
          ]
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
    helm_release.seaweedfs,
  ]
}
