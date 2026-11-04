terraform {
  required_version = ">= 1.10.0"
}

locals {
  repo_root                   = abspath("${path.root}/../../../..")
  default_cluster_config_path = "${local.repo_root}/infra/kind/local/kind-cluster.yaml"
  cluster_config_path         = var.cluster_config_path != null ? abspath(pathexpand(var.cluster_config_path)) : local.default_cluster_config_path
  kubeconfig_path             = var.kubeconfig_path != null ? abspath(pathexpand(var.kubeconfig_path)) : "${local.repo_root}/.tmp/kubeconfigs/local.yaml"
  kube_context                = "kind-${var.cluster_name}"
  registry_name               = "${var.cluster_name}-registry"
  registry_volume             = "${var.cluster_name}-registry-data"
  registry_image              = "registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e"
}

resource "terraform_data" "kind_cluster" {
  input = {
    cluster_name          = var.cluster_name
    cluster_config_path   = local.cluster_config_path
    cluster_config_sha256 = filesha256(local.cluster_config_path)
    kube_context          = local.kube_context
    kubeconfig_path       = local.kubeconfig_path
    repo_root             = local.repo_root
    kind_executable_path  = var.kind_executable_path
  }

  triggers_replace = [
    var.cluster_name,
    filesha256(local.cluster_config_path),
  ]

  provisioner "local-exec" {
    command = <<-EOT
      set -euo pipefail

      kind="$${KIND_BIN:-kind}"
      kubectl="$${KUBECTL_BIN:-kubectl}"

      if ! "$kubectl" version --client >/dev/null 2>&1; then
        echo "ERROR: kubectl ($kubectl) is not executable. Install a native Linux kubectl binary and retry." >&2
        exit 1
      fi

      if ! docker info >/dev/null 2>&1; then
        echo "ERROR: Docker daemon is not running or not accessible." >&2
        exit 1
      fi

      if [[ ! -f "$CLUSTER_CONFIG" ]]; then
        echo "ERROR: kind cluster config not found: $CLUSTER_CONFIG" >&2
        exit 1
      fi

      mkdir -p "$(dirname "$KUBECONFIG_PATH")"

      if "$kind" get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
        if [[ "$RESET_EXISTING_CLUSTER" == "true" ]]; then
          echo "==> Deleting existing kind cluster '$CLUSTER_NAME'..."
          "$kind" delete cluster --name "$CLUSTER_NAME"
        else
          echo "Kind cluster '$CLUSTER_NAME' already exists. Reusing it."
          "$kind" export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG_PATH"
          "$kubectl" --kubeconfig "$KUBECONFIG_PATH" cluster-info --context "kind-$CLUSTER_NAME"
          exit 0
        fi
      fi

      echo "==> Creating kind cluster '$CLUSTER_NAME'..."
      "$kind" create cluster \
        --name "$CLUSTER_NAME" \
        --config "$CLUSTER_CONFIG" \
        --kubeconfig "$KUBECONFIG_PATH" \
        --wait "$KIND_WAIT_TIMEOUT"

      "$kubectl" --kubeconfig "$KUBECONFIG_PATH" get nodes --context "kind-$CLUSTER_NAME"
    EOT

    interpreter = ["/usr/bin/env", "bash", "-c"]

    environment = {
      CLUSTER_CONFIG         = local.cluster_config_path
      CLUSTER_NAME           = var.cluster_name
      KIND_WAIT_TIMEOUT      = var.kind_wait_timeout
      KUBECONFIG_PATH        = local.kubeconfig_path
      RESET_EXISTING_CLUSTER = tostring(var.reset_existing_cluster)
      KIND_BIN               = var.kind_executable_path
      KUBECTL_BIN            = var.kubectl_executable_path
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      set -euo pipefail

      kind="$${KIND_BIN:-kind}"

      # The stored KIND_BIN is a managed-toolchain path resolved at apply
      # time (Terraform destroy-time provisioners can only read `self`, so
      # a freshly-resolved path can't be threaded in here). If OLF_HOME
      # changed or `olf toolchain clean` removed that version since apply,
      # the path may no longer exist. Falling through to "cluster does not
      # exist" in that case would be wrong: it would drop this resource
      # from state while the real kind cluster keeps running, unmanaged
      # and undetected. Try a bare `kind` on PATH as a last resort, then
      # fail loudly rather than silently declaring victory.
      if ! "$kind" version >/dev/null 2>&1; then
        if command -v kind >/dev/null 2>&1; then
          echo "WARNING: managed kind at '$kind' is not executable (stale OLF_HOME or pruned toolchain); falling back to 'kind' on PATH." >&2
          kind="kind"
        else
          echo "ERROR: kind ('$kind') is not executable and no 'kind' was found on PATH. Refusing to report cluster '$CLUSTER_NAME' as absent - that would silently orphan it. Set KIND_BIN to a working kind binary and retry." >&2
          exit 1
        fi
      fi

      if "$kind" get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
        echo "==> Deleting kind cluster '$CLUSTER_NAME'..."
        "$kind" delete cluster --name "$CLUSTER_NAME"
      else
        echo "Kind cluster '$CLUSTER_NAME' does not exist."
      fi
    EOT

    interpreter = ["/usr/bin/env", "bash", "-c"]

    environment = {
      CLUSTER_NAME = self.input.cluster_name
      # State created before kind_executable_path existed on `input` has no
      # such attribute; try() falls back to "" (the bare `kind` on PATH
      # path below) instead of blocking teardown of that older state.
      KIND_BIN = try(self.input.kind_executable_path, "")
    }
  }
}

resource "terraform_data" "registry_volume" {
  input            = { name = local.registry_volume }
  triggers_replace = [local.registry_volume]

  provisioner "local-exec" {
    command     = "docker volume inspect \"$REGISTRY_VOLUME\" >/dev/null 2>&1 || docker volume create \"$REGISTRY_VOLUME\""
    environment = { REGISTRY_VOLUME = self.input.name }
  }

  provisioner "local-exec" {
    when        = destroy
    command     = "docker volume rm \"$REGISTRY_VOLUME\""
    environment = { REGISTRY_VOLUME = self.input.name }
  }
}

resource "terraform_data" "registry" {
  input = {
    name   = local.registry_name
    image  = local.registry_image
    port   = var.registry_port
    volume = terraform_data.registry_volume.output.name
  }
  triggers_replace = [local.registry_name, local.registry_image, var.registry_port]

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      if docker container inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
        docker start "$REGISTRY_NAME"
      else
        docker run -d --restart=always --memory=256m \
          --name "$REGISTRY_NAME" -p "127.0.0.1:$REGISTRY_PORT:5000" \
          -v "$REGISTRY_VOLUME:/var/lib/registry" "$REGISTRY_IMAGE"
      fi
    EOT
    environment = {
      REGISTRY_NAME   = self.input.name
      REGISTRY_PORT   = tostring(self.input.port)
      REGISTRY_VOLUME = self.input.volume
      REGISTRY_IMAGE  = self.input.image
    }
  }

  provisioner "local-exec" {
    when        = destroy
    command     = "docker rm -f \"$REGISTRY_NAME\""
    environment = { REGISTRY_NAME = self.input.name }
  }
}
