# ADR 0007: the reconciler exists only for a governed stage, runs bounded and
# unprivileged, holds no credential as a value, and each stage's pass can name
# only that stage's prefix, database and key.

mock_provider "kubernetes" {}

variables {
  namespace        = "olf-system"
  image            = "openlakeforge/platform-api:local-abc"
  openmetadata_url = "http://openmetadata.olf-system:8585"
  s3_endpoint_url  = "http://seaweedfs-s3.olf-system:8333"
  s3_region        = "us-east-1"
  stages = {
    dev = {
      artifact_base_uri       = "s3://ops/activations/dev/"
      database_fqn            = "polaris.lakehouse_dev"
      credentials_secret_name = "quality-reconciler-dev-s3-creds"
    }
    prod = {
      artifact_base_uri       = "s3://ops/activations/prod/"
      database_fqn            = "polaris.lakehouse_prod"
      credentials_secret_name = "quality-reconciler-prod-s3-creds"
    }
  }
}

run "no_governed_stage_means_nothing_is_created" {
  command = plan

  variables {
    stages = {}
    image  = null
  }

  assert {
    condition     = length(kubernetes_cron_job_v1.reconciler) == 0 && length(kubernetes_service_account_v1.reconciler) == 0
    error_message = "Slim has no reconciler: no CronJob and no ServiceAccount"
  }

  assert {
    condition     = output.service_account_name == null && length(output.cron_job_names) == 0
    error_message = "the outputs name nothing when nothing exists"
  }
}

run "each_governed_stage_gets_its_own_cronjob" {
  command = plan

  assert {
    condition     = output.cron_job_names == { dev = "quality-reconciler-dev", prod = "quality-reconciler-prod" }
    error_message = "one CronJob per governed stage, named by the stage"
  }

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      anytrue([for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : e.name == "OPENLAKEFORGE_STAGE" && e.value == stage])
      && anytrue([for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : e.name == "OPENLAKEFORGE_ARTIFACT_BASE_URI" && e.value == "s3://ops/activations/${stage}/"])
      && anytrue([for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : e.name == "OPENLAKEFORGE_CATALOG_DATABASE_FQN" && e.value == "polaris.lakehouse_${stage}"])
    ])
    error_message = "a stage's pass is pinned to that stage's prefix and database root"
  }

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      job.spec[0].job_template[0].spec[0].template[0].metadata[0].labels["openlakeforge.io/job"] == "quality-reconciler"
      && job.spec[0].job_template[0].spec[0].template[0].metadata[0].labels["openlakeforge.io/stage"] == stage
    ])
    error_message = "the pod carries the label the flow table selects on, and its stage"
  }
}

run "a_stage_reads_only_its_own_key_and_its_own_writer_token_by_reference" {
  command = plan

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      anytrue([
        for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env :
        e.name == "AWS_SECRET_ACCESS_KEY" && e.value == null
        && e.value_from[0].secret_key_ref[0].name == "quality-reconciler-${stage}-s3-creds"
      ])
    ])
    error_message = "each stage's pass mounts its own key's Secret, never another stage's"
  }

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      anytrue([
        for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env :
        e.name == "OPENMETADATA_TOKEN" && e.value == null
        && e.value_from[0].secret_key_ref[0].name == "openmetadata-quality-writer-${stage}"
        && e.value_from[0].secret_key_ref[0].key == "OPENMETADATA_QUALITY_WRITER_JWT"
      ])
    ])
    error_message = "each stage's pass publishes with its own quality writer's token, through secretKeyRef"
  }

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      !anytrue([
        for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env :
        try(e.value_from[0].secret_key_ref[0].name, "") == "openmetadata-ingestion-bot"
      ])
    ])
    error_message = "the reconciler no longer mounts the ingestion bot's token (ADR 0007 D13)"
  }

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      alltrue([for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : e.value == null || !strcontains(lower(e.name), "token") && !strcontains(lower(e.name), "secret")])
      && job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].args == null
    ])
    error_message = "no credential is a literal env value, and none is in the arguments"
  }
}

run "the_job_is_bounded_and_unprivileged" {
  command = plan

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      job.spec[0].schedule == "*/5 * * * *"
      && job.spec[0].concurrency_policy == "Forbid"
      && job.spec[0].job_template[0].spec[0].active_deadline_seconds == 240
      && job.spec[0].job_template[0].spec[0].backoff_limit == 0
      && anytrue([for e in job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env : e.name == "OPENLAKEFORGE_QUALITY_SYNC_LIMIT" && e.value == "25"])
    ])
    error_message = "one pass at a time, a bounded batch, and a deadline under the schedule period"
  }

  assert {
    condition = alltrue([
      for stage, job in kubernetes_cron_job_v1.reconciler :
      job.spec[0].job_template[0].spec[0].template[0].spec[0].automount_service_account_token == false
      && job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].run_as_non_root == true
      && job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].read_only_root_filesystem == true
      && job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].security_context[0].allow_privilege_escalation == false
      && job.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].resources[0].limits["memory"] == "256Mi"
    ])
    error_message = "no Kubernetes API token, a non-root read-only root filesystem and a memory limit"
  }

  assert {
    condition     = !kubernetes_service_account_v1.reconciler[0].automount_service_account_token
    error_message = "the ServiceAccount does not mount its token either"
  }
}

run "a_stage_that_names_another_stage_is_refused" {
  command = plan

  variables {
    stages = {
      dev = {
        artifact_base_uri       = "s3://ops/activations/prod/"
        database_fqn            = "polaris.lakehouse_dev"
        credentials_secret_name = "x"
      }
    }
  }

  expect_failures = [var.stages]
}

run "a_database_root_of_another_stage_is_refused" {
  command = plan

  variables {
    stages = {
      dev = {
        artifact_base_uri       = "s3://ops/activations/dev/"
        database_fqn            = "polaris.lakehouse_prod"
        credentials_secret_name = "x"
      }
    }
  }

  expect_failures = [var.stages]
}

run "schema_maps_are_passed_when_the_provider_prefixes_schemas" {
  command = plan

  variables {
    stages = {
      dev = {
        artifact_base_uri       = "s3://ops/activations/dev/"
        database_fqn            = "aws_glue.lakehouse_dev"
        credentials_secret_name = "quality-reconciler-dev-s3-creds"
        silver_schema_fqns      = { sales = "aws_glue.lakehouse_dev.lakehouse_dev_sales_silver" }
      }
    }
  }

  assert {
    condition = anytrue([
      for e in kubernetes_cron_job_v1.reconciler["dev"].spec[0].job_template[0].spec[0].template[0].spec[0].container[0].env :
      e.name == "OPENLAKEFORGE_CATALOG_SILVER_SCHEMA_FQNS_JSON" && strcontains(e.value, "lakehouse_dev_sales_silver")
    ])
    error_message = "a prefixed physical schema is reachable only through the contract's mapping"
  }
}

run "a_governed_stage_without_an_image_is_refused" {
  command = plan

  variables {
    image = null
  }

  expect_failures = [kubernetes_cron_job_v1.reconciler]
}

run "a_warn_can_never_publish_as_success" {
  command = plan

  variables {
    warn_status = "Success"
  }

  expect_failures = [var.warn_status]
}

run "the_batch_is_bounded" {
  command = plan

  variables {
    limit = 500
  }

  expect_failures = [var.limit]
}
