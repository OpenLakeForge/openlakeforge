output "contract" {
  description = "OpenMetadata governance contract consumed by the Dagster module."
  value = {
    service_name              = var.release_name
    service_namespace         = var.namespace
    http_port                 = var.om_http_port
    ingestion_bot_secret_name = var.ingestion_bot_secret_name
    ingestion_bot_jwt_key     = var.ingestion_bot_jwt_key
  }
}
