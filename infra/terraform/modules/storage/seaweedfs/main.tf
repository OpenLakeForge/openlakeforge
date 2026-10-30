locals {
  labels = {
    "app.kubernetes.io/name"       = "seaweedfs"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "storage"
  }

  chart      = var.chart_package_path != null ? var.chart_package_path : "seaweedfs"
  repository = var.chart_package_path != null ? null : var.chart_repository
  version    = var.chart_package_path != null ? null : var.chart_version

  base_values = yamldecode(file(var.base_values_file))
  # One row per size, shared with `olf` (its expansion check reads the same file).
  object_store = yamldecode(file(var.storage_sizes_file)).sizes[var.storage_size].object_store

  s3_service_name     = "${var.release_name}-s3"
  master_service_name = "${var.release_name}-master"
  filer_service_name  = "${var.release_name}-filer-client"
  bucket_job_annotations = {
    "openlakeforge.io/seaweedfs-release-revision" = tostring(helm_release.seaweedfs.metadata.revision)
  }
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

      master = { data = { size = local.object_store.master } }
      filer  = { data = { size = local.object_store.filer } }
      # Helm replaces a list rather than merging it, so each entry is the base file's with the sized fields added.
      volume = {
        dataDirs = [
          for dir in local.base_values.volume.dataDirs : merge(dir, {
            size       = local.object_store.volume
            maxVolumes = local.object_store.max_volumes
          })
        ]
      }

      s3 = {
        port                 = var.s3_port
        domainName           = "${var.namespace}.svc.cluster.local"
        createBuckets        = []
        enableAuth           = true
        existingConfigSecret = local.auth_secret_name
      }
    }),
  ]

  depends_on = [kubernetes_job_v1.credentials]
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
