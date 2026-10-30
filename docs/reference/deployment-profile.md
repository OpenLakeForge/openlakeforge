# Deployment Profile reference

`openlakeforge.yaml` at the project root says where a project deploys, which
stages it runs, how people reach its services and who may sign in. This page
lists every field, its type and default, and the rules `olf` checks across
fields.

The file is validated against `docs/schema/deployment-profile.schema.json` and
then by `tools/olf/olf/common/profile.py`, which is the authority where the two differ.
Check a profile without deploying:

```bash
olf profile validate --project .
olf profile resolve --project . --json
```

Every object rejects fields it does not list. The decisions behind the model are
in [ADR 0011](../adr/0011-deployment-profile-and-stages.md) (profile and stages),
[ADR 0013](../adr/0013-service-access-and-ingress.md) (access) and
[ADR 0014](../adr/0014-identity-and-authorization.md) (identity).

---

## A complete example

```yaml
apiVersion: openlakeforge.io/v1alpha1
kind: DeploymentProfile

metadata:
  name: openlakeforge

spec:
  provider:
    type: local
  preset: slim
  stages:
    dev:
      enabled: true
    prod:
      enabled: true
  access:
    base_domain: olf.localhost
    issuer: local-ca
  identity:
    issuer: github
    github:
      organization: my-org
      teams:
        platform-admin: [platform-team]
        analyst: [analysts]
      oauth_app_secret_ref:
        name: github-oauth-app
```

---

## Envelope

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `apiVersion` | string | yes | Must be `openlakeforge.io/v1alpha1`. |
| `kind` | string | yes | Must be `DeploymentProfile`. |
| `metadata` | object | yes | See below. |
| `spec` | object | yes | See below. |

### `metadata`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes | Names this deployment. Must match `^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`: it becomes the `openlakeforge.io/profile` label on every namespace the deployment owns, and teardown finds them by it. |

---

## `spec`

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `provider` | object | yes | | Where the platform runs. |
| `preset` | `slim` or `full` | yes | | Default optional capabilities for every stage. |
| `stages` | object | yes | | Which lifecycle stages exist. |
| `access` | object | no | absent: no ingress | How user-facing services are published. |
| `identity` | object | no | absent: sign-in off | Who may sign in and which roles they hold. |
| `project` | object | no | absent: the public example image | Your project's source repository and image registry. |
| `query` | object | no | `size: small` | Size of the shared Trino. |
| `storage` | object | no | `size: small` | Size of self-hosted object storage (SeaweedFS). Rejected on `aws`. |

### `spec.provider`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | `local`, `aws` or `azure` | yes | The deployment target. `local` is a kind cluster on this machine. |
| `region` | string, non-empty | no | Cloud region. Rejected when `type` is `local`. Used for `aws`; when unset, `AWS_REGION` and then `eu-west-1` apply. For `azure` the region comes from the foundation tfvars (`AZURE_TFVARS_FILE`). |

### `spec.preset`

| Value | `analytics` default | `governance` default |
| --- | --- | --- |
| `slim` | `false` | `false` |
| `full` | `true` | `true` |

The preset is the default for each stage's `capabilities` (below); a stage that
states its capabilities keeps its own values.

### `spec.query` and `spec.storage`

Each takes one optional field, `size`, which defaults to `small`. There is one
size for the whole platform, not one per stage: a single Trino serves every
stage.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `spec.query.size` | `small` | `small` | The shared Trino. `small` is coordinator only, no workers. |
| `spec.storage.size` | `small` | `small` | The SeaweedFS object storage. Not valid on `aws`. |

`medium` and `large` are reserved names, not usable values. They fail
validation with a message naming the issue that will implement them (#324 for
Trino, #326 for storage volumes), so a profile never asks for capacity the
deployment does not provide. Any other value, and any CPU, memory, worker or
volume field, is rejected as an unknown value or field. A profile that omits
both fields deploys exactly as it did before they existed, and
`olf profile resolve` prints the effective sizes (`sizes: query=small
storage=small`; `query` and `storage` in `--json`). Nothing deploys differently
by size yet.

What each size means on the local profile, as the Helm values set it today:

| Size | Trino (`infra/helm/values/local/trino.yaml`) | SeaweedFS volume server (`seaweedfs.yaml`) | Available |
| --- | --- | --- | --- |
| `small` | Coordinator only (`workers: 0`), 2G heap, 3Gi memory limit | 20Gi PVC | Yes |
| `medium` | Not defined | Not defined | No: [#324](https://github.com/OpenLakeForge/openlakeforge/issues/324), [#326](https://github.com/OpenLakeForge/openlakeforge/issues/326) |
| `large` | Not defined | Not defined | No: [#324](https://github.com/OpenLakeForge/openlakeforge/issues/324), [#326](https://github.com/OpenLakeForge/openlakeforge/issues/326) |

The `small` volume figure is the chart default, not yet derived from
`storage.size`; the profile does not change it. This page gives no data-volume
or host-capacity guidance because none has been measured.

### `spec.stages`

An object with at least one of the keys `dev`, `uat` and `prod`. Any other key is
rejected. A stage left out is disabled: `prod` is never created implicitly.

Each stage takes:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Whether the stage is deployed. |
| `capabilities` | object | the preset's defaults | Optional services in this stage. |

`capabilities` takes:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `analytics` | boolean | `false` | Deploy Superset in this stage. |
| `governance` | boolean | `false` | Deploy OpenMetadata governance for this stage. |

The preset defaults apply only when the stage has no `capabilities` object at
all. Once `capabilities` is written, a capability it leaves out is `false`.
A disabled stage resolves both capabilities to `false` whatever was written.

### `spec.project`

Omit the block and the deployment uses OpenLakeForge's public example image
(`ghcr.io/openlakeforge/openlakeforge/project-code`). Declare it and the deployment is yours:
it never falls back to the example.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `repository` | `https://` or `ssh://` URL | no | Where the project's source lives. Provenance only: `olf project image` and `build` use the checked-out directory, never a fetched ref. |
| `image.repository` | `host[:port]/path` | no on `aws`/`azure` (the provider registry, ECR or ACR, is used), yes on `local` | The project-code image repository. Registry host first, lowercase, no tag or digest. A bare `acme/code` is rejected, not read as Docker Hub. |
| `image.pull_secret_ref.name` | Kubernetes Secret name | no | A `kubernetes.io/dockerconfigjson` Secret you create in each stage namespace, for a private image. The profile holds the name, never the credential. |

The block must declare `repository` or `image`. Unknown fields (`password`,
`token`, ...), credentials or a query string in a URL, and a tag or digest on
the image are rejected, as is a pull secret without an `image`.

The image repository resolves in this order, first match wins
(`olf profile resolve` prints the winner as `project.source`):

1. `spec.project.image.repository` (`profile`);
2. `PROJECT_CODE_IMAGE_REPOSITORY`, or its `AWS_`/`AZURE_` alias
   (`environment`; a malformed value is an error);
3. the provider registry, for a project that declares only `repository` on
   `aws` or `azure` (`provider-default`);
4. the public example, only when `spec.project` is absent (`public-example`).

Existing profiles have no `spec.project` and keep working unchanged.

On the `local` provider the public example is the project-code image the release
published, pinned by digest in the `project_code` entry of the package's
`release/component-catalog.yaml`. `olf deploy` pulls it once on the workstation
and loads it into kind, so no Docker build and no registry credential is needed
and the pods pull nothing. A
source checkout carries no such entry (a release stamps in the digest of the
image built from its own commit), so there `olf deploy` builds the example
locally. So does anything that is not the unmodified example: a `lakehouse_code/`
in your project that differs from the bundled one, `PROJECT_CODE_IMAGE_TAG`, or
a machine that is not amd64 (the release image is amd64 only).
On `aws` and `azure` the same pin is deployed as it is, with the same conditions
(a non-amd64 `*_IMAGE_PLATFORM` also builds): the cluster pulls the public
package, and nothing is built or pushed to ECR or ACR. Anything else builds and
pushes to the provider registry. For the example, `olf project image` prints that
pinned reference without building, so `olf project build` and `olf project deploy`
promote one image digest through the stages.

```yaml
# Local, your own image on GHCR (private: also name the pull secret).
project:
  repository: https://github.com/acme/lakehouse.git
  image:
    repository: ghcr.io/acme/lakehouse-code
    pull_secret_ref: {name: ghcr-pull}
# AWS: ECR is the default registry, so only the source is declared.
project:
  repository: https://github.com/acme/lakehouse.git
# AWS/Azure with an explicit registry.
project:
  image:
    repository: 123456789012.dkr.ecr.eu-west-1.amazonaws.com/acme/lakehouse-code  # or acme.azurecr.io/...
```

> [!NOTE]
> `olf project deploy` consumes `pull_secret_ref`. `olf deploy` does not read
> `spec.project` yet: it still takes the image from the environment variables
> above (deploying the example image is #539).

#### Pulling a private image

`olf` never creates the pull Secret and never reads a registry credential.
Create it yourself in each stage namespace that runs the project, with a
read-only token (for GHCR, a personal access token with `read:packages`; not
the Actions `GITHUB_TOKEN`, which expires with the workflow run):

```bash
kubectl create secret docker-registry ghcr-pull -n olf-dev \
  --docker-server=ghcr.io --docker-username=<user> --docker-password=<token>
```

Repeat for `olf-uat` and `olf-prod`. To rotate, recreate the Secret under the
same name; running pods keep their pulled image and the next pull uses the new
token. Before rolling anything out, `olf project deploy` checks that the Secret
exists in the stage namespace, has type `kubernetes.io/dockerconfigjson` and
lists the image's registry. A failed check leaves the running revision and
`ACTIVE.json` untouched. It does not contact the registry, so an expired or
revoked token shows up as `ImagePullBackOff` on the pod.

The activation then names the Secret in `imagePullSecrets` on the code-location
Deployments (and, through them, Dagster run pods) and on the
`openlakeforge-k8s-log-archive` CronJob, and adds it to the stage's `dagster`
ServiceAccount, which Floe Jobs run as. A rollback restores the Secret the
previous release used.

### `spec.access`

Declaring the block turns the ingress on; `access: {}` asks for the defaults.
Without it there is no ingress at all.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `base_domain` | string | `olf.localhost` | Lowercase DNS name with at least one dot. Every service host is `<service>.<base_domain>` or `<service>-<stage>.<base_domain>`, and the portal is served at the base domain itself. |
| `issuer` | string, `^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$` | `local-ca` | The certificate issuer. The local provider ships only `local-ca` (a CA the platform creates) and `none`, which serves plain HTTP. |

### `spec.identity`

Declares who signs in. `issuer` selects one of two shapes, and the other
shape's fields are rejected.

| Field | Type | Applies to | Meaning |
| --- | --- | --- | --- |
| `issuer` | `github` or `external` | both | **Required.** Which OIDC issuer signs people in. |
| `github` | object | `github` | **Required** for `github`; rejected for `external`. |
| `issuer_url` | string, `https://` URL | `external` | **Required.** The issuer's URL; the token `iss` claim must equal it. It needs a host and no query, fragment, whitespace or control characters. |
| `role_claim` | string, non-empty | `external` | **Required.** The token claim holding role values, for example `groups` or `cognito:groups`. |
| `role_mapping` | object | `external` | **Required.** Canonical role to the claim values that grant it. |
| `client_ids` | object | `external` | Optional issuer-assigned client ids, keyed by consumer. |

For `github`, `issuer_url`, `role_claim`, `role_mapping` and `client_ids` must
not be written: `olf` derives them (`role_claim` becomes `groups`, and each role
maps to `<organization>:<slug>` for its teams).

The canonical roles, highest first, are `platform-admin`, `data-engineer`,
`analyst` and `viewer` (`release/identity-roles.yaml`). Any other role name is
rejected.

#### `spec.identity.github`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `organization` | string, `^[A-Za-z0-9-]+$` | yes | The organisation login exactly as GitHub shows it, case included: Dex compares it exactly. |
| `host` | hostname string | no | A GitHub Enterprise Server hostname, with no scheme or path. Absent means `github.com`. Dex then trusts the platform CA for it. |
| `teams` | object | yes | Canonical role to a list of GitHub team slugs. At least one role. |
| `oauth_app_secret_ref` | object | yes | The Secret holding the GitHub OAuth App client id and secret. |

`teams` rules: each key is a canonical role; each value is a non-empty list of
team slugs as GitHub shows them in the team URL (`olf` lowercases them). A slug
may not repeat within a list, and one team may not map to two roles.

`oauth_app_secret_ref`:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | none; **required** | The Kubernetes Secret's name: a DNS subdomain of at most 253 characters, each label at most 63. You create the Secret; the profile never carries a value. |
| `client_id_key` | string | `client-id` | The Secret key holding the client id. |
| `client_secret_key` | string | `client-secret` | The Secret key holding the client secret. Must differ from `client_id_key`. |

Keys match `[-._a-zA-Z0-9]+` and are at most 253 characters.

#### `spec.identity.role_mapping` (`external` only)

An object whose keys are canonical roles (at least one). Each value is a
non-empty list of non-empty claim-value strings. A value may not repeat within a
role, and one claim value may not map to two roles.

#### `spec.identity.client_ids` (`external` only)

An object with at least one key from `perimeter`, `superset`, `openmetadata` and
`trino`; each value is a non-empty string. A consumer left out uses its own name
as the client id. `external` deploys no issuer: you provide the four client
Secrets the contract names, as described in ADR 0014.

---

## Cross-field rules

| Rule | Rejected when |
| --- | --- |
| At least one stage runs | Every stage is disabled. |
| DEV gates the others | `uat` or `prod` is enabled while `dev` is disabled or absent: every promotion sources from DEV. |
| Storage size is not for managed storage | `spec.storage` is set with `type: aws`: S3 is managed, so there is no volume to size. |
| Region is cloud-only | `spec.provider.region` is set with `type: local`. |
| Identity needs access on local | `type: local` has `spec.identity` but no `spec.access`: sign-in needs the route hostnames. |
| Authentication off publishes nothing | There is no `spec.identity`, and `spec.access` is declared on `aws` or `azure`, or with a `base_domain` not ending in `.localhost` on `local`. Without identity no one signs in, so it is allowed only where nothing is reachable beyond the operator's machine: locally on `*.localhost`, and on the clouds without `spec.access`, where services are reached with `olf forward`. |
| A local project names its image | `type: local` declares `spec.project` without `image.repository`: kind has no default registry. |
| Plain HTTP is local-only | `spec.access.issuer` is `none` and the provider is not `local`, or `base_domain` does not end in `.localhost`. |
| Issuer shape | `external` lacks `issuer_url`, `role_claim` or `role_mapping`; `github` lacks `github` or sets `issuer_url`, `role_claim` or `role_mapping`; `github` is set without `issuer: github`; `client_ids` is set without `issuer: external`. |

On `local`, `olf deploy` also refuses a `spec.access.issuer` other than
`local-ca` or `none` before it creates the cluster.

> [!NOTE]
> `olf init` writes a profile with `access: {issuer: none}` and no
> `spec.identity`: plain HTTP on `*.olf.localhost` with authentication off.

---

## Related

* [ADR 0011: Deployment Profile and stages](../adr/0011-deployment-profile-and-stages.md)
* [ADR 0013: service access](../adr/0013-service-access-and-ingress.md)
* [ADR 0014: identity and authorization](../adr/0014-identity-and-authorization.md)
* [`olf` CLI](cli.md)
