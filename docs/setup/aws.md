# Deploy to AWS (EKS)

The AWS environment runs the same OpenLakeForge platform on managed AWS
services: **EKS, ECR, S3, RDS PostgreSQL and AWS Glue**. The `poc` name refers
to the sandbox account used for validation, not to a reduced platform. See
[the AWS EKS POC architecture](../architecture/aws-eks-poc.md) for the chart and
for what the POC does not cover yet.

Prerequisites, local Terraform state, and what stays out of git are shared with
Azure: see [Cloud POC setup](cloud-poc-setup.md#prerequisites).

## 1. Authenticate

Use IAM Identity Center without installing AWS CLI:

```bash
olf auth login --provider aws \
  --start-url "https://your-company.awsapps.com/start" \
  --sso-region eu-west-1
```

This opens an AWS-hosted device authorization page. If the browser cannot open,
use `--no-browser` and enter the printed code. To reuse an existing SDK/CLI SSO
profile without copying its cache, run
`olf auth login --provider aws --profile my-profile`. Export `AWS_PROFILE` if
needed.

If your network intercepts TLS (corporate proxy such as Zscaler), point boto3
at your CA bundle: `export AWS_CA_BUNDLE=/path/to/ca-bundle.pem`. botocore
reads this the same way the AWS CLI did.

## 2. Provide your configuration (tfvars)

Copy the tracked templates to local `sandbox.tfvars` files and edit them. These
carry only non-secret **tags** (and the foundation cluster name) — the values
your account mandates. `sandbox.tfvars` is gitignored, so it stays on your
machine.

In a checkout, the templates are files on disk. Installed from PyPI, they are
part of the read-only payload `olf distribution path` prints, so the same
commands work either way:

```bash
DIST="$(olf distribution path)"
cp "$DIST/infra/terraform/foundations/aws-eks/sandbox.tfvars.example" sandbox.tfvars   # set your Owner tag
export AWS_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

`AWS_TFVARS_FILE` is reused for both the foundation and the platform apply, so
one file covers both. Set `Owner`/`Requester` to **your** email. If your
account needs an IAM naming prefix such as `limited-`, set that via
`AWS_CLUSTER_NAME`.

## 3. Common overrides (optional)

All have sane defaults; override via environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_REGION` | `eu-west-1` | Region for the foundation + platform |
| `AWS_CLUSTER_NAME` | `limited-eks-openlakeforge-poc` | EKS cluster name (must match `cluster_name` in the foundation tfvars) |
| `AWS_NODE_INSTANCE_TYPES` | `m7i.large` | Node group instance type(s) |
| `AWS_TFVARS_FILE` | `<dir>/sandbox.tfvars` | Path to your tfvars |
| `--kubeconfig-path` (CLI flag) | `.tmp/kubeconfigs/aws.yaml` in a checkout, `~/.openlakeforge/state/aws/kubeconfig.yaml` installed | Isolated EKS kubeconfig; not an environment variable |

## 4. Deploy

Three-step deploy (foundation, platform, then artifacts):

```bash
olf deploy --provider aws --phase foundation   # VPC, EKS, ECR, IAM; writes your kubeconfig context
olf deploy --provider aws --phase platform     # RDS, S3, Glue, Trino, Superset, Dagster, OpenMetadata
olf deploy --provider aws --phase artifacts    # build/push images, upload Floe manifests, load code
olf forward --provider aws                     # port-forward Superset/Dagster/etc. to localhost
```

`olf deploy --provider aws` with no `--phase` runs foundation, platform, and
artifacts in sequence.

## 5. Validate

With `olf forward --provider aws` running, run the end-to-end suite from a
second terminal:

```bash
olf e2e run --env aws
```

## 6. Tear down

```bash
olf destroy --provider aws --phase platform     # platform environment (RDS, buckets, Helm releases)
olf destroy --provider aws --phase foundation   # EKS, ECR, networking
olf destroy --provider aws                      # full teardown: platform, then foundation
```

ECR repositories use `force_delete`. Superset report ZIPs use ephemeral pod
storage, so teardown does not depend on deleting a reports PVC.
