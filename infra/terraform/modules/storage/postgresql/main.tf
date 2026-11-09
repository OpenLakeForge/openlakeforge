locals {
  labels = {
    "app.kubernetes.io/name"       = "postgresql"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "storage"
  }

  pod_labels = {
    "app.kubernetes.io/name"     = "postgresql"
    "app.kubernetes.io/instance" = var.release_name
  }

  host = "${var.release_name}.${var.namespace}.svc.cluster.local"
  port = 5432

  databases_by_key = { for database in var.databases : database.key => database }

  # One env-var prefix per database, so the init script can address each
  # without the module knowing which services exist.
  database_env_prefixes = { for database in var.databases : database.key => "OLF_DB_${upper(replace(database.key, "-", "_"))}" }

  database_env_values = merge(concat([{}], [
    for database in var.databases : {
      "${local.database_env_prefixes[database.key]}_USER" = database.db_user
      "${local.database_env_prefixes[database.key]}_NAME" = database.db_name
    }
  ])...)

  database_env_passwords = { for database in var.databases : "${local.database_env_prefixes[database.key]}_PASSWORD" => database.key }

  # The credentials Secret always exists in this module's own namespace - the
  # StatefulSet and bootstrap Job mount it - plus wherever the consuming
  # workload runs, since a Secret cannot be read across namespaces.
  database_secrets = merge(concat([{}], [
    for database in var.databases : {
      for target in toset(concat([var.namespace], database.namespaces)) :
      "${database.key}/${target}" => {
        key       = database.key
        namespace = target
        name      = database.credentials_secret_name
      }
    }
  ])...)

  bootstrap_script = templatefile("${path.module}/templates/init.sh.tftpl", {
    database_env_prefixes = [for database in var.databases : local.database_env_prefixes[database.key]]
    async_commit_prefixes = [for database in var.databases : local.database_env_prefixes[database.key] if database.async_commit]
  })

  bootstrap_hash = substr(sha256(jsonencode({
    script          = local.bootstrap_script
    databases       = var.databases
    polaris_db_name = var.polaris_db_name
    polaris_db_user = var.polaris_db_user
  })), 0, 12)
}
