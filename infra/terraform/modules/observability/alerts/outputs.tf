output "cron_job_name" {
  description = "Name of the evaluator CronJob, or null without receivers."
  value       = one(kubernetes_cron_job_v1.evaluator[*].metadata[0].name)
}

output "service_account_name" {
  description = "ServiceAccount of the evaluator, or null without receivers; it holds no Role and its token is not mounted."
  value       = one(kubernetes_service_account_v1.evaluator[*].metadata[0].name)
}
