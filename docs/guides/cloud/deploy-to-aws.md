# Deploy to AWS

How to run your project in your own AWS account: EKS for Kubernetes, ECR for
images, S3 for storage, RDS PostgreSQL for service state and AWS Glue as the
Iceberg catalog. Your `lakehouse_code/` does not change; only the Deployment
Profile does.

> [!WARNING]
> The AWS deployment is alpha. It has passed the end-to-end suite in a test
> account but is not production-hardened, and it creates billable resources.
> Read [Limitations and costs](#limitations-and-costs) first, and tear it
> down when you are done.

## Prerequisites

- An initialized project (`olf init`) and `olf` installed as in
  [Install and configure a local deployment](../local-installation.md).
- A running Docker engine: the deploy builds and pushes images.
- An AWS identity allowed to create a VPC, an EKS cluster and its node group,
  ECR repositories, IAM roles and EKS Pod Identity associations, an RDS
  instance, S3 buckets and Glue databases. `AdministratorAccess` in a
  dedicated account is the simplest choice.

You do not need the AWS CLI, Terraform, Helm or kubectl: `olf` uses the AWS SDK
and its own pinned tools.

## 1. Authenticate

`olf` reads credentials through the AWS SDK, from the first source it finds:

| Source | How |
| --- | --- |
| Automation credentials | `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, a web identity token (GitHub Actions OIDC), a container role or an EC2 instance profile |
| An existing AWS profile | `olf auth login --provider aws --profile <name>` |
| IAM Identity Center (SSO) | `olf auth login --provider aws --start-url <portal URL> --sso-region <region>` |

For IAM Identity Center, the start URL is your AWS access portal URL, and
`--sso-region` is the region of your Identity Center instance, which can
differ from the region you deploy to. The command opens a browser to approve
the device; add `--no-browser` to print the URL and code instead. With no
option at all, `olf auth login --provider aws` reuses the `default` profile, or
asks for the start URL.

Check which identity `olf` will use:

```bash
olf auth status --provider aws
```

## 2. Point the profile at AWS

Edit `openlakeforge.yaml`:

```yaml
spec:
  provider:
    type: aws
    region: eu-west-1
  stages:
    dev:
      enabled: true
```

- `region` is where everything is created. Without it, `AWS_REGION` applies,
  then `eu-west-1`.
- Delete any `access:` block: the cloud publishes no routes, and an `access:`
  block would require `spec.identity`.
- Leave out `spec.identity`. The cloud deployment publishes no routes, so
  there is nothing to sign in to: you reach the services with `olf forward`
  from your machine, and authentication stays off.
- Keep DEV as the only stage: the cloud guides are validated with one stage.

```bash
olf profile validate --project .
```

## 3. Optional configuration

Everything has a default. Override with environment variables in the shell
that runs `olf`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_CLUSTER_NAME` | `eks-openlakeforge`, or the name an existing deployment already uses | EKS cluster name, also the prefix of the IAM roles |
| `AWS_NODE_INSTANCE_TYPES` | `m7i.large` | Node instance type(s) |
| `AWS_NODE_DESIRED_SIZE`, `AWS_NODE_MIN_SIZE`, `AWS_NODE_MAX_SIZE` | `3`, `1`, `4` | Node group size |
| `AWS_TFVARS_FILE` | none | A Terraform variables file, for resource tags |

Set the same values for every later command, including `olf e2e run` and
`olf destroy`: they find the cluster by its name.

To tag every resource, for cost tracking or because your organization requires
it, put the tags in a variables file and export its path:

```bash
cat > aws.tfvars <<'HCL'
default_tags = {
  Project = "openlakeforge"
  Owner   = "you@example.com"
}
HCL
export AWS_TFVARS_FILE="$(pwd)/aws.tfvars"
```

Keep it out of git. The same file applies to the cluster and the platform.

## 4. Deploy

```bash
olf deploy --provider aws
```

It runs three phases, which you can also run one at a time with `--phase`:

| Phase | Creates |
| --- | --- |
| `foundation` | VPC with two public subnets, EKS and its node group, ECR repositories, IAM roles |
| `platform` | S3 buckets, Glue databases, RDS PostgreSQL, Trino, Dagster; Superset and OpenMetadata with the Full preset |
| `artifacts` | Builds and pushes the project-code image to ECR and deploys your data products |

Creating the EKS cluster takes most of the time. If this shell still exports
`PROJECT_CODE_IMAGE_REPOSITORY` from a local promotion, unset it: the cloud
pushes to its own ECR.

## 5. Open and validate the platform

The cloud deployment has no public endpoint. Port-forward the services to
your machine, on the same ports as locally (Dagster on
`http://localhost:3000`, Trino on `8080`, Superset on `8088`, OpenMetadata on
`8585`), and stop with Ctrl-C:

```bash
olf forward --provider aws
```

Run the end-to-end suite after you stop `olf forward`: `olf` runs one command
at a time, and the suite opens its own port-forwards.

```bash
olf e2e run --env aws
```

It launches every product's job on EKS, checks the Silver and Gold tables
through Trino and Glue, and on Full also the dashboards and OpenMetadata. It
ends with `... e2e validation passed.`

## 6. Tear down

```bash
olf destroy --provider aws
```

It removes the platform (RDS, buckets, Helm releases), then the foundation
(EKS, ECR with its images, networking). Run it from the same machine and
project as the deploy: Terraform state is local (see below). With `--phase`,
destroy `platform` before `foundation`; the foundation refuses to go first.

## Limitations and costs

| Area | Today |
| --- | --- |
| Access | No ingress, DNS, TLS or sign-in. Services are reached with `olf forward` only, by whoever has credentials to the cluster, with no OpenLakeForge sign-in in front of them. |
| Stages | Validated with DEV only. |
| State | Terraform state stays on your machine under `~/.openlakeforge`. There is no remote backend, so losing that directory loses the ability to update or destroy the deployment with `olf`. |
| Secrets | Generated service passwords live in Kubernetes Secrets and in the local Terraform state. |
| Network | Nodes run in public subnets, and the EKS API endpoint accepts connections from any address (`0.0.0.0/0`), protected by IAM authentication. |
| Data | RDS is a single `db.t4g.micro` instance with no final snapshot on destroy. There are no backups. |
| `olf query` | Not supported on AWS yet. |

The main running costs are the EKS control plane, three `m7i.large` nodes and
the RDS instance, plus S3 storage and data transfer. They accrue until
`olf destroy` finishes.

Workload identity uses EKS Pod Identity rather than IRSA, because the
restricted test account the AWS deployment was built in denied creating an
OIDC provider. Deployments created before 0.5 keep their
`limited-eks-openlakeforge-poc` cluster name: `olf` reads it from the
foundation state.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `AWS is not authenticated` | Run `olf auth login --provider aws`, then `olf auth status --provider aws`. |
| `x509: certificate signed by unknown authority` from the SDK | A proxy intercepts TLS: `export AWS_CA_BUNDLE=/path/to/your-ca-bundle.pem`. |
| An `AccessDenied` or `UnauthorizedOperation` error | Your identity lacks a permission from the prerequisites. Organization policies (SCPs) can also require tags or names; set them with `AWS_TFVARS_FILE` and `AWS_CLUSTER_NAME`. |
| `olf e2e run` cannot find the cluster | Export the same `AWS_CLUSTER_NAME` as for the deploy. |

To promote revisions from CI, see
[CI/CD and environment promotion](../promote-dev-to-prod.md).
