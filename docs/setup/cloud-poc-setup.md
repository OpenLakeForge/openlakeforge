# Cloud POC setup (AWS / Azure)

How to deploy the OpenLakeForge POC to your **own** AWS or Azure account. Nothing
account-specific is committed to the repo: `olf` authenticates through the
cloud SDKs and your configuration stays in local, gitignored files or
environment variables.

> The local (kind/SeaweedFS) path needs none of this — see the root `README.md`.
> This guide is only for the managed-cloud POCs.

## Prerequisites

Installed from PyPI (`pip install openlakeforge`), you need `docker` and
`python3` on your `PATH`. From a source checkout, you additionally need `git`
and `uv`; `make` is optional, deprecated compatibility whose targets delegate
to the same `olf` commands this guide uses directly.

`olf` provisions its own versioned `terraform`, `kubectl`, and `helm` under
`OLF_HOME` (default `~/.openlakeforge`). Neither `aws` nor `az` is required.
Set `OLF_TOOLCHAIN_MODE=host` to use host Terraform, kubectl, and Helm instead.

Terraform state is stored **locally** (no remote backend), so run the `olf`
commands from the same machine/project each time for a given environment.

---

## AWS (EKS)

The AWS steps live in [Deploy to AWS](aws.md).

---

## Azure (AKS)

The Azure steps live in [Deploy to Azure](azure.md).

---

## Concurrent deployments

The complete local, Azure, and AWS workflows can run at the same time. Each one
uses its own kubeconfig, Helm cache, Docker credential directory, report work
directory, and port-forward logs:

```bash
olf deploy --provider local &
olf deploy --provider azure &
olf deploy --provider aws &
wait
```

The default kubeconfig, Terraform state, and work/cache paths are all
scoped by *provider*, so the three different providers above never collide
with no extra flags needed. That scoping is per-provider, not per-invocation:
**two concurrent deployments of the same provider are not supported.** They
would resolve the same default kubeconfig, the same Terraform state file, and
the same work/cache directories regardless of `--kubeconfig-path` — that flag
only relocates the kubeconfig, not the Terraform state or work root the two
runs would still contend for. The workflows never switch the current context
in your global kubeconfig.

---

## What stays out of git

- **Credentials** — never in the repo; resolved by the cloud SDK from OLF
  browser authentication, vendor session reuse, or automation identity.
- **`*.tfvars`** — gitignored; only the `*.tfvars.example` templates are tracked.
- **Terraform state** (`*.tfstate`) and `.terraform/` — local only.

If you add a new account-specific value, put it in your local `sandbox.tfvars`,
not in a tracked file.
