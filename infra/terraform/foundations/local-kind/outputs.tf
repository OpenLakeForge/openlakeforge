output "cluster_name" {
  description = "Local kind cluster name."
  value       = var.cluster_name

  depends_on = [
    terraform_data.kind_cluster,
  ]
}

output "kube_context" {
  description = "Kubeconfig context for the local kind cluster."
  value       = local.kube_context

  depends_on = [
    terraform_data.kind_cluster,
  ]
}

output "foundation_contract" {
  description = "Provider-neutral local cluster foundation contract."
  value = {
    provider            = "local"
    implementation      = "kind"
    image_repository    = "localhost:${var.registry_port}/openlakeforge/${var.cluster_name}/project-code"
    cluster_name        = var.cluster_name
    kube_context        = local.kube_context
    kubeconfig_path     = local.kubeconfig_path
    cluster_config_path = local.cluster_config_path
  }

  depends_on = [
    terraform_data.kind_cluster,
    terraform_data.registry,
  ]
}
