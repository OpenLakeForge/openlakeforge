output "contract" {
  description = "S3-compatible storage contract implemented by local SeaweedFS."
  value = {
    endpoint                = "http://${local.s3_service_name}.${var.namespace}:${var.s3_port}"
    virtual_host_endpoint   = "http://${var.namespace}.svc.cluster.local:${var.s3_port}"
    region                  = var.region
    bucket_name             = var.bucket_names[0]
    bucket_names            = var.bucket_names
    path_style_access       = true
    credentials_secret_name = var.credentials_secret_name
    access_key_id_key       = "AWS_ACCESS_KEY_ID"
    secret_access_key_key   = "AWS_SECRET_ACCESS_KEY"
    s3_service_name         = local.s3_service_name
    s3_service_namespace    = var.namespace
    s3_service_port         = var.s3_port
    filer_service_name      = local.filer_service_name
    filer_http_port         = 8888
    filer_endpoint          = "http://${local.filer_service_name}.${var.namespace}:8888"
    master_service_name     = local.master_service_name
    master_http_port        = 9333
    master_endpoint         = "http://${local.master_service_name}.${var.namespace}:9333"
    # The gateway serves STS on its S3 port.
    sts_role_arn = local.sts_role_arn
    sts_endpoint = "http://${local.s3_service_name}.${var.namespace}:${var.s3_port}"
  }

  depends_on = [
    kubernetes_job_v1.bucket,
  ]
}

output "stage_credentials" {
  description = "Stage-scoped S3 credential bindings for provider-contract construction."
  value = {
    for stage, binding in var.stage_credentials : stage => {
      credentials_secret_name = binding.credentials_secret_name
      access_key_id_key       = "AWS_ACCESS_KEY_ID"
      secret_access_key_key   = "AWS_SECRET_ACCESS_KEY"
    }
  }
}

# Read through this output, a consumer is created after the Job that mints the
# key, so its pod never starts before the Secret exists.
output "log_reader_secret_name" {
  description = "Secret holding the platform API's read-only key for the log prefixes, or null when log_reader_credentials is."
  value       = var.log_reader_credentials == null ? null : var.log_reader_credentials.credentials_secret_name
  depends_on  = [kubernetes_job_v1.credentials]
}

output "quality_reader_secret_names" {
  description = "Each stage's read-only quality key Secret, keyed by stage; empty when quality_reader_credentials is."
  value       = { for stage, reader in var.quality_reader_credentials : stage => reader.credentials_secret_name }
  depends_on  = [kubernetes_job_v1.credentials]
}

output "quality_sync_secret_names" {
  description = "Each governed stage's quality reconciler key Secret, keyed by stage; empty when quality_sync_credentials is."
  value       = { for stage, writer in var.quality_sync_credentials : stage => writer.credentials_secret_name }
  depends_on  = [kubernetes_job_v1.credentials]
}

output "audit_writer_secret_name" {
  description = "Secret holding the platform API's write-only key for its audit prefix, or null when audit_writer_credentials is."
  value       = var.audit_writer_credentials == null ? null : var.audit_writer_credentials.credentials_secret_name
  depends_on  = [kubernetes_job_v1.credentials]
}

output "log_collector_secret_name" {
  description = "Secret holding the log collector's write-only key for the v2 archive prefixes, or null when log_collector_credentials is."
  value       = var.log_collector_credentials == null ? null : var.log_collector_credentials.credentials_secret_name
  depends_on  = [kubernetes_job_v1.credentials]
}

output "files_secret_name" {
  description = "Secret holding the platform API's files explorer key, or null when files_credentials is."
  value       = var.files_credentials == null ? null : var.files_credentials.credentials_secret_name
  depends_on  = [kubernetes_job_v1.credentials]
}

output "alerts_secret_name" {
  description = "Secret holding the alert evaluator's key for its state prefix and the quality manifests, or null when alerts_credentials is."
  value       = var.alerts_credentials == null ? null : var.alerts_credentials.credentials_secret_name
  depends_on  = [kubernetes_job_v1.credentials]
}
