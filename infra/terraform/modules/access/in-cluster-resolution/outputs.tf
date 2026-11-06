output "ca_configmap_name" {
  description = "ConfigMap, present in every trust namespace, whose `ca.crt` is the local CA root; null when publish_ca is false."
  value       = var.publish_ca ? local.ca_configmap_name : null

  depends_on = [kubernetes_job_v1.publish_ca]
}
