# The S3 keys are created in-cluster, only if missing, so Terraform never
# generates one (#400, docs/architecture/identity/identity-credentials.md).
# Redeploy reuses them; rotation is deleting a Secret and replacing this Job.
#
# The same Job builds the gateway's auth config from those Secrets, so Terraform
# passes only identity data: names, Secret references and bucket grants. SeaweedFS
# reads the config at startup, so keeping every identity in one config, rather
# than minting credentials only in Kubernetes, makes a stage Secret incapable of
# authenticating as a different stage.
locals {
  auth_secret_name = "${var.release_name}-s3-auth"

  admin_namespaces = distinct(concat([var.namespace], var.workload_namespaces))

  # Secrets the Job mints beside the admin pair, one per namespace that holds it.
  scoped_credentials = concat(
    [for stage in values(var.stage_credentials) : { secret = stage.credentials_secret_name, namespace = stage.namespace }],
    var.log_reader_credentials == null ? [] : [{
      secret    = var.log_reader_credentials.credentials_secret_name
      namespace = var.log_reader_credentials.namespace
    }],
    var.audit_writer_credentials == null ? [] : [{
      secret    = var.audit_writer_credentials.credentials_secret_name
      namespace = var.audit_writer_credentials.namespace
    }],
    [for reader in values(var.quality_reader_credentials) : { secret = reader.credentials_secret_name, namespace = reader.namespace }],
    [for writer in values(var.quality_sync_credentials) : { secret = writer.credentials_secret_name, namespace = writer.namespace }],
    var.files_credentials == null ? [] : [{
      secret    = var.files_credentials.credentials_secret_name
      namespace = var.files_credentials.namespace
    }],
    var.log_collector_credentials == null ? [] : [{
      secret    = var.log_collector_credentials.credentials_secret_name
      namespace = var.log_collector_credentials.namespace
    }],
    var.alerts_credentials == null ? [] : [{
      secret    = var.alerts_credentials.credentials_secret_name
      namespace = var.alerts_credentials.namespace
    }],
    var.alert_state_reader_credentials == null ? [] : [{
      secret    = var.alert_state_reader_credentials.credentials_secret_name
      namespace = var.alert_state_reader_credentials.namespace
    }],
  )

  # Every namespace that receives a Secret, with the names it may read.
  credential_secret_names = {
    for namespace in distinct(concat(local.admin_namespaces, [for scoped in local.scoped_credentials : scoped.namespace])) : namespace => concat(
      contains(local.admin_namespaces, namespace) ? [var.credentials_secret_name] : [],
      namespace == var.namespace ? [local.auth_secret_name] : [],
      [for scoped in local.scoped_credentials : scoped.secret if scoped.namespace == namespace],
    )
  }

  auth_identities = concat(
    [{
      name      = "openlakeforge-platform"
      secret    = var.credentials_secret_name
      namespace = var.namespace
      actions   = ["Admin"]
    }],
    [for stage, binding in var.stage_credentials : {
      name      = "openlakeforge-${stage}"
      secret    = binding.credentials_secret_name
      namespace = binding.namespace
      # Bronze/Silver/Gold are wholly stage-exclusive buckets, so the
      # bucket-level grant is already scoped. The ops bucket is shared across
      # every stage's activation artifacts, so its grant is further scoped to
      # this stage's own prefix. SeaweedFS 4.48 matches an action with
      # wildcard.MatchesWildcard, where `*` also matches `/`, so the grant
      # ends at `activations/<stage>/*`: without the slash, `activations/dev*`
      # would also reach a stage named `dev2`.
      actions = concat(
        flatten([for bucket in [
          binding.bronze_bucket_name,
          binding.silver_bucket_name,
          binding.gold_bucket_name,
        ] : ["Read:${bucket}", "Write:${bucket}", "List:${bucket}"]]),
        [for action in ["Read", "Write", "List"] : "${action}:${binding.ops_bucket_name}/activations/${stage}/*"],
      )
    }],
    # Read and List only, on each archive prefix: the platform API reads pod
    # logs and nothing else in the ops bucket. A prefix ends in `/` (variable
    # validation), so `*` stays under it.
    var.log_reader_credentials == null ? [] : [{
      name      = "openlakeforge-platform-api-logs"
      secret    = var.log_reader_credentials.credentials_secret_name
      namespace = var.log_reader_credentials.namespace
      actions = flatten([
        for prefix in var.log_reader_credentials.prefixes : [
          for action in ["Read", "List"] : "${action}:${var.log_reader_credentials.ops_bucket_name}/${prefix}*"
        ]
      ])
    }],
    # Read and List only, one identity per stage, on that stage's quality
    # manifests: the key cannot read another stage's, nor anything else of its
    # own. The prefix ends in `/` so `*` stays under it.
    [for stage, reader in var.quality_reader_credentials : {
      name      = "openlakeforge-platform-api-quality-${stage}"
      secret    = reader.credentials_secret_name
      namespace = reader.namespace
      actions   = [for action in ["Read", "List"] : "${action}:${reader.ops_bucket_name}/activations/${stage}/run-artifacts/quality/*"]
    }],
    # The quality reconciler (ADR 0007), one identity per governed stage: Read and List on that stage's quality
    # manifests, which it replays and never writes, and Read, Write and List on its own `quality-sync/` state and
    # summary. The key cannot reach another stage's manifests or state, nor the stage's other evidence. Prefixes end
    # in `/` so `*` stays under them. SeaweedFS gates overwrite and delete on Write, so the state is tamper-evident
    # against everyone but this key, not immutable.
    [for stage, writer in var.quality_sync_credentials : {
      name      = "openlakeforge-quality-sync-${stage}"
      secret    = writer.credentials_secret_name
      namespace = writer.namespace
      actions = concat(
        [for action in ["Read", "List"] : "${action}:${writer.ops_bucket_name}/activations/${stage}/run-artifacts/quality/*"],
        [for action in ["Read", "Write", "List"] : "${action}:${writer.ops_bucket_name}/activations/${stage}/quality-sync/*"],
      )
    }],
    # Read and List only, on the alert evaluator's state prefix: the platform
    # API answers open incidents from it and can neither change the state nor
    # read the history, so the evaluator stays the only writer (ADR 0023,
    # decision 3). The prefix ends in `/` so `*` stays under it.
    var.alert_state_reader_credentials == null ? [] : [{
      name      = "openlakeforge-platform-api-alerts"
      secret    = var.alert_state_reader_credentials.credentials_secret_name
      namespace = var.alert_state_reader_credentials.namespace
      actions   = [for action in ["Read", "List"] : "${action}:${var.alert_state_reader_credentials.ops_bucket_name}/${var.alert_state_reader_credentials.prefix}*"]
    }],
    # Write only, under one prefix: the platform API stores audit records and
    # can neither read nor list them back. SeaweedFS 4.48 also gates overwrite
    # and delete on Write (weed/s3api/s3api_server.go), so this is not
    # append-only; ADR 0021 decision 3 says what that leaves.
    var.audit_writer_credentials == null ? [] : [{
      name      = "openlakeforge-platform-api-audit"
      secret    = var.audit_writer_credentials.credentials_secret_name
      namespace = var.audit_writer_credentials.namespace
      actions   = ["Write:${var.audit_writer_credentials.ops_bucket_name}/${var.audit_writer_credentials.prefix}*"]
    }],
    # Write only, on each v2 archive prefix: the log collector (ADR 0022) puts
    # objects and can neither read nor list them. As with the audit key,
    # SeaweedFS 4.48 gates overwrite and delete on Write, so the archive is not
    # append-only.
    var.log_collector_credentials == null ? [] : [{
      name      = "openlakeforge-log-collector"
      secret    = var.log_collector_credentials.credentials_secret_name
      namespace = var.log_collector_credentials.namespace
      actions   = [for prefix in var.log_collector_credentials.prefixes : "Write:${var.log_collector_credentials.ops_bucket_name}/${prefix}*"]
    }],
    # Read, Write and List on whole buckets: the platform API's files explorer
    # (ADR 0021) browses and edits the layer buckets and the ops bucket. SeaweedFS
    # gates delete on Write, so the API itself keeps the audit prefix read-only;
    # this grant cannot.
    var.files_credentials == null ? [] : [{
      name      = "openlakeforge-platform-api-files"
      secret    = var.files_credentials.credentials_secret_name
      namespace = var.files_credentials.namespace
      actions = flatten([
        for bucket in var.files_credentials.bucket_names : [for action in ["Read", "Write", "List"] : "${action}:${bucket}"]
      ])
    }],
    # The alert evaluator (ADR 0023, decision 3): its own state and history
    # under one prefix, and the stages' quality manifests read-only. SeaweedFS
    # gates overwrite and delete on Write, so the history is tamper-evident
    # against everyone but this key, not immutable.
    var.alerts_credentials == null ? [] : [{
      name      = "openlakeforge-alerts"
      secret    = var.alerts_credentials.credentials_secret_name
      namespace = var.alerts_credentials.namespace
      actions = concat(
        [for action in ["Read", "Write", "List"] : "${action}:${var.alerts_credentials.ops_bucket_name}/${var.alerts_credentials.prefix}*"],
        flatten([
          for prefix in var.alerts_credentials.quality_prefixes : [
            for action in ["Read", "List"] : "${action}:${var.alerts_credentials.ops_bucket_name}/${prefix}*"
          ]
        ]),
        # The alert policy (ADR 0023, decision 10): each stage's pointer and activation records, and the frozen
        # revisions. Read only, no List: the evaluator follows keys it derives. `project/revisions/*` holds every
        # file of every revision, wider than the two descriptors it needs; the maintainer accepted that. Whether
        # SeaweedFS honours a `*` in the middle of an action path, which would narrow it to
        # `project/revisions/sha256/*/descriptors/*`, is still untested (ADR 0023, History).
        flatten([
          for stage in var.alerts_credentials.policy_stages : [
            # Trailing `*`: SeaweedFS denies an action naming an exact object (verified on kind, 2026-11-17).
            "Read:${var.alerts_credentials.ops_bucket_name}/activations/${stage}/ACTIVE.json*",
            "Read:${var.alerts_credentials.ops_bucket_name}/activations/${stage}/revisions/*",
          ]
        ]),
        length(var.alerts_credentials.policy_stages) == 0 ? [] : ["Read:${var.alerts_credentials.ops_bucket_name}/project/revisions/*"],
      )
    }],
  )

  # Polaris vends table-scoped credentials by assuming this role through the
  # gateway's STS, with a session policy naming the table (ADR 0019). Every
  # identity lives in SeaweedFS's one default account, so the trust policy names
  # the platform identity Polaris signs its storage calls with.
  sts_role_arn = "arn:aws:iam::000000000000:role/polaris-vending"
  vending_buckets = length(var.stage_credentials) > 0 ? distinct(flatten([
    for stage in values(var.stage_credentials) : [stage.bronze_bucket_name, stage.silver_bucket_name, stage.gold_bucket_name]
  ])) : var.bucket_names

  # The gateway's advanced IAM file, less the STS signing key the Job adds. A
  # request signed with a session token is allowed only where the role's policy
  # and the session policy both allow it; a request with a static key keeps the
  # identity's actions above.
  iam_config = {
    sts = {
      tokenDuration    = "15m"
      maxSessionLength = "1h"
      issuer           = "seaweedfs-sts"
    }
    policy = {
      defaultEffect = "Deny"
      storeType     = "memory"
    }
    policies = [{
      name = "polaris-vending"
      document = {
        Version = "2012-10-17"
        Statement = [{
          Effect   = "Allow"
          Action   = ["s3:*"]
          Resource = flatten([for bucket in local.vending_buckets : ["arn:aws:s3:::${bucket}", "arn:aws:s3:::${bucket}/*"]])
        }]
      }
    }]
    roles = [{
      roleName         = "polaris-vending"
      roleArn          = local.sts_role_arn
      attachedPolicies = ["polaris-vending"]
      trustPolicy = {
        Version = "2012-10-17"
        Statement = [{
          Effect    = "Allow"
          Principal = { AWS = "arn:aws:iam::000000000000:user/${local.auth_identities[0].name}" }
          Action    = ["sts:AssumeRole"]
        }]
      }
    }]
  }

  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    admin_secret     = var.credentials_secret_name
    admin_namespaces = join(" ", local.admin_namespaces)
    namespace        = var.namespace
    auth_secret      = local.auth_secret_name
    s3_deployment    = local.s3_service_name
    identities       = jsonencode(local.auth_identities)
    scoped_pairs     = join(" ", [for scoped in local.scoped_credentials : "${scoped.secret}=${scoped.namespace}"])
    iam_config       = jsonencode(local.iam_config)
  })
}

# Earlier releases generated the keys in Terraform. The Secrets stay in the
# cluster and the Job keeps them, so the upgrade rotates nothing. Drop the
# blocks once every environment has applied past this change.
removed {
  from = random_id.s3_access_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.s3_secret_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_id.stage_s3_access_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = random_password.stage_s3_secret_key
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.s3_credentials
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.stage_s3_credentials
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.s3_auth_config
  lifecycle {
    destroy = false
  }
}

resource "kubernetes_service_account_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials"
    namespace = var.namespace
    labels    = local.labels
  }
}

# `create` cannot be limited by resourceNames, so it covers every Secret in the
# namespace (ADR 0017 exception E1); reading is limited to the Secrets this Job
# owns there.
resource "kubernetes_role_v1" "credentials" {
  for_each = local.credential_secret_names

  metadata {
    name      = "${var.release_name}-credentials"
    namespace = each.key
    labels    = local.labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["create"]
  }
  rule {
    api_groups     = [""]
    resources      = ["secrets"]
    resource_names = each.value
    verbs          = ["get"]
  }
  # The auth config and the gateway's config-hash annotation live in the
  # module's own namespace.
  dynamic "rule" {
    for_each = each.key == var.namespace ? [1] : []
    content {
      api_groups     = [""]
      resources      = ["secrets"]
      resource_names = [local.auth_secret_name]
      verbs          = ["patch"]
    }
  }
  dynamic "rule" {
    for_each = each.key == var.namespace ? [1] : []
    content {
      api_groups     = ["apps"]
      resources      = ["deployments"]
      resource_names = [local.s3_service_name]
      verbs          = ["get", "patch"]
    }
  }
}

resource "kubernetes_role_binding_v1" "credentials" {
  for_each = kubernetes_role_v1.credentials

  metadata {
    name      = each.value.metadata[0].name
    namespace = each.key
    labels    = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = each.value.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.credentials.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "credentials" {
  metadata {
    name      = "${var.release_name}-credentials-${substr(sha256("${local.credentials_script}${var.bootstrap_job_image}"), 0, 10)}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "seaweedfs-credentials"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.credentials.metadata[0].name

        container {
          name    = "credentials"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.credentials_script]

          resources {
            requests = {
              cpu    = "10m"
              memory = "32Mi"
            }
            limits = {
              memory = "128Mi"
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
    update = "5m"
  }

  depends_on = [kubernetes_role_binding_v1.credentials]
}
