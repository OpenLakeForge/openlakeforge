locals {
  rest_uri = "https://glue.${var.region}.amazonaws.com/iceberg"
  # tomap(...): see modules/catalog/polaris/main.tf's stage_catalogs comment -
  # a bare object literal here does not reliably unify with
  # var.stage_catalogs's map(object(...)) type at apply time.
  stage_catalogs = length(var.stage_catalogs) > 0 ? var.stage_catalogs : tomap({
    dev = { catalog_name = var.catalog_name }
  })
}

# No per-stage aws_glue_catalog resource: this account's Glue service
# rejects CreateCatalog for a plain ("native") data-lake-access catalog
# (InvalidInputException: "Create glue native catalog is not supported"),
# confirmed directly against the AWS API, not just this module's config.
# Every stage instead shares the account's one default catalog
# (glue_catalog_id = account_id, see outputs.tf); isolation moves to the
# physical database name, prefixed per stage by
# olf.infra.contracts.resolve_physical_names's namespace_prefix.

# Database lifecycle moved to Phase 2 (ADR 0002): `olf infra catalog sync-namespaces`
# reconciles Glue databases from the lakehouse descriptors
# (lakehouse_code/governance.yml + bronze source.yaml) during artifacts-deploy,
# the same way it already reconciles Polaris namespaces. This root no longer
# creates or destroys databases -- it only stands up the catalog service.
#
# `removed` forgets the resource from state without deleting the underlying
# Glue databases, so an existing deployment's tables survive the upgrade. This
# block can be dropped once every environment has applied past this change.
removed {
  from = aws_glue_catalog_database.namespace

  lifecycle {
    destroy = false
  }
}

# A previous revision of this module created a custom aws_glue_catalog per
# stage before discovering the account rejects it (see above); this hands
# any already-applied instances back to Terraform's ordinary destroy path
# without erroring on an address that no longer exists in configuration.
removed {
  from = aws_glue_catalog.stage

  lifecycle {
    destroy = true
  }
}

removed {
  from = aws_iam_role_policy.glue_data_transfer

  lifecycle {
    destroy = true
  }
}

removed {
  from = aws_iam_role.glue_data_transfer

  lifecycle {
    destroy = true
  }
}
