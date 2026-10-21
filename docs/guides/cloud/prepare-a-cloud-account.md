# Prepare a cloud account

What to have ready before you deploy OpenLakeForge to your own AWS or Azure
account: tools, permissions, local Terraform state, and where account values
live. The deployments themselves are in [Deploy to AWS](deploy-to-aws.md) and
[Deploy to Azure](deploy-to-azure.md).

Credentials never reach git: `olf` authenticates through the cloud SDKs, and
the `sandbox.tfvars` file and your environment variables stay local and
gitignored. The Deployment Profile, `openlakeforge.yaml`, is committed with
your project and holds the provider, region and identity settings, so keep
secrets out of it.

> [!WARNING]
> A cloud deployment creates billable resources, and it is a proof of concept,
> not a production-hardened platform. Tear it down when you are done.

## Install the tools

Installed from PyPI (`pip install openlakeforge`, or
`uv tool install "openlakeforge==0.4.0a1" --python 3.12`), you need
`docker` and `python3` (3.12 or newer) on your `PATH`. `olf` provisions its own
versioned Terraform, kubectl and Helm under `OLF_HOME` (default
`~/.openlakeforge`). Neither the `aws` nor the `az` CLI is required. Set
`OLF_TOOLCHAIN_MODE=host` to use Terraform, kubectl and Helm from your `PATH`
instead.

## Check your permissions

| Provider | The account must allow you to create |
| --- | --- |
| AWS | A VPC, an EKS cluster, an ECR registry, IAM roles, RDS, S3 buckets and AWS Glue databases |
| Azure | An AKS cluster and an ACR registry in a resource group, which you either supply or let Terraform create |

Your account may also mandate resource tags, an IAM naming prefix or an allowed
VM size. The deploy guides show where each goes.

## Keep state and configuration local

- **Credentials** are never in the repository. The cloud SDK resolves them from
  the browser sign-in of `olf auth login`, a reused vendor session or an
  automation identity.
- **`*.tfvars`** files are gitignored; only the `*.tfvars.example` templates are
  tracked. Put every account-specific value in your local `sandbox.tfvars`.
- **Terraform state** (`*.tfstate`) and `.terraform/` are local only. There is no
  remote backend, so run the `olf` commands for a given environment from the
  same machine and project each time.

`olf distribution path` prints the read-only payload an installed `olf` ships,
which holds the templates:

```bash
DIST="$(olf distribution path)"
ls "$DIST/infra/terraform/foundations/aws-eks/sandbox.tfvars.example" \
   "$DIST/infra/terraform/foundations/azure-aks/sandbox.tfvars.example"
```

## Work behind a TLS-intercepting proxy

If your network intercepts TLS, such as a corporate proxy, point the cloud SDK
at your CA bundle:

```bash
export AWS_CA_BUNDLE=/path/to/ca-bundle.pem        # AWS
export REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem   # Azure
```

## Run providers side by side

A local, an AWS and an Azure platform can all exist at the same time. Each
uses its own kubeconfig, Helm cache, Docker credential directory, report work
directory and port-forward logs, scoped by provider, and none switches the
current context in your global kubeconfig. Run one `olf` command at a time,
though: `olf` is a single-operator tool, and two invocations running together
are not supported, whatever their providers.

## Check it worked

```bash
olf toolchain list
olf doctor --provider aws --phase foundation
docker ps
```

The first lists the managed tools (or fails with the reason). The second
reports `PASS` for the project and the tools, and it checks your cloud
authentication; use `--provider azure` for Azure. It does not check Docker,
which the artifacts phase needs to build images, so the third confirms the
engine is running. Authenticate first
if it reports a failed login, as described in the deploy guide.
