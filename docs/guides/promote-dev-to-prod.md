# CI/CD and environment promotion

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
olf infra platform apply -f openlakeforge.yaml
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

Build the revision once. `olf infra project image` builds the project-code image,
pushes it, loads it into kind and ends with a digest-pinned reference;
`olf infra project build` freezes your descriptors, Floe contracts, dbt projects,
Dagster code, reports and that image into a revision and prints its id. An
unmodified example project with no `spec.project` skips the image build:
`olf infra project image` prints the release-pinned reference instead (a source checkout has no pin and builds).

```bash
IMAGE="$(olf infra project image -f openlakeforge.yaml | tee /dev/stderr | tail -n 1)"
REVISION="$(olf infra project build --project . --image "$IMAGE" | tee /dev/stderr | tail -n 1)"
echo "$REVISION"
```

Activate it in DEV and validate it there:

```bash
olf infra project deploy -f openlakeforge.yaml --stage dev --revision "$REVISION"
olf infra e2e run --env local -f openlakeforge.yaml --stage dev --suite full
```

`--env` is the profile's provider: use `--env aws` or `--env azure` on a cloud
platform.

Only when DEV passes, activate the same revision in PROD:

```bash
olf infra project deploy -f openlakeforge.yaml --stage prod --revision "$REVISION"
```

Activation is stage-scoped: it reconciles the stage's catalog namespaces,
imports its Superset reports and OpenMetadata metadata when those are on, and
rolls out its Dagster code location, which renders the stage's Floe profile
when it loads. It starts
no Dagster schedule: the recurring schedules a revision defines exist only in
PROD and are created `STOPPED`. Switch one on in the PROD Dagster when you want
recurring runs.

## Automate with GitHub Actions

OpenLakeForge ships two composite actions. GitHub is the reference CI, not part
of the runtime contract: each action is a few `olf` commands, and any CI can
run the same ones.

| Action | Does | Outputs |
| --- | --- | --- |
| `OpenLakeForge/openlakeforge/.github/actions/build-revision` | `olf infra project image`, then `olf infra project build` | `image` (by digest), `revision` |
| `OpenLakeForge/openlakeforge/.github/actions/deploy-stage` | `olf infra project deploy --stage <s> --revision <r>`; fails unless `olf infra project status` then records exactly `<r>`; optionally `olf infra e2e run --suite full` | `status` |

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
  holds that directory, for example the machine that ran `olf infra platform apply`.
  `OPENLAKEFORGE_PROVIDER_CONTRACTS_FILE`, the JSON that
  `olf infra platform contract` prints, can stand in for the platform's contract,
  but not for the foundation state or the kubeconfig.
- **`olf`**: `python3.12 -m pip install openlakeforge==<version>`, then
  `olf toolchain install` for the pinned Terraform, Helm and kubectl.
- **Cloud credentials** on AWS or Azure (for example
  `aws-actions/configure-aws-credentials` with `id-token: write`, or
  `azure/login`).
- **Registry access** for the build job, to push the image to the platform's
  registry, and a working Docker CLI and daemon: `olf infra project image` builds and
  pushes with them.

### Example workflow

Save it as `.github/workflows/promote.yml` in the project repository. Pin the
actions to the OpenLakeForge release your `openlakeforge` package matches, and
every third-party action to a commit SHA: these jobs hold cloud credentials.
This version targets AWS; for a local platform, drop the credentials step and
use `e2e-env: local`.

> [!NOTE]
> The cloud deployments are validated with one stage, and the profile in
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
  OLF_VERSION: "0.6.0a3"

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
        uses: OpenLakeForge/openlakeforge/.github/actions/build-revision@v0.6.0-alpha.3
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
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.6.0-alpha.3
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
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.6.0-alpha.3
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
      - uses: OpenLakeForge/openlakeforge/.github/actions/deploy-stage@v0.6.0-alpha.3
        with:
          stage: prod
          revision: ${{ needs.build.outputs.revision }}
```

## Publish to GHCR from the workflow

A local platform has no registry of its own, so a customer project publishes to
GHCR. Declare the repository in `spec.project.image.repository` (see
[`spec.project`](../reference/deployment-profile.md#specproject)) or set
`image-repository` on `build-revision`; it must be lowercase. In the example
workflow above, drop the cloud-credentials step, set `e2e-env: local`, give the
build job `packages: write` and every stage job `packages: read`, and add this
step to each job before its OpenLakeForge action, so each one can push or pull
the image on any runner:

```yaml
      - run: echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u "${{ github.actor }}" --password-stdin
```

`build-revision` pushes the image, resolves its registry digest and freezes
that digest into the revision, so DEV and PROD activate the same image without
a rebuild. The repository's own nightly (`.github/workflows/local-full-e2e.yml`)
runs this path, build then DEV then PROD, against GHCR on kind.

A package the workflow creates is private. If the repository cannot write to
it on a later run, grant the repository access under the package's **Manage
Actions access** settings.

### Who authenticates, and with what

| Step | Credential | Lifetime |
| --- | --- | --- |
| Push the image (`build-revision`) | `GITHUB_TOKEN` with `packages: write` and the package's Actions access | The job |
| Read the image on a runner (`deploy-stage`) | `GITHUB_TOKEN` with `packages: read` | The job |
| Pull in the cluster (code servers, Dagster run pods, the log-archive CronJob) | The Secret named by `spec.project.image.pull_secret_ref`, created by you in each stage namespace from a read-only token | Until you rotate it |
| Public image | None | n/a |

Never copy `GITHUB_TOKEN` into a Kubernetes Secret: it expires when the job
ends, so pods would fail to pull after a reschedule. See GitHub's
[token lifetime documentation](https://docs.github.com/en/actions/concepts/security/github_token).
`olf` never creates the
pull Secret;
[Pulling a private image](../reference/deployment-profile.md#pulling-a-private-image)
shows how, and activation refuses to start when the Secret is missing, of the
wrong type or lacks the image's registry.

### Check that the cluster pulls through the Secret

On kind, activation pulls the image on your machine and loads it into every
node, so a missing or wrong `imagePullSecrets` entry stays hidden until a node
has to pull for itself. `olf infra e2e registry-pull` removes that cache and
proves the kubelet pulls with the Secret:

```bash
olf infra e2e registry-pull -f openlakeforge.yaml --stage dev
```

It reads the image from the stage's active revision and the Secret from
`spec.project.image.pull_secret_ref`, then:

1. checks that every Deployment, StatefulSet and CronJob running the image
   lists the Secret, and so does the stage's `dagster` ServiceAccount, which
   Floe Jobs inherit it from. `--static-only` stops after step 2 and removes
   nothing from the nodes;
2. asks the registry for the image's manifest without credentials. If that
   works, the image is public, the Secret is not what lets the cluster pull
   it, and the check fails with `image is public: pull-secret path not
   exercised` unless you pass `--allow-public`;
3. removes the image from every node (`crictl rmi` and `ctr images rm` through
   `docker exec`, with an empty `DOCKER_CONFIG` so no host login is involved,
   then a containerd restart on a node whose CRI image record survives),
   deletes the code location pods, and requires a `Successfully pulled` event
   from a pod on each node rather than only `already present on machine`;
4. removes the image again, launches one run of the project's first product
   and requires the pods it creates to pull the same way (the first pod on a
   node pulls, later pods there find that pull) and to list the Secret, which
   Floe pods get from the ServiceAccount;
5. removes the image again, cordons the node that runs a code location, deletes
   the pod and requires it to start on another node with a real pull.

The kind cluster `olf` creates has a control-plane node and two workers
(`infra/kind/local/kind-cluster.yaml`). kind cannot add a node to a running
cluster, so step 5 uses the second worker; with a single schedulable node it
is skipped, and step 3 already shows that no node-local cache is relied on.
The check launches a run, so do not use it while a restore is in progress.
It never reads or prints the Secret's data.

The repository's nightly runs it for DEV and PROD after the PROD suite, with
`--allow-public`. That is a known gap: the nightly's `project-code-nightly`
package answered an anonymous manifest request when this was written (a new
package is private by default, so its visibility was changed), and its Secret is built from the job's `GITHUB_TOKEN`, which expires when the job
ends. The nightly therefore proves the mechanism, not that a private image
survives the operator and the runner going away.

To prove that, a maintainer runs it by hand against a private package:

1. Make the package private under its **Package settings**, and push an image
   to it (`olf infra project image`, then `olf infra project build`).
2. Create a personal access token (classic) with only `read:packages`, and the
   Secret in each stage namespace, as in
   [Pulling a private image](../reference/deployment-profile.md#pulling-a-private-image).
   Name it in `spec.project.image.pull_secret_ref` and run
   `olf infra project deploy` for the stage.
3. Log out of the registry on the host (`docker logout ghcr.io`), stop any CI
   job that could push or pull, and run
   `olf infra e2e registry-pull -f openlakeforge.yaml --stage dev` without
   `--allow-public`. It must report that the image is not anonymously
   pullable and finish with the image pulled through the Secret.
4. Revoke the token you used to push, if it differs from the `read:packages`
   one, and run the check once more: the pull still works because only the
   Secret is used.

### When no registry is configured

| Project | Registry |
| --- | --- |
| No `spec.project` | The release's public example image; nothing is built or pushed |
| Customer project on `aws` or `azure`, no `image.repository` | The platform's ECR or ACR |
| Customer project on `local`, no `image.repository` and no `PROJECT_CODE_IMAGE_REPOSITORY` | Refused: declare a registry; the pipeline never publishes to the public example's package |

### A failed run does not move a stage

Only `olf infra project deploy` writes a stage's `ACTIVE.json`, and only after
the rollout succeeded; a failed push or `project build` never reaches it.
`deploy-stage` also fails unless `olf infra project status` then records
exactly the revision it was given. These tests in
`tools/olf/tests/infra/test_deployment_activation.py` cover the pointer:
`test_failed_rollout_leaves_the_previous_revision_active`,
`test_a_pointer_that_will_not_commit_restores_the_previous_activation` and
`test_an_unusable_pull_secret_stops_the_activation_before_any_rollout`.

### Not on a code-only change

The pipeline runs only `olf infra project image`, `build` and `deploy`. It never
runs `olf infra deploy` or `olf infra platform apply`, so a dbt, Floe or Dagster
change never runs Terraform. Run those separately, when the platform itself
changes.

### Later: ECR and ACR from CI

Not shipped, and not needed for a local platform. The planned shape is
provider-native federation instead of stored secrets: GitHub OIDC to an AWS role
that pushes to ECR, and Azure workload identity federation to ACR. Until then
the AWS example above, an OIDC role on a self-hosted runner that holds
`OLF_HOME`, is the supported cloud path.

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
olf infra project status -f openlakeforge.yaml --json
olf infra e2e run --env local -f openlakeforge.yaml --stage prod --suite full
```

`olf infra project status` records the revision active in each stage; PROD shows the
same id as DEV. The e2e run launches every product's job in the PROD Dagster and
requires rows in every Gold mart of the `lakehouse_prod` catalog. In CI, the
`status` output of `deploy-stage` carries that document. Use `--env aws` or
`--env azure` on a cloud platform.
