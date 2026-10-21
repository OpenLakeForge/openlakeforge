# Promote DEV to PROD

How to build a project once into an immutable revision, activate it in DEV and
then unchanged in PROD, by hand and with GitHub Actions, and how to protect the
`prod` GitHub environment so a promotion waits for approval.

A project promotes one revision through its shared stages: built once,
activated and validated in DEV, optionally in UAT, then activated unchanged in
PROD. Promotion never rebuilds source and never copies Bronze, Silver, Gold,
Dagster, Superset or OpenMetadata state between stages; each stage computes
from its own Bronze.

## Enable the stages

Add `prod` (and `uat` if you want it) under `spec.stages` in
`openlakeforge.yaml`, or create the profile with `olf init --stages dev,prod`:

```yaml
spec:
  stages:
    dev:
      enabled: true
    prod:
      enabled: true
```

Then create the namespaces, buckets and catalogs of every enabled stage:

```bash
olf platform apply -f openlakeforge.yaml
```

Each stage has its own `olf-<stage>` namespace with its own Dagster (and
Superset with the Full preset), its own buckets and its own `lakehouse_<stage>`
catalog. The platform services in `olf-system` are shared.

## Promote by hand

A revision has its own id, and it must reference a digest-pinned image; an
image built straight into kind has no digest. A local platform therefore pushes to a registry you control:

```bash
export PROJECT_CODE_IMAGE_REPOSITORY=ghcr.io/your-account/project-code
export PROJECT_CODE_IMAGE_TAG=dev
docker login ghcr.io
```

Replace `your-account` with your own. On AWS and Azure the platform's own
registry (ECR, ACR) is used and you set nothing.

Build the revision once. `olf project image` builds the project-code image,
pushes it, loads it into kind and ends with a digest-pinned reference;
`olf project build` freezes your descriptors, Floe contracts, dbt projects,
Dagster code, reports and that image into a revision and prints its id:

```bash
IMAGE="$(olf project image -f openlakeforge.yaml | tee /dev/stderr | tail -n 1)"
REVISION="$(olf project build --project . --image "$IMAGE" | tee /dev/stderr | tail -n 1)"
echo "$REVISION"
```

Activate it in DEV and validate it there:

```bash
olf project deploy -f openlakeforge.yaml --stage dev --revision "$REVISION"
olf e2e run --env local -f openlakeforge.yaml --stage dev --suite full
```

Only when DEV passes, activate the same revision in PROD:

```bash
olf project deploy -f openlakeforge.yaml --stage prod --revision "$REVISION"
```

Activation is stage-scoped: it reconciles the stage's catalog namespaces,
activates its Floe revision, imports its Superset reports and OpenMetadata
metadata when those are on, and rolls out its Dagster code location. It starts
no Dagster schedule: the recurring schedules a revision defines exist only in
PROD and are created `STOPPED`. Switch one on in the PROD Dagster when you want
recurring runs.

## Automate with GitHub Actions

OpenLakeForge ships two composite actions. GitHub is the reference CI, not part
of the runtime contract: each action is a few `olf` commands, and any CI can
run the same ones.

| Action | Does | Outputs |
| --- | --- | --- |
| `OpenLakeForge/openlakeforge/.github/actions/build-revision` | `olf project image`, then `olf project build` | `image` (by digest), `revision` |
| `OpenLakeForge/openlakeforge/.github/actions/deploy-stage` | `olf project deploy --stage <s> --revision <r>`; fails unless `olf project status` then records exactly `<r>`; optionally `olf e2e run --suite full` | `status` |

Inputs: `build-revision` takes `image-repository` (required), `image-tag`
(default the commit SHA) and `profile` (default `openlakeforge.yaml`);
`deploy-stage` takes `stage` and `revision` (both required), `e2e-env`
(`local`, `aws` or `azure`; empty skips validation) and `profile`. Behaviour
follows the stage and revision you pass, never the branch name.

### What a job needs

- **A runner holding the platform's `OLF_HOME`.** An installed `olf` keeps the
  platform's Terraform state and kubeconfig under `OLF_HOME` (default
  `~/.openlakeforge`) and reads the cluster, region and registry from them.
  Until remote state lands (#132), run these jobs on a self-hosted runner that
  holds that directory, for example the machine that ran `olf platform apply`.
- **`olf`**: `python3.12 -m pip install openlakeforge==<version>`, then
  `olf toolchain install` for the pinned Terraform, Helm and kubectl.
- **Cloud credentials** on AWS or Azure (for example
  `aws-actions/configure-aws-credentials` with `id-token: write`, or
  `azure/login`).
- **Registry access** for the build job, to push the image to the platform's
  registry, and a working Docker CLI and daemon: `olf project image` builds and
  pushes with them.

### Example workflow

Save it as `.github/workflows/promote.yml` in the project repository. Pin the
actions to the OpenLakeForge release your `openlakeforge` package matches, and
every third-party action to a commit SHA: these jobs hold cloud credentials.
This version targets AWS; for a local platform, drop the credentials step and
use `e2e-env: local`.

> [!NOTE]
> The AWS deployment serves one stage today, and the profile in
> [Deploy to AWS](cloud/deploy-to-aws.md) enables only `dev`. With that profile,
> keep the `build` and `dev` jobs and delete the `uat` and `prod` jobs. A
> stage job only works for a stage the profile enables.

```yaml
name: Promote

on:
  push:
    branches: [main]
  workflow_dispatch:

# One promotion at a time: an older run resuming after approval must not
# overwrite a stage a newer run already promoted.
concurrency:
  group: promote
  cancel-in-progress: false

permissions:
  contents: read
  id-token: write  # cloud credentials via OIDC

env:
  OLF_VERSION: "0.4.0a1"

jobs:
  build:
    runs-on: [self-hosted, olf-platform]  # holds OLF_HOME until #132
    outputs:
      revision: ${{ steps.build.outputs.revision }}
      uat: ${{ steps.profile.outputs.uat }}
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: python3.12 -m pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - id: build
        uses: OpenLakeForge/openlakeforge/.github/actions/build-revision@v0.4.0-alpha.1
        with:
          # The platform's registry: an ECR repository on AWS, ACR on Azure.
          image-repository: ${{ vars.OLF_IMAGE_REPOSITORY }}
      - id: profile
        name: Is UAT enabled in the profile?
        # Fail closed: an empty value would skip UAT and let PROD run.
        run: |
          set -euo pipefail
          resolved="$(olf profile resolve --project . --json)"
          uat="$(python3 -c 'import json, sys; print(str(any(s["name"] == "uat" and s["enabled"] for s in json.load(sys.stdin)["stages"])).lower())' <<< "$resolved")"
          case "$uat" in true|false) ;; *) echo "::error::cannot tell whether UAT is enabled"; exit 1 ;; esac
          echo "uat=$uat" >> "$GITHUB_OUTPUT"

  dev:
    needs: build
    runs-on: [self-hosted, olf-platform]
    environment: dev
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: python3.12 -m pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.4.0-alpha.1
        with:
          stage: dev
          revision: ${{ needs.build.outputs.revision }}
          e2e-env: aws  # your profile's provider type; omit to skip validation

  uat:
    needs: [build, dev]
    if: needs.build.outputs.uat == 'true'
    runs-on: [self-hosted, olf-platform]
    environment: uat
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: python3.12 -m pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.4.0-alpha.1
        with:
          stage: uat
          revision: ${{ needs.build.outputs.revision }}
          e2e-env: aws

  prod:
    # Runs when UAT succeeded or was skipped because the profile has none.
    needs: [build, dev, uat]
    if: always() && needs.dev.result == 'success' && contains(fromJSON('["success", "skipped"]'), needs.uat.result)
    runs-on: [self-hosted, olf-platform]
    environment: prod
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: python3.12 -m pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.4.0-alpha.1
        with:
          stage: prod
          revision: ${{ needs.build.outputs.revision }}
```

On a local platform, the build job also needs `packages: write` and a
`docker login ghcr.io` step, and the stage jobs `packages: read`, so the image
can be pushed and pulled.

## Protect the `prod` environment

> [!WARNING]
> Create the `uat` and `prod` environments with required reviewers before the
> first run. A workflow that names an environment GitHub has not seen creates
> it without protection, and would then promote to PROD without waiting.

The approval is GitHub policy, not OpenLakeForge's. In the project repository:

1. Open **Settings**, then **Environments**, then **New environment**, and name
   it `prod`.
2. Turn on **Required reviewers** and add the people or teams who approve a
   PROD promotion.
3. Save, and repeat for `uat` if the profile enables it.

Required reviewers on a private repository need a GitHub plan that supports
them. A job that names `environment: prod` then waits for a reviewer before it
starts, so the build and DEV jobs run unattended and only the PROD activation
pauses.

## Check it worked

```bash
olf project status -f openlakeforge.yaml --json
olf e2e run --env local -f openlakeforge.yaml --stage prod --suite full
```

`olf project status` records the revision active in each stage; PROD shows the
same id as DEV. The e2e run launches every product's job in the PROD Dagster and
requires rows in every Gold mart of the `lakehouse_prod` catalog. In CI, the
`status` output of `deploy-stage` carries that document. Use `--env aws` or
`--env azure` on a cloud platform.
