# Reference CI/CD: build once, promote through stages

A project repository promotes one immutable project revision through its
shared stages: built once, activated and validated in DEV, optionally in UAT,
then activated unchanged in PROD. Promotion never rebuilds source and never
copies Bronze, Silver, Gold, Dagster, Superset or OpenMetadata state between
stages; each stage computes from its own Bronze.

OpenLakeForge ships two composite GitHub Actions for this. GitHub is the
reference CI, not part of the runtime contract: each action is a few `olf`
commands, and any CI can run the same ones.

| Action | Does | Outputs |
| --- | --- | --- |
| `OpenLakeForge/openlakeforge/.github/actions/build-revision` | `olf project image`, then `olf project build` | `image` (by digest), `revision` |
| `OpenLakeForge/openlakeforge/.github/actions/deploy-stage` | `olf project deploy --stage <s> --revision <r>`; fails unless `olf project status` then records exactly `<r>`; optionally `olf e2e run --suite full` | `status` (the `olf project status --json` document) |

Behaviour is driven by the stage and revision the caller passes, never by the
branch name. Activation changes only the selected stage's Dagster code
location, and never starts a schedule: the recurring schedules a revision
defines exist only in PROD and are created `STOPPED`.

## What a job needs

- **A runner holding the platform's `OLF_HOME`.** An installed `olf` keeps the
  platform's Terraform state and its kubeconfig under `OLF_HOME` (default
  `~/.openlakeforge`) and reads the cluster, region and registry from them.
  Until remote state lands (#132), run these jobs on a self-hosted runner that
  holds that directory, for example the machine that ran `olf platform apply`.
  `OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE` can stand in for the platform's
  contract ([provider contracts](../architecture/provider-contracts.md)), but not
  for the foundation state or the kubeconfig.
- **`olf`**: `pip install openlakeforge==<version>`, then `olf toolchain install`
  for the pinned Terraform, Helm and kubectl.
- **Cloud credentials** on AWS or Azure (for example
  `aws-actions/configure-aws-credentials` with `id-token: write`, or
  `azure/login`): the registry and the ops bucket are reached with them.
- **Registry access** for the build job, to push the project-code image to the
  platform's registry (ECR on AWS, ACR on Azure).

## Example caller workflow

`.github/workflows/promote.yml` in the project repository. Pin the actions to
the OpenLakeForge release your `openlakeforge` package matches, and every
third-party action to a commit SHA: these jobs hold cloud credentials. PROD,
and UAT when used, are GitHub environments with required reviewers, so a
promotion waits for approval; that protection is CI policy, not
OpenLakeForge's.

**Create the `uat` and `prod` environments with required reviewers before the
first run.** A workflow that names an environment GitHub has not seen creates
it without protection, and would then promote to PROD without waiting.

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
  OLF_VERSION: "0.3.0a1"

jobs:
  build:
    runs-on: [self-hosted, olf-platform]  # holds OLF_HOME until #132
    outputs:
      revision: ${{ steps.build.outputs.revision }}
      uat: ${{ steps.profile.outputs.uat }}
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - id: build
        uses: OpenLakeForge/openlakeforge/.github/actions/build-revision@v0.3.0-alpha.1
        with:
          # The platform's registry: an ECR repository on AWS, ACR on Azure.
          image-repository: ${{ vars.OLF_IMAGE_REPOSITORY }}
      - id: profile
        name: Is UAT enabled in the profile?
        run: echo "uat=$(olf profile resolve --project . --json | python3 -c 'import json, sys; print(str(any(s["name"] == "uat" and s["enabled"] for s in json.load(sys.stdin)["stages"])).lower())')" >> "$GITHUB_OUTPUT"

  dev:
    needs: build
    runs-on: [self-hosted, olf-platform]
    environment: dev
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - run: pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.3.0-alpha.1
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
      - run: pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.3.0-alpha.1
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
      - run: pip install "openlakeforge==${OLF_VERSION}" && olf toolchain install
      - uses: aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a  # v4
        with:
          role-to-assume: ${{ vars.OLF_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.OLF_AWS_REGION }}
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.3.0-alpha.1
        with:
          stage: prod
          revision: ${{ needs.build.outputs.revision }}
```

A `local` (kind) platform works the same way on the machine running the kind
cluster, with no cloud-credentials step. OpenLakeForge's own nightly
(`.github/workflows/local-full-e2e.yml`) runs both actions that way, from a
project `olf init` created with the wheel built from the same commit, reading
the contract through `OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE`.
