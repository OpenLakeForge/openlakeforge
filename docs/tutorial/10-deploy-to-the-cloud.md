# Deploy to the cloud

This lesson runs the same project on AWS: EKS for Kubernetes, ECR for images,
S3 for storage, RDS PostgreSQL for service state and AWS Glue as the Iceberg
catalog. Azure (AKS and ACR) is a preview and follows the same steps with
different values, listed at the end.

**You will** deploy the project you built to your own AWS account and
validate it, in about 60 minutes plus the time AWS needs to create the
cluster.

**Start from** the project of [lesson 9](09-add-prod-and-promote.md). Your
`lakehouse_code/` does not change: only the Deployment Profile and the
provider do. You need `docker` and `python3` on your `PATH` and permission to
create a VPC, an EKS cluster, an ECR registry, IAM roles, RDS, S3 and AWS Glue
databases in the account. `olf` provisions its own Terraform, Helm and
kubectl.

> [!WARNING]
> This creates billable AWS resources. It is a proof of concept, not a
> production-hardened platform. Run the teardown step when you are done.

## 1. Point the profile at AWS

In `openlakeforge.yaml`, set the provider and the region to deploy to, keep
only the DEV stage, remove the local-only plain-HTTP setting, and add an
identity block:

```yaml
spec:
  provider:
    type: aws
    region: eu-west-1
  stages:
    dev:
      enabled: true
    prod:
      enabled: false
  identity:
    issuer: external
    issuer_url: https://login.example.com/
    role_claim: groups
    role_mapping:
      platform-admin: [olf-admins]
```

- `region` decides where everything is created. To use another region, change
  it here.
- `stages`: the AWS deployment serves one stage today, so this lesson runs
  your project in DEV. The DEV-to-PROD promotion of lesson 9 stays on your
  local platform.
- Delete the `access:` block: `issuer: none` is for `provider: local` only.
- `identity` replaces any `spec.identity` from lesson 8. A profile without it
  is accepted only for `provider: local`, so keep this block as it is. The AWS
  deployment does not use it for sign-in yet: its services are reached only
  through `olf forward` on your machine.

Check the profile:

```bash
olf profile validate --project .
```

## 2. Authenticate

Sign in with IAM Identity Center; no AWS CLI is needed:

```bash
olf auth login --provider aws \
  --start-url "https://your-company.awsapps.com/start" \
  --sso-region "<identity-center-region>"
```

Use your AWS access portal URL and the region where your IAM Identity Center
instance is configured, which can differ from the region you deploy to. It
opens an AWS-hosted device authorization page. If the browser cannot open,
add `--no-browser` and enter the printed code.

## 3. Provide your tags

Copy the tfvars template and set the `Owner` and `Requester` tags to your
email. The file stays on your machine:

```bash
DIST="$(olf distribution path)"
cp "$DIST/infra/terraform/foundations/aws-eks/sandbox.tfvars.example" sandbox.tfvars
export AWS_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

The defaults (cluster `limited-eks-openlakeforge-poc`, node type
`m7i.large`) can be overridden with `AWS_CLUSTER_NAME` and
`AWS_NODE_INSTANCE_TYPES`; see [Deploy to AWS](../setup/aws.md) for the full
list. The region comes from `spec.provider.region` in the profile.

The cloud pushes your project image to the ECR registry it creates, so clear
the local registry settings from lesson 9 if this shell still has them:

```bash
unset PROJECT_CODE_IMAGE_REPOSITORY PROJECT_CODE_IMAGE_TAG
```

## 4. Deploy

The same three phases you used locally, one at a time:

```bash
olf deploy --provider aws --phase foundation   # VPC, EKS, ECR, IAM
olf deploy --provider aws --phase platform     # RDS, S3, Glue, Trino, Dagster, OpenMetadata, Superset
olf deploy --provider aws --phase artifacts    # build and push images, upload Floe manifests, load your code
```

`olf deploy --provider aws` with no `--phase` runs all three. The cloud has no
public ingress or DNS. To open the services in a browser, port-forward them to
your machine; it keeps running until you stop it with Ctrl+C:

```bash
olf forward --provider aws
```

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

This removes the platform, then the foundation. To remove one phase at a time,
run `--phase platform` first: `--phase foundation` refuses to run while the
platform still exists.

## Azure (preview)

Azure runs the same platform on AKS and ACR, with the in-cluster SeaweedFS,
Polaris and PostgreSQL. Set `provider.type: azure` and delete
`provider.region`: Azure takes its region from `location` in `sandbox.tfvars`.
Authenticate with `olf auth login --provider azure` (add `--device-code` on a
headless machine), copy
`infra/terraform/foundations/azure-aks/sandbox.tfvars.example` from `olf
distribution path` to `sandbox.tfvars` with your resource group, region and VM
size, export `AZURE_TFVARS_FILE`, then:

```bash
olf deploy --provider azure
olf e2e run --env azure
olf destroy --provider azure
```

To open the services in a browser between the deploy and the teardown, run
`olf forward --provider azure` and stop it with Ctrl+C before the next `olf`
command.

See [Deploy to Azure](../setup/azure.md).

## What you learned

- The project does not change between local, AWS and Azure; the profile's
  provider does.
- A cloud deploy is the same foundation, platform and artifacts phases.
- Cloud services are reached with `olf forward`, and `olf e2e run --env aws`
  validates them.

## Next

- [Reference CI/CD](../reference/ci-cd.md): build and deploy revisions from
  CI. On AWS today, deploy them to the `dev` stage only; leave out the PROD
  job.
- [Architecture overview](../architecture/overview.md): how the provider
  contracts keep the project portable.
