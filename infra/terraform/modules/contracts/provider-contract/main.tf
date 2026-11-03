check "foundation_contract_matches_platform_context" {
  assert {
    condition     = var.kube_context == var.foundation_kube_context
    error_message = "The ${var.environment_label} platform kube context must come from the foundation contract."
  }
}

check "catalog_contract_consumer_support" {
  assert {
    condition = alltrue([
      contains(var.catalog.trino_support, var.catalog.catalog_type),
      contains(var.catalog.dagster_support, var.catalog.catalog_type),
      contains(var.catalog.floe_support, var.catalog.catalog_type),
      contains(var.catalog.dbt_support, var.catalog.catalog_type),
      contains(var.catalog.openmetadata_support, var.catalog.catalog_type),
    ])
    error_message = "The active catalog_type must be declared as supported by Trino, Dagster, Floe, dbt, and OpenMetadata."
  }
}

check "stage_namespaces_are_distinct" {
  assert {
    condition = alltrue([
      length(distinct(values(var.stage_namespaces))) == length(var.stage_namespaces),
      !contains(values(var.stage_namespaces), var.shared_namespace),
    ])
    error_message = "Every enabled stage must own a namespace of its own, distinct from the shared platform namespace."
  }
}

check "stage_services_stay_in_their_own_stage" {
  assert {
    condition     = alltrue([for instance in var.stage_service_namespaces : instance.namespace == var.stage_namespaces[instance.stage]])
    error_message = "Each stage-scoped service instance must be deployed in its own stage namespace."
  }
}

check "stage_metadata_state_is_not_shared" {
  assert {
    condition     = length(distinct([for database in values(var.stage_databases) : database.db_name])) == length(var.stage_databases)
    error_message = "Every stage-scoped service instance must own its metadata database; sharing one mixes stage state."
  }
}
