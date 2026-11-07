resource "random_string" "bucket_suffix" {
  length  = 8
  lower   = true
  numeric = true
  special = false
  upper   = false
}

locals {
  ops_bucket_name = coalesce(var.ops_bucket_name, "${var.bucket_name_prefix}-ops-${random_string.bucket_suffix.result}")

  # Per-stage bucket names carry the shared random suffix because S3 names are
  # global. The one exception is DEV during the v0.2 migration: its legacy
  # resource addresses are moved below and its physical names must stay exact
  # so Terraform preserves existing objects rather than replacing buckets.
  stage_bucket_names = {
    for stage, binding in var.stage_buckets : stage => {
      bronze = binding.preserve_legacy_names ? coalesce(
        binding.bronze_bucket_name, "${var.bucket_name_prefix}-bronze-${random_string.bucket_suffix.result}"
      ) : "${binding.bronze_bucket_name}-${random_string.bucket_suffix.result}"
      silver = binding.preserve_legacy_names ? coalesce(
        binding.silver_bucket_name, "${var.bucket_name_prefix}-silver-${random_string.bucket_suffix.result}"
      ) : "${binding.silver_bucket_name}-${random_string.bucket_suffix.result}"
      gold = binding.preserve_legacy_names ? coalesce(
        binding.gold_bucket_name, "${var.bucket_name_prefix}-gold-${random_string.bucket_suffix.result}"
      ) : "${binding.gold_bucket_name}-${random_string.bucket_suffix.result}"
    }
  }

  all_buckets = merge(
    { for stage, names in local.stage_bucket_names : "${stage}-bronze" => names.bronze },
    { for stage, names in local.stage_bucket_names : "${stage}-silver" => names.silver },
    { for stage, names in local.stage_bucket_names : "${stage}-gold" => names.gold },
    { ops = local.ops_bucket_name },
  )

  bucket_names = values(local.all_buckets)
}

resource "aws_s3_bucket" "this" {
  for_each = local.all_buckets

  bucket        = each.value
  force_destroy = var.force_destroy

  tags = {
    Project     = "openlakeforge"
    Environment = "aws-poc"
    Layer       = each.key
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = aws_s3_bucket.this

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# v0.2 used unqualified medallion keys. The v3 DEV binding uses stage-qualified
# addresses but the same physical buckets, so moving every dependent resource
# is necessary to make the platform migration non-destructive.
moved {
  from = aws_s3_bucket.this["bronze"]
  to   = aws_s3_bucket.this["dev-bronze"]
}

moved {
  from = aws_s3_bucket.this["silver"]
  to   = aws_s3_bucket.this["dev-silver"]
}

moved {
  from = aws_s3_bucket.this["gold"]
  to   = aws_s3_bucket.this["dev-gold"]
}

moved {
  from = aws_s3_bucket_public_access_block.this["bronze"]
  to   = aws_s3_bucket_public_access_block.this["dev-bronze"]
}

moved {
  from = aws_s3_bucket_public_access_block.this["silver"]
  to   = aws_s3_bucket_public_access_block.this["dev-silver"]
}

moved {
  from = aws_s3_bucket_public_access_block.this["gold"]
  to   = aws_s3_bucket_public_access_block.this["dev-gold"]
}

moved {
  from = aws_s3_bucket_versioning.this["bronze"]
  to   = aws_s3_bucket_versioning.this["dev-bronze"]
}

moved {
  from = aws_s3_bucket_versioning.this["silver"]
  to   = aws_s3_bucket_versioning.this["dev-silver"]
}

moved {
  from = aws_s3_bucket_versioning.this["gold"]
  to   = aws_s3_bucket_versioning.this["dev-gold"]
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.this["bronze"]
  to   = aws_s3_bucket_server_side_encryption_configuration.this["dev-bronze"]
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.this["silver"]
  to   = aws_s3_bucket_server_side_encryption_configuration.this["dev-silver"]
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.this["gold"]
  to   = aws_s3_bucket_server_side_encryption_configuration.this["dev-gold"]
}
