resource "random_string" "bucket_suffix" {
  length  = 8
  lower   = true
  numeric = true
  special = false
  upper   = false
}

locals {
  ops_bucket_name = coalesce(var.ops_bucket_name, "${var.bucket_name_prefix}-ops-${random_string.bucket_suffix.result}")

  # Stage buckets take the name the resolver derived, exactly: a prefix that
  # is unique on the account is the operator's job (`metadata.name`), not a
  # random suffix. Only the legacy DEV binding keeps the v0.2 physical names,
  # whose resource addresses are moved below so Terraform preserves objects.
  stage_bucket_names = {
    for stage, binding in var.stage_buckets : stage => {
      bronze = binding.preserve_legacy_names ? coalesce(
        binding.bronze_bucket_name, "${var.bucket_name_prefix}-bronze-${random_string.bucket_suffix.result}"
      ) : binding.bronze_bucket_name
      silver = binding.preserve_legacy_names ? coalesce(
        binding.silver_bucket_name, "${var.bucket_name_prefix}-silver-${random_string.bucket_suffix.result}"
      ) : binding.silver_bucket_name
      gold = binding.preserve_legacy_names ? coalesce(
        binding.gold_bucket_name, "${var.bucket_name_prefix}-gold-${random_string.bucket_suffix.result}"
      ) : binding.gold_bucket_name
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
