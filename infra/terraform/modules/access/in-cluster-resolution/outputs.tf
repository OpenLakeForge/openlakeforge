output "ca_configmap_name" {
  description = "ConfigMap, present in every trust namespace, whose `ca.crt` is the local CA root."
  value       = local.ca_configmap_name

  depends_on = [kubernetes_job_v1.publish_ca]
}
