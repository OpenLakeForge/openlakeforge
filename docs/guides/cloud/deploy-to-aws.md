# Deploy to AWS

How to run your project on AWS: EKS for Kubernetes, ECR for images, S3 for
storage, RDS PostgreSQL for service state and AWS Glue as the Iceberg catalog.
The `poc` in cluster names refers to the sandbox account used for validation,
not to a reduced platform.

Your `lakehouse_code/` does not change between providers; only the Deployment
Profile and the provider do. You need an initialized project, `docker` and
`python3` (3.12 or newer) on your `PATH`, and permission in the account to
create a VPC, an EKS cluster, an ECR registry, IAM roles, RDS, S3 and AWS Glue
databases. `olf` provisions its own Terraform, Helm and kubectl, and keeps
Terraform state locally, so run every command from the same machine and
project.

> [!WARNING]
> This creates billable AWS resources. It is a proof of concept, not a
> production-hardened platform. Run the teardown step when you are done.

## 1. Point the profile at AWS

In `openlakeforge.yaml`, set the provider and the region, keep only the DEV
stage, remove the local-only plain-HTTP setting, and add an identity block:

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

- `region` decides where everything is created.
- The AWS deployment serves one stage today, so it runs your project in DEV.
- Delete the `access:` block: `issuer: none` is for `provider: local` only.
- A profile without `spec.identity` is accepted only for `provider: local`, so
  keep the block. The AWS deployment does not use it for sign-in yet: its
  services are reached only through `olf forward` on your machine.

Check the profile:

```bash
olf profile validate --project .
```

## 2. Authenticate

Sign in with IAM Identity Center; no AWS CLI is needed:

```bash
olf auth login --provider aws \
  --start-url "https://your-company.awsapps.com/start" \
  --sso-region "eu-west-1"
```

Use your AWS access portal URL and the region where your IAM Identity Center
instance is configured, which can differ from the region you deploy to. It
opens an AWS-hosted device authorization page. If the browser cannot open, add
`--no-browser` and enter the printed code. To reuse an existing SDK or CLI SSO
profile, run `olf auth login --provider aws --profile "my-profile"`, and export
`AWS_PROFILE` if needed.

If your network intercepts TLS, point boto3 at your CA bundle:
`export AWS_CA_BUNDLE=/path/to/ca-bundle.pem`.

## 3. Provide your tags

Copy the tfvars template and set the `Owner` and `Requester` tags to your own
email, plus any tag your account mandates. The file is gitignored and stays on
your machine:

```bash
DIST="$(olf distribution path)"
cp "$DIST/infra/terraform/foundations/aws-eks/sandbox.tfvars.example" sandbox.tfvars
export AWS_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

`AWS_TFVARS_FILE` is reused for the foundation and the platform apply. If your
account needs an IAM naming prefix such as `limited-`, set it through
`AWS_CLUSTER_NAME`. Other overrides are optional environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_CLUSTER_NAME` | `limited-eks-openlakeforge-poc` | EKS cluster name |
| `AWS_NODE_INSTANCE_TYPES` | `m7i.large` | Node group instance type or types |
| `AWS_TFVARS_FILE` | `sandbox.tfvars` in the foundation directory | Path to your tfvars |
| `AWS_REGION` | `eu-west-1` | Used only when the profile sets no `region` |

The kubeconfig lives at `~/.openlakeforge/state/aws/kubeconfig.yaml` for an
installed `olf`; `--kubeconfig-path` on the commands moves it.

The cloud pushes your project image to the ECR registry it creates, so clear
local registry settings if this shell still has them:

```bash
unset PROJECT_CODE_IMAGE_REPOSITORY PROJECT_CODE_IMAGE_TAG
```

## 4. Deploy

Run the three phases one at a time:

```bash
olf deploy --provider aws --phase foundation   # VPC, EKS, ECR, IAM
olf deploy --provider aws --phase platform     # RDS, S3, Glue, Trino, Dagster, OpenMetadata, Superset
olf deploy --provider aws --phase artifacts    # build and push images, upload Floe manifests, load your code
```

`olf deploy --provider aws` with no `--phase` runs all three. The cloud has no
public ingress or DNS: reach the services by port-forwarding them to your
machine, and leave this running in a second terminal:

```bash
olf forward --provider aws
```

## 5. Validate

With `olf forward` running, from the first terminal:

```bash
olf e2e run --env aws
```

It launches every product's job on EKS, verifies the Silver and Gold tables
through Trino and Glue, and checks the dashboards and OpenMetadata assets. It
ends with a line of the form `<Env> OpenLakeForge <suite> e2e validation
passed.`

## 6. Tear down

```bash
olf destroy --provider aws
```

This removes the platform (RDS, buckets, Helm releases), then the foundation
(EKS, ECR, networking). To go one phase at a time, run `--phase platform` first:
`--phase foundation` refuses to run while the platform still exists. ECR
repositories are force-deleted with their images.

To promote revisions through CI on AWS, see [Promote DEV to PROD](../promote-dev-to-prod.md); on
AWS today, deploy to the `dev` stage only and leave out the PROD job.
