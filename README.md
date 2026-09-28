# OpenLakeForge

**An open-source lakehouse platform for small data teams — self-hosted, modular, and designed to run anywhere.**

[![Release](https://img.shields.io/github/v/release/OpenLakeForge/openlakeforge?include_prereleases)](/releases)  
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](/LICENSE)  
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](/docs/industrialization-roadmap.md)

Modern data platforms are powerful, but building one often means assembling and operating a growing collection of infrastructure, orchestration, storage, catalog, governance and analytics tools.

**OpenLakeForge packages those pieces into one opinionated, open-source lakehouse platform**, so small data teams can focus on data products instead of becoming platform engineers.

Deploy only the core data platform when that is all you need, add governance and BI when you need them, and keep the same architecture whether you run locally, on AWS or on Azure.

![OpenLakeForge Architecture](/docs/assets/openlakeforge_v1.png)

> Looking for the implementation details? See the [architecture documentation](/docs/architecture/README.md) and [technical diagrams](/docs/architecture/diagrams/README.md).

## Why OpenLakeForge?

- 🪶 **Built for small data teams** — reduce the operational work required to run a modern lakehouse.
    
- 🧩 **Modular by design** — use the core data platform alone or add governance and analytics.
    
- 🔓 **Open and self-hosted** — your infrastructure, your storage, your data.
    
- ☁️ **Deploy anywhere** — local Kubernetes, AWS and Azure share the same platform contracts.
    
- 🧊 **Open lakehouse architecture** — Apache Iceberg keeps storage independent from the engines operating on it.
    
- 🏗️ **Infrastructure included** — Kubernetes, Terraform and Helm deployment are part of the platform instead of something every team has to rebuild.
    

## The stack

OpenLakeForge provides an end-to-end batch data platform from ingestion to analytics.

|Capability|Technology|
|---|---|
|Extraction|dlt|
|Validation & technical contracts|Floe|
|Table format|Apache Iceberg|
|Transformation|dbt-trino|
|Query engine|Trino|
|Orchestration|Dagster|
|Data catalog|Apache Polaris / AWS Glue|
|Object storage|S3-compatible storage / AWS S3|
|Governance|OpenMetadata _(optional)_|
|Dashboards|Superset _(optional)_|

The underlying infrastructure is exposed through provider-neutral contracts, allowing infrastructure implementations to change without redefining the data platform. 

Local, AWS and Azure therefore expose the same platform capabilities while using
different infrastructure implementations.

→ [Learn how provider portability works](/docs/architecture/provider-contracts.md)

## Quick start

The easiest way to evaluate OpenLakeForge is with the local Kubernetes environment.

### Requirements

You need a working:

**Docker engine · Python >= 3.12**

`olf` (#127) provisions its own versioned Terraform, Helm, kubectl, and kind
under `~/.openlakeforge` at the exact versions
[`release/component-catalog.yaml`](release/component-catalog.yaml) pins — you
do not need to install them yourself. Set `OLF_TOOLCHAIN_MODE=host` to use
your own host-installed copies instead.

Make sure Docker is available from your shell:

```bash
docker ps
```

Create a project directory, install the release, and initialize its writable
user code:

```bash
mkdir my-lakehouse
cd my-lakehouse

pip install "openlakeforge==0.2.0a1"
olf init
```

`uv tool install "openlakeforge==0.2.0a1" --python 3.12` works too, but `uv`
is not a prerequisite.

`olf init` verifies the packaged platform payload, provisions or reuses the
pinned toolchain, checks that Docker is reachable, and creates the writable
project files `openlakeforge.yaml` and `lakehouse_code/`. Validate the project
boundary and the Deployment Profile with `olf project validate --project .`,
or resolve the profile on its own with `olf profile resolve --project . --json`.
Use `olf init --empty` to start from a
bare project instead — it has no source or product yet, so scaffold both with
`olf source new` and `olf product new` before deploying.

`olf init` writes a Slim profile, so deploying needs no preset flag — the
profile is what `olf deploy` and `olf e2e run` both resolve:

```bash
olf deploy --provider local
```

For a stage-aware v0.3 deployment, apply the profile-owned platform first,
then activate a previously built immutable project revision. The activation
does not run Terraform or rebuild the project:

```bash
olf platform apply -f openlakeforge.yaml
olf project image  -f openlakeforge.yaml            # -> <repository>@sha256:<digest>
olf project build  --project . --image <repository>@sha256:<digest>
olf project deploy -f openlakeforge.yaml --stage dev --revision sha256:<digest>
olf project status -f openlakeforge.yaml --json
```

A revision names its image by registry digest, so this loop needs a registry
even locally. Cloud uses the foundation's own; local pushes to
`PROJECT_CODE_IMAGE_REPOSITORY` (GHCR or Docker Hub) — see
[docs/setup/local.md](docs/setup/local.md).

Promoting that same revision to another stage never rebuilds it:

```bash
olf project deploy -f openlakeforge.yaml --stage prod --revision sha256:<digest>
```

Slim contains the complete data path while leaving out the optional governance and dashboarding services.

Run the included data products end-to-end:

```bash
olf e2e run --env local --suite full
```

Start local port forwarding:

```bash
olf forward --provider local
```

Dagster is then available at:

```text
http://localhost:3000
```

From there you can inspect the asset graph and launch the example pipelines.

### Run the complete platform

To include **OpenMetadata** and **Superset**, set `preset: full` in
`openlakeforge.yaml` and redeploy. The preset is a profile field, not a flag,
so the deployment and its validation cannot name different topologies:

```bash
olf deploy --provider local
olf e2e run --env local --suite full
olf forward --provider local
```

### Slim or Full?

OpenLakeForge ships the same core platform in two footprints.

|Profile|Includes|Use it when|
|---|---|---|
|**Slim**|Ingestion, validation, Iceberg, catalog, Trino, dbt and Dagster|You want the shortest path to a working lakehouse|
|**Full**|Slim + OpenMetadata + Superset|You also want governance, lineage and dashboards|

The optional layers do not change the underlying ingestion-to-Gold data path.


## Deploy to AWS

The AWS environment implements the same OpenLakeForge data platform using AWS infrastructure including **EKS, ECR, S3, RDS PostgreSQL and AWS Glue**.

The `poc` name reflects the sandbox environment used to validate the deployment. It does **not** represent a reduced OpenLakeForge stack.

### 1. Authenticate AWS

For standard credentials:

```bash
olf auth login --provider aws --start-url "https://example.awsapps.com/start" --sso-region eu-west-1
```

OpenLakeForge opens the AWS-hosted IAM Identity Center page. It does not
collect credentials or host an authentication page. Existing AWS SDK/CLI
profiles can be adopted with `olf auth login --provider aws --profile NAME`.

### 2. Configure the deployment

AWS account-specific configuration such as tags is kept in a local, gitignored
`sandbox.tfvars` file. `olf distribution path` prints the read-only payload
root in both installed and checkout mode, so this works either way:

```bash
cp "$(olf distribution path)/infra/terraform/foundations/aws-eks/sandbox.tfvars.example" sandbox.tfvars
# edit sandbox.tfvars: set Owner/Requester and any other account tags
export AWS_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

See the [AWS setup guide](/docs/setup/cloud-poc-setup.md#aws-eks) for the full configuration.

### 3. Deploy

```bash
olf deploy --provider aws
```

Then:

```bash
olf forward --provider aws

# Open a new terminal then
olf e2e run --env aws
```

The deployment follows the same foundation → platform → artifacts lifecycle as the local environment.


## Deploy to Azure

The Azure environment runs the same platform on **AKS and ACR** while retaining the OpenLakeForge platform services.

As with AWS, `poc` refers to the sandbox environment used for validation rather than a reduced platform implementation.

### 1. Authenticate Azure

```bash
olf auth login --provider azure
```

The Azure SDK opens Microsoft Entra's hosted sign-in page and asks you to
select a subscription. For a headless terminal, use `--device-code`.

### 2. Configure the deployment

`olf distribution path` prints the read-only payload root in both installed
and checkout mode, so this works either way:

```bash
cp "$(olf distribution path)/infra/terraform/foundations/azure-aks/sandbox.tfvars.example" sandbox.tfvars
# edit sandbox.tfvars: set your resource group, region, and AKS settings
export AZURE_TFVARS_FILE="$(pwd)/sandbox.tfvars"
```

See the [Azure setup guide](/docs/setup/cloud-poc-setup.md#azure-aks) for the full configuration.

### 3. Deploy

```bash
olf deploy --provider azure
```

Then:

```bash
olf forward --provider azure

# Open a new terminal then
olf e2e run --env azure
```

## Documentation

New to OpenLakeForge?

- 🚀 [Installation guide](/docs/setup/local.md)
- 🧱 [Build your first data product](/docs/getting-started/first-data-product.md)
- 📚 [Full documentation](/docs/README.md)

Going deeper:

- 🏗️ [Architecture overview](/docs/architecture/overview.md) 
- 📊 [Architecture diagrams](/docs/architecture/diagrams/README.md)   
- 🔌 [Provider contracts](/docs/architecture/provider-contracts.md)   
- ☁️ [AWS & Azure deployment](/docs/setup/cloud-poc-setup.md)
- 🧠 [Architecture Decision Records](/docs/adr/README.md)
- 📦 [Release compatibility](/docs/release/compatibility-matrix.md)
- 🗺️ [Project roadmap](/docs/industrialization-roadmap.md) 
- 📝 [Changelog](/CHANGELOG.md)
    

## Project status

OpenLakeForge is currently **alpha** and under active development.

The current focus is making the platform easier for small teams to install and extend: reducing the default runtime footprint, simplifying data-product onboarding and moving toward an installable release artifact instead of requiring users to operate directly from the repository.

Local, AWS and Azure environments exercise the same OpenLakeForge platform. Their validation and support maturity differ, and the project is progressively turning those tested environments into stable distribution targets.

Follow the [industrialization roadmap](/docs/industrialization-roadmap.md) for the current direction.

## Contributing

Contributions, experiments and feedback are welcome.

[AGENTS.md](/AGENTS.md) contains the repository map, architectural rules, development workflow and validation gates for contributors.

## License

OpenLakeForge is released under the [Apache License 2.0](/LICENSE).
