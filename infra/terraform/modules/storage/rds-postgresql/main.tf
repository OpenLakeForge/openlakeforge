locals {
  labels = {
    "app.kubernetes.io/name"       = "rds-postgresql"
    "app.kubernetes.io/managed-by" = "terraform"
    "openlakeforge.io/component"   = "metadata-database"
  }

  host = aws_db_instance.this.address
  port = aws_db_instance.this.port

  databases_by_key = { for database in var.databases : database.key => database }

  # One env-var prefix per database, so the bootstrap script can address each
  # without the module knowing which services exist.
  database_env_prefixes = { for database in var.databases : database.key => "OLF_DB_${upper(replace(database.key, "-", "_"))}" }

  # The credentials Secret always exists in this module's own namespace - the
  # bootstrap Job mounts it - plus wherever the consuming workload runs, since
  # a Secret cannot be read across namespaces.
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
  })

  # Per database: the Secret name and every namespace that runs a consumer.
  credentials_script = templatefile("${path.module}/templates/credentials.sh.tftpl", {
    databases = join(" ", [
      for database in var.databases :
      "${database.credentials_secret_name}=${join(",", distinct(concat([var.namespace], database.namespaces)))}"
    ])
  })

  credential_secret_names = {
    for namespace in distinct(concat([var.namespace], [for secret in values(local.database_secrets) : secret.namespace])) :
    namespace => [for secret in values(local.database_secrets) : secret.name if secret.namespace == namespace]
  }

  bootstrap_hash = substr(sha256(jsonencode({
    script      = local.bootstrap_script
    credentials = local.credentials_script
    image       = var.bootstrap_job_image
    databases   = var.databases
  })), 0, 12)
}

# Passwords are not generated in Terraform (#400, docs/architecture/identity/
# identity-credentials.md). RDS creates and rotates the master password in
# Secrets Manager, and the bootstrap Job below creates each database password in
# the cluster only if missing, so state holds neither. The Secrets earlier
# releases wrote stay in the cluster and the Job keeps them: the upgrade rotates
# no database password. Drop these blocks once every environment has applied
# past this change.
removed {
  from = kubernetes_secret_v1.database_credentials
  lifecycle {
    destroy = false
  }
}

resource "aws_security_group" "this" {
  name_prefix = "${var.name_prefix}-rds-"
  description = "OpenLakeForge AWS POC RDS PostgreSQL access"
  vpc_id      = var.vpc_id

  ingress {
    description = "PostgreSQL from EKS VPC CIDRs"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Project     = "openlakeforge"
    Environment = "aws-poc"
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-rds"
  subnet_ids = var.subnet_ids

  tags = {
    Project     = "openlakeforge"
    Environment = "aws-poc"
  }
}

resource "aws_db_instance" "this" {
  identifier             = "${var.name_prefix}-metadata"
  engine                 = "postgres"
  engine_version         = var.engine_version
  instance_class         = var.instance_class
  allocated_storage      = var.allocated_storage
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.this.id]
  username               = var.master_username
  # State keeps the Secrets Manager ARN, never the password.
  manage_master_user_password = true
  db_name                     = "postgres"
  publicly_accessible         = false
  skip_final_snapshot         = true
  deletion_protection         = false
  apply_immediately           = true
  storage_encrypted           = true

  tags = {
    Project     = "openlakeforge"
    Environment = "aws-poc"
  }
}

# The Job's identity: Kubernetes RBAC to create the database Secrets, and an
# IAM role (EKS Pod Identity) to read the RDS-managed master password. Nothing
# else in the cluster is bound to the role.
resource "kubernetes_service_account_v1" "bootstrap" {
  metadata {
    name      = "${var.name_prefix}-rds-bootstrap"
    namespace = var.namespace
    labels    = local.labels
  }
}

resource "aws_iam_role" "bootstrap" {
  name = "${var.cluster_name}-${var.name_prefix}-rds-bootstrap"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy" "bootstrap" {
  name = "read-master-password"
  role = aws_iam_role.bootstrap.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_db_instance.this.master_user_secret[0].secret_arn
    }]
  })
}

resource "aws_eks_pod_identity_association" "bootstrap" {
  cluster_name    = var.cluster_name
  namespace       = var.namespace
  service_account = kubernetes_service_account_v1.bootstrap.metadata[0].name
  role_arn        = aws_iam_role.bootstrap.arn
}

# `create` cannot be limited by resourceNames, so it covers every Secret in the
# namespace (ADR 0017 exception E1); reading is limited to the Secrets the Job
# owns there.
resource "kubernetes_role_v1" "bootstrap" {
  for_each = local.credential_secret_names

  metadata {
    name      = "${var.name_prefix}-rds-bootstrap"
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
}

resource "kubernetes_role_binding_v1" "bootstrap" {
  for_each = kubernetes_role_v1.bootstrap

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
    name      = kubernetes_service_account_v1.bootstrap.metadata[0].name
    namespace = var.namespace
  }
}

resource "kubernetes_job_v1" "bootstrap" {
  metadata {
    name      = "${var.name_prefix}-rds-bootstrap-${local.bootstrap_hash}"
    namespace = var.namespace
    labels    = local.labels
  }

  spec {
    backoff_limit = 6

    template {
      metadata {
        labels = merge(local.labels, {
          "openlakeforge.io/job"       = "rds-bootstrap"
          "openlakeforge.io/readiness" = "required"
        })
      }

      spec {
        restart_policy       = "Never"
        service_account_name = kubernetes_service_account_v1.bootstrap.metadata[0].name

        volume {
          name = "master"
          empty_dir {
            medium = "Memory"
          }
        }

        # Creates the database Secrets if missing and hands the master
        # password to the psql container through memory-backed storage.
        init_container {
          name    = "credentials"
          image   = var.bootstrap_job_image
          command = ["/bin/sh", "-ec"]
          args    = [local.credentials_script]

          env {
            name = "NAMESPACE"
            value_from {
              field_ref {
                field_path = "metadata.namespace"
              }
            }
          }

          env {
            name  = "AWS_REGION"
            value = var.region
          }

          env {
            name  = "MASTER_SECRET_ARN"
            value = aws_db_instance.this.master_user_secret[0].secret_arn
          }

          volume_mount {
            name       = "master"
            mount_path = "/master"
          }
        }

        container {
          name  = "bootstrap"
          image = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"

          command = ["/bin/sh", "-ec"]
          args    = [local.bootstrap_script]

          env {
            name  = "PGSSLMODE"
            value = "require"
          }

          env {
            name  = "PGHOST"
            value = local.host
          }

          env {
            name  = "PGPORT"
            value = tostring(local.port)
          }

          env {
            name  = "POSTGRES_USER"
            value = var.master_username
          }

          volume_mount {
            name       = "master"
            mount_path = "/master"
            read_only  = true
          }

          dynamic "env" {
            for_each = local.databases_by_key
            content {
              name  = "${local.database_env_prefixes[env.key]}_USER"
              value = env.value.db_user
            }
          }

          dynamic "env" {
            for_each = local.databases_by_key
            content {
              name  = "${local.database_env_prefixes[env.key]}_NAME"
              value = env.value.db_name
            }
          }

          dynamic "env" {
            for_each = local.databases_by_key
            content {
              name = "${local.database_env_prefixes[env.key]}_PASSWORD"
              value_from {
                secret_key_ref {
                  name = env.value.credentials_secret_name
                  key  = "postgresql-password"
                }
              }
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "15m"
    update = "15m"
  }

  depends_on = [
    aws_db_instance.this,
    aws_eks_pod_identity_association.bootstrap,
    aws_iam_role_policy.bootstrap,
    kubernetes_role_binding_v1.bootstrap,
  ]
}
