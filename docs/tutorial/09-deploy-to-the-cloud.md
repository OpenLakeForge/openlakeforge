# Deploy to the cloud

This lesson runs the same project on AWS: EKS for Kubernetes, ECR for images,
S3 for storage, RDS PostgreSQL for service state and AWS Glue as the Iceberg
catalog.

**You will** deploy the project you built to your own AWS account and
validate it, in about 60 minutes plus the time AWS needs to create the
cluster.

**Start from** the project of [lesson 8](08-add-prod-and-promote.md), and an
AWS account that meets the
[prerequisites of the AWS guide](../guides/cloud/deploy-to-aws.md#prerequisites).
Your `lakehouse_code/` does not change: only the Deployment Profile does.

> [!WARNING]
> This creates billable AWS resources and is not production-hardened. Run the
> teardown step when you are done. The limits are listed in
> [Deploy to AWS](../guides/cloud/deploy-to-aws.md#limitations-and-costs).

## 1. Point the profile at AWS

In `openlakeforge.yaml`, set the provider and the region, keep only the DEV
stage, and remove the `access:` and `identity:` blocks:

```yaml
spec:
  provider:
    type: aws
    region: eu-west-1
  stages:
    dev:
      enabled: true
```

The cloud deployment is validated with one stage, so the DEV-to-PROD promotion
of lesson 8 stays on your local platform. The cloud deployment publishes no
routes, so there is no sign-in: you reach its services with `olf forward`, and
an `access:` block would require `identity:`.

```bash
olf profile validate --project .
```

## 2. Authenticate

```bash
olf auth login --provider aws
```

It reuses your `default` AWS profile, or asks for your IAM Identity Center
start URL and region. The other sign-in options are in
[Deploy to AWS](../guides/cloud/deploy-to-aws.md#1-authenticate). The cloud
pushes your image to its own ECR registry, so clear the registry settings of
lesson 8:

```bash
unset PROJECT_CODE_IMAGE_REPOSITORY PROJECT_CODE_IMAGE_TAG
```

## 3. Deploy

The same phases you used locally:

```bash
olf deploy --provider aws --phase foundation   # VPC, EKS, ECR, IAM
olf deploy --provider aws --phase platform     # S3, Glue, RDS, Trino, Dagster, Superset, OpenMetadata
olf deploy --provider aws --phase artifacts    # build and push images, load your code
```

The cloud has no public endpoint. To open the services, port-forward them to
your machine, then stop with Ctrl-C; Dagster is at `http://localhost:3000`:

```bash
olf forward --provider aws
```

## Check it worked

Stop `olf forward` first: run one `olf` command at a time.

```bash
olf e2e run --env aws
```

It launches every product's job on EKS and verifies the Silver and Gold
tables, the dashboards and OpenMetadata. It ends with a line of the form
`<Env> OpenLakeForge <suite> e2e validation passed.`

## Tear down

```bash
olf destroy --provider aws
```

On Azure the steps are the same with `--provider azure`, plus a variables file
for the resource group and node size; see
[Deploy to Azure](../guides/cloud/deploy-to-azure.md).

## What you learned

- The project does not change between local, AWS and Azure; the profile's
  provider does.
- A cloud deploy is the same foundation, platform and artifacts phases.
- Cloud services are reached with `olf forward`, and `olf e2e run --env aws`
  validates them.

## Next

- [Deploy to AWS](../guides/cloud/deploy-to-aws.md): every option, the
  limitations and the costs.
- [CI/CD and environment promotion](../guides/promote-dev-to-prod.md): build
  and deploy revisions from CI.
- [Architecture](../architecture/overview.md): how the same project runs on
  each provider.
