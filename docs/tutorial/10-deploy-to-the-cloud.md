# Deploy to the cloud

This lesson runs the same project on AWS: EKS for Kubernetes, ECR for images,
S3 for storage, RDS PostgreSQL for service state and AWS Glue as the Iceberg
catalog. Azure follows the same steps with different values, listed at the
end.

**You will** deploy the project you built to your own AWS account and
validate it, in about 60 minutes plus the time AWS needs to create the
cluster.

**Start from** the project of [lesson 9](09-add-prod-and-promote.md). Your
`lakehouse_code/` does not change: only the Deployment Profile and the
provider do. You need a running Docker engine and an AWS identity allowed to
create a VPC, an EKS cluster, ECR repositories, IAM roles, RDS, S3 buckets and
Glue databases; administrator access in a dedicated account is the simplest.

> [!WARNING]
> This creates billable AWS resources and is not production-hardened. Run the
> teardown step when you are done. The limits are listed in
> [Deploy to AWS](../guides/cloud/deploy-to-aws.md#limitations-and-costs).

## 1. Point the profile at AWS

In `openlakeforge.yaml`, set the provider and the region, keep only the DEV
stage, remove the `access:` block, and replace any `identity:` block with this
one:

```yaml
spec:
  provider:
    type: aws
    region: eu-west-1
  stages:
    dev:
      enabled: true
  identity:
    issuer: external
    issuer_url: https://login.example.com/
    role_claim: groups
    role_mapping:
      platform-admin: [olf-admins]
```

- `region` decides where everything is created.
- The cloud deployment is validated with one stage, so it runs your project in
  DEV. The DEV-to-PROD promotion of lesson 9 stays on your local platform.
- `access: {issuer: none}` (plain HTTP) is for `provider: local` only.
- A profile without `identity` is accepted only for `provider: local`. The
  cloud deployment does not use it for sign-in yet, because its services are
  reached through `olf forward`, so these placeholder values are enough.

```bash
olf profile validate --project .
```

## 2. Authenticate

If you already use an AWS profile, adopt it:

```bash
olf auth login --provider aws --profile "<your-profile>"
```

With IAM Identity Center (SSO), pass your AWS access portal URL and the region
of your Identity Center instance instead:

```bash
olf auth login --provider aws \
  --start-url "https://<your-portal>.awsapps.com/start" \
  --sso-region "<identity-center-region>"
```

No AWS CLI is needed. `olf auth status --provider aws` shows the identity `olf`
will use.

The cloud pushes your project image to its own ECR registry, so clear the
local registry settings from lesson 9 if this shell still has them:

```bash
unset PROJECT_CODE_IMAGE_REPOSITORY PROJECT_CODE_IMAGE_TAG
```

## 3. Deploy

The same phases you used locally, one at a time:

```bash
olf deploy --provider aws --phase foundation   # VPC, EKS, ECR, IAM
olf deploy --provider aws --phase platform     # S3, Glue, RDS, Trino, Dagster, Superset, OpenMetadata
olf deploy --provider aws --phase artifacts    # build and push images, load your code
```

`olf deploy --provider aws` with no `--phase` runs all three. The cloud has no
public endpoint. To open the services in a browser, port-forward them to your
machine, then stop with Ctrl-C:

```bash
olf forward --provider aws
```

Dagster is then at `http://localhost:3000` and Superset at
`http://localhost:8088`.

## Check it worked

Stop `olf forward` first: run one `olf` command at a time. The suite opens its
own port-forwards:

```bash
olf e2e run --env aws
```

It launches every product's job on EKS, verifies the Silver and Gold tables
through Trino and Glue, and checks the dashboards and OpenMetadata assets. It
ends with a line of the form `<Env> OpenLakeForge <suite> e2e validation
passed.`

## Tear down

```bash
olf destroy --provider aws
```

This removes the platform, then the foundation, including the ECR images.

## Azure

Azure runs the same platform on AKS and ACR, keeping SeaweedFS, Polaris and
PostgreSQL inside the cluster. Set `provider.type: azure` and delete
`provider.region`, then authenticate and describe the resource group and node
size in a variables file:

```bash
olf auth login --provider azure
cat > azure.tfvars <<'HCL'
create_resource_group = true
resource_group_name   = "rg-openlakeforge"
location              = "westeurope"
node_vm_size          = "Standard_D4s_v5"
HCL
export AZURE_TFVARS_FILE="$(pwd)/azure.tfvars"
olf deploy --provider azure
olf e2e run --env azure
olf destroy --provider azure
```

Pick a `node_vm_size` your subscription offers in that region. The options are
in [Deploy to Azure](../guides/cloud/deploy-to-azure.md).

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
