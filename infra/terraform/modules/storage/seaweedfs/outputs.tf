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
