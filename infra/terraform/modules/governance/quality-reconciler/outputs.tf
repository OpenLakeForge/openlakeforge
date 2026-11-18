output "cron_job_names" {
  description = "Each governed stage's reconciler CronJob name, keyed by stage; empty when no stage has governance."
  value       = { for stage, job in kubernetes_cron_job_v1.reconciler : stage => job.metadata[0].name }
}

output "service_account_name" {
  description = "ServiceAccount of the reconcilers, or null without a governed stage; it holds no Role and its token is not mounted."
  value       = one(kubernetes_service_account_v1.reconciler[*].metadata[0].name)
}
